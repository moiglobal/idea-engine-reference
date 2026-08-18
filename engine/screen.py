"""The hard filter (Lesson 2), built in stages because roic.ai has no screener.

The universe is assembled rather than requested: first the list of every
listed primary-line stock, then market caps (converted to dollars) to apply
the size floor, then company profiles for domicile and industry. Each stage
cuts the number of companies that reach the next, more expensive stage, and
the count after each stage is reported so you can see where your filter bites.

This stage only narrows the universe; it never ranks anything.

Country has two meanings and the config chooses one explicitly:
  listing  = the country of the exchange (listing_country_code on the ticker)
  domicile = the country of the company (address.country_code on the profile)
Most investors mean domicile. The two differ often enough to matter.

One honest cost note. With basis "domicile", the strictly correct order pulls
market caps for every listed stock on earth before any country narrowing,
because domicile is only known after fetching a profile. That is tens of
thousands of requests to find, say, US-domiciled names. So when an include
list is set, the engine by default also prefilters on the LISTING country as
a cost saving, and says so in the log. The price of that shortcut: a company
domiciled in an included country but listed only elsewhere is missed. Set
country.prefilter_by_listing to false in config/universe.yaml to run the
expensive, complete version.
"""

from __future__ import annotations

import time

from . import roic
from .currency import MissingRateError, amount_to_usd
from .util import DATA_DIR, num, write_json

SURVIVORS_FILE = DATA_DIR / "universe-survivors.json"
PROGRESS_EVERY = 500  # progress line frequency during per-company stages


def _check_exclusion_labels(exclude_industries, catalogue, universe_industries, logger):
    """A configured industry exclusion that matches nothing must warn loudly.

    Guessing a label produces a filter that silently matches nothing, which
    is the worst kind of bug because it looks like it works. Labels are
    checked against roic.ai's own industry catalogue and the closest real
    labels are suggested for anything unknown.
    """
    import difflib
    catalogue_names = {c.get("name") for c in (catalogue or []) if c.get("name")}
    for label in exclude_industries:
        if catalogue_names and label not in catalogue_names:
            close = difflib.get_close_matches(label, catalogue_names, n=3, cutoff=0.4)
            logger.warning(
                f"Industry exclusion {label!r} is not a roic.ai industry label at all, "
                f"so it excludes nothing. Closest real labels: {close or 'none found'}. "
                "Copy the exact string from config/reference/industries.json.")
        elif label not in universe_industries:
            logger.info(
                f"Industry exclusion {label!r} matched no company in today's "
                "universe. The label is valid; there was simply nothing to exclude.")


def _estimate_and_confirm(n_candidates: int, assume_yes: bool, logger) -> None:
    """Print the request arithmetic before the expensive stages, and on a big
    cold run at a keyboard, ask before starting.

    The cost is roughly one enterprise-value request per candidate, then one
    profile request per size-filter survivor, then three statement requests
    per survivor at the scoring stage (cached companies cost nothing). The
    confirmation only ever happens at an interactive terminal; a scheduled
    run never blocks on a question nobody is there to answer.
    """
    estimate = n_candidates  # EV stage; later stages shrink with the funnel
    if logger:
        logger.info(
            f"Request estimate: up to {n_candidates} enterprise-value requests, "
            f"then one profile per size survivor, then three statements per "
            f"scored survivor. Cached responses cost nothing.")
    if assume_yes or estimate < 5000:
        return
    try:
        answer = input(f"About {estimate}+ requests ahead on a cold cache. "
                       "Continue? [y/N] ").strip().lower()
    except EOFError:
        return  # no keyboard attached after all; proceed
    if answer not in ("y", "yes"):
        raise RuntimeError("Run cancelled at the request-count confirmation.")


