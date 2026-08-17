"""roic.ai client: tickers, profiles, statements, enterprise value, prices.

Everything the engine knows about a company comes through this file. The key
is sent in an "Authorization: Bearer" header and never as the apikey query
parameter; roic.ai documents that the two must not be combined in one request.

How errors are handled, in plain terms:
  401 means the key is missing or wrong. Fatal: the run stops and emails you.
  402 means your roic.ai plan does not cover the endpoint. Fatal, same alert.
  429 means too many requests per minute. The client waits the number of
      seconds roic.ai names in its Retry-After header, then tries again.
  Network errors and 5xx server errors are retried with a growing pause.

Responses are cached on disk under data/cache/, keyed by symbol, so a normal
daily run reuses what it already fetched. How long each kind of data is
trusted before being refetched (the "TTL", time to live, in days):

  tickers list            7 days  (the universe changes slowly)
  reference catalogues    7 days  (countries, sectors, industries, exchanges)
  company profile         7 days  (carries the reporting calendar)
  statements             30 days, OR sooner when the profile shows a newer
                                  reporting period than the cached statements
  enterprise value        1 day   (the market-cap source; note that roic.ai
                                  reports market cap as of the period end,
                                  not as of today, so this refresh mainly
                                  catches newly posted periods)
  latest prices           1 day   (fetched in bulk via the batch endpoint)

`requests` is imported lazily so the offline self-test needs no network stack.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from .util import CACHE_DIR, REFERENCE_DIR, num, read_json, write_json

BASE = "https://api.roic.ai"
V = "/v3.0.0"

# One place to see every endpoint the engine uses. Paths and field names are
# taken from Lessons 2 and 3 and were verified against
# https://api.roic.ai/v3.0.0/openapi.json on 2026-08-10.
EP_TICKERS = f"{V}/tickers"
EP_PROFILE = f"{V}/company/profile"
EP_EV = f"{V}/fundamental/enterprise-value"
EP_INCOME = f"{V}/fundamental/income-statement"
EP_BALANCE = f"{V}/fundamental/balance-sheet"
EP_CASHFLOW = f"{V}/fundamental/cash-flow"
EP_PRICES_LATEST = f"{V}/stock-prices/latest"
EP_FX_LATEST = f"{V}/forex-prices/latest"

# Cache statistics for the end-of-run report ("X from cache, Y refetched").
CACHE_STATS = {"hits": 0, "fetches": 0}


class RoicError(RuntimeError):
    """Something went wrong talking to roic.ai."""


class RoicFatalError(RoicError):
    """A 401 or 402: no memo can be produced until the owner acts.

    401: the API key stopped working. Regenerate it and update .env.
    402: the roic.ai plan no longer covers an endpoint the engine uses.
    """


_SESSION = None


def _session():
    """One shared HTTP connection, reused across thousands of calls."""
    global _SESSION
    import requests  # lazy import, see module docstring
    if _SESSION is None:
        _SESSION = requests.Session()
    return _SESSION


def _get(path: str, api_key: str, params: dict | None = None,
         retries: int = 4, timeout: int = 60):
    """GET one roic.ai endpoint and return parsed JSON."""
    import requests  # lazy import, see module docstring

    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"{BASE}{path}" if path.startswith("/") else path
    last_error = None
    for attempt in range(retries):
        try:
            resp = _session().get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 401:
            raise RoicFatalError(
                "roic.ai returned 401 (authentication). The API key is missing, "
                "wrong, or was sent both as a header and a query parameter. "
                "Check ROIC_API_KEY in .env; no memo until this is fixed.")
        if resp.status_code == 402:
            raise RoicFatalError(
                f"roic.ai returned 402 (payment required) for {path}. Your plan "
                "no longer covers this endpoint. No memo until this is fixed.")
        if resp.status_code == 429:
            # Too many requests. roic.ai names the wait, in seconds.
            wait = num(resp.headers.get("Retry-After")) or 5.0
            time.sleep(min(wait, 120))
            last_error = "HTTP 429 (rate limited)"
            continue
        if resp.status_code in (500, 502, 503, 504):
            last_error = f"HTTP {resp.status_code}"
            time.sleep(2 * (attempt + 1))
            continue
        raise RoicError(f"roic.ai {path} HTTP {resp.status_code}: {resp.text[:200]}")
    raise RoicError(f"roic.ai {path} failed after {retries} attempts: {last_error}")


def get_paged(path: str, api_key: str, params: dict | None = None,
              logger=None, label: str = "", progress_every: int = 0,
              started=None):
    """Follow next_page_url until there are no more pages; return all rows.

    Page links expire after one hour, so pages are consumed promptly, one
    after another. If a link does expire mid-run, the whole listing restarts
    once from the beginning rather than resuming from a dead link.
    """
    for attempt in range(2):
        rows = []
        resp = _get(path, api_key, params)
        while True:
            rows.extend(resp.get("data") or [])
            if progress_every and logger and len(rows) % progress_every < 2000:
                elapsed = (time.time() - started) if started else 0
                logger.info(f"{label}: {len(rows)} rows so far "
                            f"({elapsed:.0f}s elapsed)")
            next_url = resp.get("next_page_url")
            if not next_url:
                return rows
            try:
                resp = _get(next_url, api_key)
            except RoicError as exc:
                if "expired" in str(exc).lower() and attempt == 0:
                    if logger:
                        logger.warning(f"{label}: page link expired; restarting the listing once.")
                    break  # restart the outer loop from page one
                raise
    raise RoicError(f"{label or path}: paging failed twice; giving up.")


# --- Disk cache --------------------------------------------------------------
def _cache_path(kind: str, key: str) -> Path:
    safe = str(key).replace("/", "_").replace("\\", "_").replace(":", "_")
    return CACHE_DIR / kind / f"{safe}.json"


def _age_days(fetched_at: str) -> float:
    try:
        ts = datetime.fromisoformat(fetched_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 1e9


def cache_get(kind: str, key: str, ttl_days: float):
    """Return the cached payload if it is fresh enough, else None."""
    cached = read_json(_cache_path(kind, key), default=None)
    if cached and _age_days(cached.get("fetched_at", "")) < ttl_days:
        CACHE_STATS["hits"] += 1
        return cached["payload"]
    return None


def cache_put(kind: str, key: str, payload) -> None:
    CACHE_STATS["fetches"] += 1
    write_json(_cache_path(kind, key), {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    })


# --- Universe and reference data --------------------------------------------
def list_tickers(api_key: str, listing_country_code: str | None = None,
                 ttl_days: float = 7, logger=None, listing_cfg: dict | None = None):
    """Every listed, primary-line common stock, optionally for one listing country.

    Filters are applied by roic.ai itself and come from the listing block of
    config/universe.yaml: type=stock excludes funds and depositary receipts,
    status=listed drops delisted lines, is_primary=true keeps one line per
    listing venue. One company can still appear on several exchanges, so
    callers dedupe with `dedupe_primary` below.
    """
    listing_cfg = listing_cfg or {}
    ltype = listing_cfg.get("type", "stock")
    status = listing_cfg.get("status", "listed")
    primary_only = listing_cfg.get("primary_only", True)
    key = f"tickers-{listing_country_code or 'ALL'}-{ltype}-{status}-{primary_only}"
    cached = cache_get("universe", key, ttl_days)
    if cached is not None:
        return cached
    params = {"type": ltype, "status": status, "limit": 2000}
    if primary_only:
        params["is_primary"] = "true"
    if listing_country_code:
        params["listing_country_code"] = listing_country_code
    rows = get_paged(EP_TICKERS, api_key, params, logger=logger,
                     label=f"tickers {listing_country_code or 'ALL'}",
                     progress_every=10000, started=time.time())
    cache_put("universe", key, rows)
    return rows


def dedupe_primary(rows: list, logger=None) -> list:
    """One line per company: keep the row that is its own primary_symbol.

    roic.ai marks a primary line per exchange, so a company listed in five
    places can carry several is_primary flags. The primary_symbol field names
    the company's one true home line; keeping only rows where the symbol IS
    that home line collapses the duplicates. Rows with no primary_symbol at
    all (rare, mostly stale minor-venue lines) are kept only when their
    symbol has not been seen through a primary_symbol reference.
    """
    keep, seen_primary = [], set()
    orphans = []
    for r in rows:
        sym, prim = r.get("symbol"), r.get("primary_symbol")
        if prim:
            seen_primary.add(prim)
            if sym == prim:
                keep.append(r)
        else:
            orphans.append(r)
    kept_orphans = [r for r in orphans if r.get("symbol") not in seen_primary]
    if logger:
        logger.info(f"Primary-line dedupe: {len(rows)} rows -> "
                    f"{len(keep) + len(kept_orphans)} companies "
                    f"({len(orphans) - len(kept_orphans)} duplicate venue lines dropped)")
    return keep + kept_orphans


def get_reference(api_key: str, name: str, ttl_days: float = 7):
    """One of the four catalogues: countries, sectors, industries, exchanges.

    Saved to config/reference/<name>.json so the owner can read the real
    labels, as Lesson 2 instructs. Note this endpoint rejects large limit
    values, so it is paged with roic.ai's own defaults.
    """
    cached = cache_get("reference", name, ttl_days)
    if cached is None:
        cached = get_paged(f"{V}/{name}", api_key)
        cache_put("reference", name, cached)
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    write_json(REFERENCE_DIR / f"{name}.json", cached)
    return cached


# --- Per-company data --------------------------------------------------------
def _ident(identifier: str) -> str:
    """Make a symbol safe inside a URL path.

    Some listings carry a slash in the symbol (share-class and preferred
    lines such as AMEX:PCG/PA), which would otherwise be read as a path
    separator and answered with a 404. The colon stays as-is; roic.ai
    expects EXCHANGE:SYMBOL.
    """
    from urllib.parse import quote
    return quote(str(identifier), safe=":")


def get_profile(api_key: str, identifier: str, ttl_days: float = 7):
    cached = cache_get("profile", identifier, ttl_days)
    if cached is not None:
        return cached
    payload = _get(f"{EP_PROFILE}/{_ident(identifier)}", api_key)
    cache_put("profile", identifier, payload)
    return payload


def get_enterprise_value_row(api_key: str, identifier: str, ttl_days: float = 1):
    """The newest trailing-twelve-month row from the enterprise-value endpoint.

    This is the only place market cap appears in roic.ai. The market_cap and
    enterprise_value fields are stated as of the row's period_end_date (the
    end of the latest reported quarter or year), NOT as of today, and come in
    the price currency. Callers must state that as-of date wherever the
    figure is shown.
    """
    # The row is wrapped so that "this company has no enterprise-value data"
    # (a real, cacheable answer) is distinguishable from "not in the cache";
    # otherwise every no-data company would be refetched on every run.
    cached = cache_get("ev", identifier, ttl_days)
    if cached is not None:
        return cached.get("row") if isinstance(cached, dict) and "row" in cached else cached
    resp = _get(f"{EP_EV}/{_ident(identifier)}", api_key,
                {"period_type": "ttm", "order": "desc", "limit": 1})
    rows = resp.get("data") or []
    row = rows[0] if rows else None
    cache_put("ev", identifier, {"row": row})
    return row


def get_statements(api_key: str, identifier: str, profile: dict | None = None,
                   limit: int = 10, ttl_days: float = 30):
    """Up to `limit` annual periods of the three statements, newest first.

    Refreshed when the cached copy is older than ttl_days OR when the
    profile's reporting calendar shows a period newer than the newest cached
    statement, which is the Lesson 5 rule: statements change when a company
    reports, not with the calendar.
    """
    cached = cache_get("statements", identifier, ttl_days)
    if cached is not None:
        newest = ((cached.get("income") or [{}])[0] or {}).get("period_end_date")
        calendar_end = ((profile or {}).get("reporting_calendar") or {}).get("current_period_end")
        if not (newest and calendar_end and str(calendar_end) > str(newest)):
            return cached
    params = {"period_type": "annual", "order": "desc", "limit": limit}
    payload = {
        "income": (_get(f"{EP_INCOME}/{_ident(identifier)}", api_key, params).get("data") or []),
        "balance": (_get(f"{EP_BALANCE}/{_ident(identifier)}", api_key, params).get("data") or []),
        "cashflow": (_get(f"{EP_CASHFLOW}/{_ident(identifier)}", api_key, params).get("data") or []),
    }
    cache_put("statements", identifier, payload)
    return payload


def get_latest_statement_period(api_key: str, identifier: str):
    """The single most recent reported period end date, any period type.

    This is the no-repeat rule's fallback for companies whose profile has no
    reporting calendar, exactly as Lesson 3 specifies: ask the income
    statement endpoint for its single most recent period with order=desc and
    limit=1, and use that period_end_date.
    """
    resp = _get(f"{EP_INCOME}/{_ident(identifier)}", api_key,
                {"order": "desc", "limit": 1})
    rows = resp.get("data") or []
    return rows[0].get("period_end_date") if rows else None


def get_stock_splits(api_key: str, identifier: str, ttl_days: float = 30):
    """Stock splits for one company, for the share-count sanity check.

    A share count that moves more than 50% in one year is either a data
    error or a stock split; the splits list is what tells the two apart, so
    a split-adjusting company is flagged rather than wrongly dropped.
    """
    cached = cache_get("splits", identifier, ttl_days)
    if cached is not None:
        return cached
    resp = _get(f"{V}/stock-splits", api_key,
                {"identifier": identifier, "order": "desc", "limit": 100})
    rows = resp.get("data") or []
    cache_put("splits", identifier, rows)
    return rows


# --- Field-existence check ---------------------------------------------------
# Every roic.ai field the engine's calculations depend on. Checked against
# the live openapi specification at the start of each run (cached weekly), so
# a field the vendor renames surfaces as a loud message instead of a missing
# value that quietly scores neutral. Lesson 2 calls this closing the loop.
REQUIRED_FIELDS = {
    "v3.IncomeStatementFinancialPeriod": [
        "is_sales_revenue_turnover", "is_gross_profit", "is_oper_income",
        "is_pretax_income", "is_inc_tax_exp", "is_int_expense",
        "is_net_income", "is_avg_num_sh_for_eps", "is_sh_for_diluted_eps",
        "period_end_date", "fiscal_year", "currency"],
    "v3.BalanceSheetFinancialPeriod": [
        "bs_cash_near_cash_item", "bs_st_borrow", "bs_lt_borrow",
        "bs_total_equity", "bs_goodwill", "bs_disclosed_intangibles",
        "bs_sh_out", "bs_tot_asset"],
    "v3.CashFlowFinancialPeriod": [
        "cf_cash_from_oper", "cf_cap_expenditures", "cf_depr_amort"],
    "v3.EnterpriseValueFinancialPeriod": [
        "market_cap", "enterprise_value", "short_and_long_term_debt",
        "price_currency", "currency", "period_end_date"],
}


def verify_fields(logger=None, ttl_days: float = 7) -> list:
    """Confirm every required field still exists in roic.ai's specification.

    Downloads https://api.roic.ai/v3.0.0/openapi.json (readable without a
    key), checks each field the engine relies on, and returns the list of
    missing ones. The caller treats a non-empty list as an alert: those
    fields would otherwise become quiet Nones in the scoring.
    """
    cached = cache_get("reference", "openapi-fieldcheck", ttl_days)
    if cached is not None:
        return cached
    import requests  # lazy
    try:
        spec = requests.get(f"{BASE}{V}/openapi.json", timeout=60).json()
    except Exception as exc:
        if logger:
            logger.warning(f"Could not download the roic.ai specification to "
                           f"verify field names ({exc}); continuing without the check.")
        return []
    schemas = (spec.get("components") or {}).get("schemas") or {}
    missing = []
    for schema_name, fields in REQUIRED_FIELDS.items():
        props = (schemas.get(schema_name) or {}).get("properties") or {}
        for f in fields:
            if f not in props:
                missing.append(f"{schema_name}.{f}")
    cache_put("reference", "openapi-fieldcheck", missing)
    if logger:
        if missing:
            logger.error(f"Field check FAILED: {len(missing)} field(s) the engine "
                         f"relies on are no longer in roic.ai's specification: "
                         f"{', '.join(missing)}")
        else:
            logger.info("Field check: every roic.ai field the engine relies on "
                        "still exists in the current specification.")
    return missing


# --- Prices ------------------------------------------------------------------
def fetch_latest_prices(api_key: str, exchanges: list, ttl_days: float = 1,
                        logger=None) -> dict:
    """Latest close per symbol for every exchange named, via the batch endpoint.

    Uses /v3.0.0/stock-prices/latest, which walks an exchange in pages of up
    to 2,000 rather than one request per company. It needs the Individual
    plan or higher. A null price means the share has not traded in 10 days;
    that is missing data, not a price of zero, and it stays None here.
    """
    out = {}
    for exch in sorted(set(e for e in exchanges if e)):
        cached = cache_get("prices", exch, ttl_days)
        if cached is None:
            rows = get_paged(EP_PRICES_LATEST, api_key,
                             {"exchange": exch, "limit": 2000},
                             logger=logger, label=f"prices {exch}")
            cached = {r["symbol"]: {"close": r.get("close"), "date": r.get("date"),
                                    "currency": r.get("currency")}
                      for r in rows if r.get("symbol")}
            cache_put("prices", exch, cached)
        out.update(cached)
    return out


def get_latest_price(api_key: str, identifier: str):
    """Latest quote for one company (used for the memo header)."""
    row = _get(f"{EP_PRICES_LATEST}/{_ident(identifier)}", api_key)
    return row or None


def fetch_usd_rates_raw(api_key: str):
    """Every currency pair against USD, paginated to exhaustion, for currency.py.

    The pairs themselves (which currencies exist, and in which direction)
    come back in the same response, which is how the engine finds the right
    pair for each currency; a separate /v3.0.0/forex-pairs listing would add
    nothing the rate rows do not already carry.
    """
    return get_paged(EP_FX_LATEST, api_key, {"currency": "USD", "limit": 2000})


# --- Assembling one company's full record ------------------------------------
def get_company_record(api_key: str, ticker_row: dict, rates: dict,
                       refresh: bool = False, logger=None) -> dict:
    """Everything the scorer and the memo need for one company, converted.

    Combines the ticker row (symbol, exchange, CIK), the profile (business
    description, domicile, industry, reporting calendar), the newest
    enterprise-value row (market cap), and up to 10 years of the three annual
    statements. Currency conversion happens here, at fetch time, through
    currency.convert_record; a company with an unavailable rate raises
    MissingRateError for the caller to exclude and count.
    """
    from . import currency

    identifier = ticker_row["symbol"]
    ttl = 0 if refresh else None
    profile = get_profile(api_key, identifier, ttl_days=ttl if ttl == 0 else 7)
    ev_row = get_enterprise_value_row(api_key, identifier, ttl_days=ttl if ttl == 0 else 1)
    stmts = get_statements(api_key, identifier, profile=profile,
                           ttl_days=ttl if ttl == 0 else 30)
    try:
        splits = get_stock_splits(api_key, identifier, ttl_days=ttl if ttl == 0 else 30)
    except RoicFatalError:
        raise
    except RoicError:
        splits = []  # the sanity check then treats big share moves as unexplained

    # The no-repeat fallback, per Lesson 3: when the profile carries no
    # reporting calendar at all, ask the income statement endpoint for the
    # single most recent period and use its end date instead.
    calendar = (profile or {}).get("reporting_calendar") or {}
    schedule = (profile or {}).get("earnings_schedule") or {}
    period_fallback = None
    if not calendar.get("current_period_end") and not schedule.get("last_release_date"):
        try:
            period_fallback = get_latest_statement_period(api_key, identifier)
        except RoicError:
            period_fallback = None

    address = (profile or {}).get("address") or {}
    record = {
        "symbol": identifier,
        "name": (profile or {}).get("name") or ticker_row.get("name"),
        "exchange": ticker_row.get("exchange") or (profile or {}).get("exchange"),
        "domicile_country": address.get("country_code"),
        "listing_country": ticker_row.get("listing_country_code"),
        "sector": (profile or {}).get("sector"),
        "industry": (profile or {}).get("industry"),
        "description": (profile or {}).get("description")
                       or (profile or {}).get("short_description"),
        "cik": ticker_row.get("cik") or (profile or {}).get("cik"),
        "reports_per_year": calendar.get("reports_per_year"),
        "current_period_end": calendar.get("current_period_end"),
        "last_release_date": schedule.get("last_release_date"),
        "current_period_end_fallback": period_fallback,
        "splits": splits,
        "ev_row": ev_row,
        "income": stmts.get("income") or [],
        "balance": stmts.get("balance") or [],
        "cashflow": stmts.get("cashflow") or [],
    }
    return currency.convert_record(record, rates)