def run_screen(api_key: str, universe_cfg: dict, rates: dict, logger=None,
               refresh: bool = False, assume_yes: bool = True,
               price_map: dict | None = None, limit: int = 0) -> tuple:
    """Apply the hard filter. Returns (survivors, stage_counts, exclusions).

    survivors: list of candidate dicts (symbol, name, countries, industry,
      market caps in both currencies, cik) ready for the scoring stage.
    stage_counts: ordered list of (stage name, count) for the run summary.
    exclusions: how many companies each rule removed, including companies
      excluded because their currency had no available dollar rate.

    limit stops the expensive stages as soon as that many companies have
    passed, which is what makes a quick test quick. It has to be applied
    HERE, not to the list this function returns: the cost of a run is one
    enterprise-value request per ticker and one profile per size survivor,
    so trimming the output afterwards saves nothing at all. A limited run is
    a sample of the market in ticker order, not a search of it, and it says
    so in the log and in the stage names.
    """
    sampled = bool(limit)
    log = logger
    country_cfg = universe_cfg.get("country", {}) or {}
    basis = (country_cfg.get("basis") or "domicile").strip().lower()
    if basis not in ("domicile", "listing"):
        raise ValueError(f"country.basis must be 'domicile' or 'listing', not {basis!r}")
    include = [c.upper() for c in (country_cfg.get("include") or [])]
    exclude_countries = {c.upper() for c in (country_cfg.get("exclude") or [])}
    prefilter = bool(country_cfg.get("prefilter_by_listing", True))

    mc = universe_cfg.get("market_cap", {}) or {}
    min_cap, max_cap = num(mc.get("min_usd")), num(mc.get("max_usd"))

    exclude_industries = [s.strip() for s in (universe_cfg.get("exclude_industries") or [])]
    exclude_keywords = [k.lower() for k in (universe_cfg.get("exclude_keywords") or [])]

    stage_counts = []
    exclusions = {"country": 0, "size_floor": 0, "size_ceiling": 0,
                  "industry": 0, "keyword": 0, "no_market_cap": 0,
                  "missing_fx_rate": 0}
    fx_missing_by_ccy: dict = {}

    # --- Stage 1: every listed, primary-line stock ---------------------------
    started = time.time()
    listing_cfg = universe_cfg.get("listing", {}) or {}
    ttl = 0 if refresh else 7
    if basis == "listing" and include:
        rows = []
        for cc in include:
            rows.extend(roic.list_tickers(api_key, cc, ttl_days=ttl, logger=log,
                                          listing_cfg=listing_cfg))
    elif basis == "domicile" and include and prefilter:
        if log:
            log.info(
                "Cost shortcut: prefiltering tickers on LISTING country "
                f"{include} even though the country basis is domicile. A company "
                "domiciled in an included country but listed only elsewhere will "
                "be missed. Set country.prefilter_by_listing: false for the "
                "complete (much slower) version.")
        rows = []
        for cc in include:
            rows.extend(roic.list_tickers(api_key, cc, ttl_days=ttl, logger=log,
                                          listing_cfg=listing_cfg))
    else:
        rows = roic.list_tickers(api_key, None, ttl_days=ttl, logger=log,
                                 listing_cfg=listing_cfg)
    rows = roic.dedupe_primary(rows, logger=log)
    stage_counts.append(("tickers (listed, primary-line stocks)", len(rows)))
    if log:
        log.info(f"Stage 1: {len(rows)} companies after the ticker filters")
        if sampled:
            log.warning(
                f"SAMPLE RUN: stopping each stage at {limit} companies, in "
                "ticker order. This is a quick test of the machinery, not a "
                "search of the market, and the idea it produces is the best "
                "of the sample only. Drop --limit for a real run.")

    # --- Stage 2: listing-country filter (only when basis is listing) --------
    if basis == "listing":
        if include:
            rows = [r for r in rows if (r.get("listing_country_code") or "").upper() in include]
        before = len(rows)
        rows = [r for r in rows if (r.get("listing_country_code") or "").upper() not in exclude_countries]
        exclusions["country"] += before - len(rows)
        stage_counts.append(("listing-country filter", len(rows)))
        if log:
            log.info(f"Stage 2: {len(rows)} companies after the listing-country filter")

    # --- Stage 3: market cap, converted to dollars, floor and ceiling --------
    if not sampled:
        _estimate_and_confirm(len(rows), assume_yes, log)
    survivors_caps = []
    for i, r in enumerate(rows, 1):
        if log and i % PROGRESS_EVERY == 0:
            log.info(f"Stage 3 progress: {i}/{len(rows)} companies "
                     f"({time.time() - started:.0f}s elapsed, "
                     f"{len(survivors_caps)} passing so far)")
        try:
            ev_row = roic.get_enterprise_value_row(api_key, r["symbol"],
                                                   ttl_days=0 if refresh else 1)
        except roic.RoicFatalError:
            raise
        except roic.RoicError as exc:
            if log:
                log.warning(f"Skipped {r['symbol']}: {exc}")
            continue
        cap = num((ev_row or {}).get("market_cap"))
        price_ccy = (ev_row or {}).get("price_currency")
        if cap is None:
            exclusions["no_market_cap"] += 1
            continue
        try:
            # amount_to_usd, not to_usd: aggregate amounts labeled with a
            # sub-unit price currency (London pence) are stated in the major
            # unit, so the market-cap floor must convert them at that rate.
            cap_usd, rate, note = amount_to_usd(cap, price_ccy, rates)
        except MissingRateError as exc:
            exclusions["missing_fx_rate"] += 1
            fx_missing_by_ccy[exc.currency] = fx_missing_by_ccy.get(exc.currency, 0) + 1
            continue
        if min_cap and cap_usd < min_cap:
            exclusions["size_floor"] += 1
            continue
        if max_cap and cap_usd > max_cap:
            exclusions["size_ceiling"] += 1
            continue
        r = dict(r)
        r["market_cap"] = cap
        r["market_cap_currency"] = price_ccy
        r["market_cap_usd"] = cap_usd
        r["market_cap_asof"] = (ev_row or {}).get("period_end_date")
        survivors_caps.append(r)
        if limit and len(survivors_caps) >= limit:
            if log:
                log.info(f"Sample reached at {len(survivors_caps)} companies "
                         f"after {i} of {len(rows)} tickers; stopping stage 3.")
            break
    stage_counts.append((("market-cap floor and ceiling (USD), SAMPLE"
                          if sampled else "market-cap floor and ceiling (USD)"),
                         len(survivors_caps)))
    if log:
        log.info(f"Stage 3: {len(survivors_caps)} companies after the size filter")
        if fx_missing_by_ccy:
            detail = ", ".join(f"{c}: {n}" for c, n in sorted(fx_missing_by_ccy.items()))
            log.warning(
                f"Excluded {exclusions['missing_fx_rate']} companies because no "
                f"dollar rate was available for their currency ({detail}). They "
                "were never compared unconverted.")

    # --- Stage 4: profile: domicile and industry -----------------------------
    survivors = {}
    universe_industries = set()
    # Keep all four label catalogues fresh in config/reference/, as Lesson 2
    # instructs, so the owner can always read the real strings.
    for cat_name in ("countries", "sectors", "exchanges"):
        try:
            roic.get_reference(api_key, cat_name, ttl_days=0 if refresh else 7)
        except roic.RoicError:
            pass  # the industry catalogue below is the load-bearing one
    catalogue = roic.get_reference(api_key, "industries", ttl_days=0 if refresh else 7)
    for i, r in enumerate(survivors_caps, 1):
        if log and i % PROGRESS_EVERY == 0:
            log.info(f"Stage 4 progress: {i}/{len(survivors_caps)} profiles "
                     f"({time.time() - started:.0f}s elapsed)")
        try:
            profile = roic.get_profile(api_key, r["symbol"], ttl_days=0 if refresh else 7)
        except roic.RoicFatalError:
            raise
        except roic.RoicError as exc:
            if log:
                log.warning(f"Skipped {r['symbol']}: {exc}")
            continue
        address = (profile or {}).get("address") or {}
        domicile = (address.get("country_code") or "").upper()
        industry = (profile or {}).get("industry") or ""
        name = (profile or {}).get("name") or r.get("name") or ""
        universe_industries.add(industry)

        if basis == "domicile":
            if include and domicile not in include:
                exclusions["country"] += 1
                continue
            if domicile in exclude_countries:
                exclusions["country"] += 1
                continue
        if industry in exclude_industries:
            exclusions["industry"] += 1
            continue
        haystack = f"{name} {industry}".lower()
        if any(k in haystack for k in exclude_keywords):
            exclusions["keyword"] += 1
            continue

        sym = r["symbol"]
        if sym in survivors:
            continue
        price_info = (price_map or {}).get(sym) or {}
        survivors[sym] = {
            "symbol": sym,
            "name": name,
            "domicile_country": domicile or None,
            "listing_country": r.get("listing_country_code"),
            "exchange": r.get("exchange"),
            "sector": (profile or {}).get("sector"),
            "industry": industry or None,
            "reporting_currency": (profile or {}).get("fundamental_currency"),
            "market_cap": r.get("market_cap"),
            "market_cap_currency": r.get("market_cap_currency"),
            "market_cap_usd": r.get("market_cap_usd"),
            "market_cap_asof": r.get("market_cap_asof"),
            "price": price_info.get("close"),
            "price_date": price_info.get("date"),
            "price_currency": price_info.get("currency"),
            "cik": r.get("cik"),
        }
        if limit and len(survivors) >= limit:
            if log:
                log.info(f"Sample reached at {len(survivors)} companies after "
                         f"{i} of {len(survivors_caps)} profiles; stopping stage 4.")
            break
    result = list(survivors.values())
    stage_counts.append((("domicile, industry and keyword filters, SAMPLE"
                          if sampled else "domicile, industry and keyword filters"),
                         len(result)))
    if log:
        log.info(f"Stage 4: {len(result)} companies pass the full hard filter")
        _check_exclusion_labels(exclude_industries, catalogue, universe_industries, log)

    # --- Stage 5: write the survivors somewhere readable ---------------------
    write_json(SURVIVORS_FILE, {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stage_counts": stage_counts,
        "exclusions": exclusions,
        "fx_missing_by_currency": fx_missing_by_ccy,
        "survivors": result,
    })
    if log:
        log.info(f"Survivors written to {SURVIVORS_FILE}")
    return result, stage_counts, exclusions
