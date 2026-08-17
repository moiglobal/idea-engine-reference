"""Currency conversion: the single place currency is ever handled.

Why this file exists. roic.ai hands back each figure in its native currency
and tells you which one, in separate fields: `currency` on every statement
row (the currency the company keeps its books in) and `price_currency` on
anything derived from the share price, such as market cap. The two can differ
for the same company; a business that keeps its books in dollars while its
shares trade in London pence has a dollar income statement and a pence market
cap. Any ratio that puts one over the other without converting is wrong by an
exchange rate, and nothing about the numbers looks wrong on inspection.

So the rule, locked in the course: everything is converted to US dollars here,
at fetch time, and no calculation anywhere downstream may combine two figures
unless both have been through this file. A company whose currency has no
available rate is excluded and reported, by currency and count. It is never
compared unconverted.

All conversions use the day's latest rate, including for historical statement
lines. That is the course's design: ratios of two statement figures cancel the
rate anyway, and cross-figure ratios then sit on one consistent rate.

Sub-units: some shares are quoted in a fraction of a currency, most commonly
London's pence (code GBX, one hundredth of a pound). roic.ai carries a direct
GBX rate; when a sub-unit code has no direct rate, the fallback table below
converts through the parent currency, and every such conversion is noted on
the record.
"""

from __future__ import annotations

from datetime import date as _date

from .util import CACHE_DIR, num, read_json, write_json

# Sub-unit codes with no guaranteed direct rate: code -> (parent, multiplier).
# One unit of the code equals `multiplier` units of the parent currency.
SUB_UNITS = {
    "GBX": ("GBP", 0.01),   # London pence
    "GBP.": ("GBP", 0.01),  # occasional vendor spelling of pence
    "ZAC": ("ZAR", 0.01),   # South African cents
    "ZAX": ("ZAR", 0.01),
    "ILA": ("ILS", 0.01),   # Israeli agorot
    "ILX": ("ILS", 0.01),
}


class MissingRateError(Exception):
    """No US dollar rate is available for this currency today."""

    def __init__(self, currency):
        self.currency = currency
        super().__init__(f"No USD exchange rate available for {currency}")


def build_rates(raw_pairs: list, rate_date: str) -> dict:
    """Turn roic.ai forex rows into a simple {currency: dollars per unit} table.

    roic.ai lists pairs in both directions (for example JPYUSD and USDJPY).
    The direct CCYUSD close is preferred; when only USDCCY exists, the rate is
    inverted. Null closes are skipped: a missing rate stays missing.
    """
    usd_per = {"USD": 1.0}
    sources = {}
    inverted = {}
    for row in raw_pairs or []:
        sym = (row.get("symbol") or "").replace("FX:", "")
        close = num(row.get("close"))
        if not sym or close is None or close <= 0:
            continue
        if sym.endswith("USD") and len(sym) > 3:
            ccy = sym[:-3]
            usd_per[ccy] = close
            sources[ccy] = sym
        elif sym.startswith("USD") and len(sym) > 3:
            ccy = sym[3:]
            inverted.setdefault(ccy, (1.0 / close, sym))
    for ccy, (rate, sym) in inverted.items():
        if ccy not in usd_per:  # direct pair wins; inversion is the fallback
            usd_per[ccy] = rate
            sources[ccy] = f"1/{sym}"
    return {"date": rate_date, "usd_per": usd_per, "sources": sources}


def load_rates(api_key: str, logger=None, force: bool = False) -> dict:
    """The day's rate table, fetched once and cached for the day."""
    from . import roic  # local import so the self-test never touches it

    cache_file = CACHE_DIR / f"fx-{_date.today().isoformat()}.json"
    if not force:
        cached = read_json(cache_file, default=None)
        if cached:
            return cached
    raw = roic.fetch_usd_rates_raw(api_key)
    rate_date = next((r.get("date") for r in raw if r.get("date")), _date.today().isoformat())
    rates = build_rates(raw, rate_date)
    write_json(cache_file, rates)
    if logger:
        logger.info(f"Loaded {len(rates['usd_per'])} USD exchange rates dated {rates['date']}")
    return rates


def to_usd(amount, currency, rates: dict):
    """Convert one figure to dollars. Returns (usd, rate, note).

    note is None for an ordinary conversion, or a plain sentence when a
    sub-unit code (such as pence) was routed through its parent currency.
    Raises MissingRateError when no rate exists; callers exclude the company
    and count the exclusion. There is no silent fallback.
    """
    amount = num(amount)
    if amount is None:
        return None, None, None
    ccy = (currency or "").strip()
    if not ccy:
        raise MissingRateError("(blank currency field)")
    usd_per = rates.get("usd_per", {})
    if ccy in usd_per:
        return amount * usd_per[ccy], usd_per[ccy], None
    if ccy.upper() in SUB_UNITS:
        parent, mult = SUB_UNITS[ccy.upper()]
        if parent in usd_per:
            rate = usd_per[parent] * mult
            note = (f"{ccy} is a sub-unit of {parent}; converted at "
                    f"{mult} {parent} per {ccy}.")
            return amount * rate, rate, note
    raise MissingRateError(ccy)


def amount_to_usd(amount, price_currency, rates: dict):
    """Convert an AGGREGATE money amount (market cap, enterprise value) that
    is labeled with a price currency.

    Why this differs from to_usd: when a share trades in a sub-unit such as
    London pence (GBX), roic.ai labels the row GBX but states the aggregate
    amounts in the MAJOR unit (pounds); only per-share prices are actually in
    pence. Verified on 2026-08-10 across five LSE companies by comparing
    market_cap against price times shares outstanding: the pounds reading
    matched within a few percent every time, the pence reading was wrong by
    100x every time. So aggregates convert at the parent currency's rate,
    and to_usd (which applies the 1/100) stays correct for per-share prices.
    """
    ccy = (price_currency or "").strip().upper()
    if ccy in SUB_UNITS:
        parent = SUB_UNITS[ccy][0]
        usd, rate, _ = to_usd(amount, parent, rates)
        note = (f"Aggregate amounts labeled {ccy} are stated in {parent} by "
                f"roic.ai (the sub-unit applies to per-share prices only); "
                f"converted at the {parent} rate.")
        return usd, rate, note
    return to_usd(amount, ccy or price_currency, rates)


# The statement fields the engine consumes that are amounts of money and must
# therefore be converted. Share counts and dates are deliberately absent.
MONETARY_INCOME = ("is_sales_revenue_turnover", "is_gross_profit", "is_oper_income",
                   "is_pretax_income", "is_inc_tax_exp", "is_int_expense",
                   "is_net_income")
MONETARY_BALANCE = ("bs_cash_near_cash_item", "bs_st_borrow", "bs_lt_borrow",
                    "bs_total_equity", "bs_goodwill", "bs_disclosed_intangibles",
                    "bs_tot_asset")
MONETARY_CASHFLOW = ("cf_cash_from_oper", "cf_cap_expenditures", "cf_depr_amort")


def convert_record(record: dict, rates: dict) -> dict:
    """Convert an assembled company record to US dollars, in place.

    What gets converted, and with which currency field:
      market cap and enterprise value: the price_currency of the EV row;
      every consumed statement line: that row's own currency field
      (checked row by row, in case a company changed reporting currency).

    The record keeps a full audit trail under record["fx"]: each currency
    seen, the rate used, the rate date, and any sub-unit notes. It also keeps
    the original unconverted statement rows under record["statements_original"]
    so the SEC filing check can compare like with like, and gets
    record["all_usd"] = True, which metrics.py refuses to work without.

    Raises MissingRateError if any needed rate is absent. The caller excludes
    the company and reports it; that is the honest failure.
    """
    if record.get("all_usd"):
        return record  # already converted; converting twice would corrupt it
    fx = {"rate_date": rates.get("date"), "statement_currency": None,
          "price_currency": None, "rates_used": {}, "notes": []}

    def _convert(amount, ccy):
        usd, rate, note = to_usd(amount, ccy, rates)
        if rate is not None:
            fx["rates_used"][ccy] = rate
        if note and note not in fx["notes"]:
            fx["notes"].append(note)
        return usd

    def _convert_amount(amount, price_ccy):
        usd, rate, note = amount_to_usd(amount, price_ccy, rates)
        if rate is not None:
            fx["rates_used"][price_ccy] = rate
        if note and note not in fx["notes"]:
            fx["notes"].append(note)
        return usd

    # 1. Price-derived figures, labeled with the price currency of the EV row.
    # Aggregates go through amount_to_usd, which knows that sub-unit codes
    # such as pence label the row while the amounts are in the major unit.
    ev_row = record.get("ev_row") or {}
    price_ccy = ev_row.get("price_currency")
    stmt_ccy_ev = ev_row.get("currency")
    fx["price_currency"] = price_ccy
    if ev_row:
        record["market_cap_usd"] = _convert_amount(ev_row.get("market_cap"), price_ccy)
        record["ev_reported_usd"] = _convert_amount(ev_row.get("enterprise_value"), price_ccy)
        # short_and_long_term_debt is a balance-sheet figure, so it carries the
        # statement currency of the EV row. It is only used as a cross-check.
        record["ev_debt_usd"] = _convert(ev_row.get("short_and_long_term_debt"),
                                         stmt_ccy_ev or price_ccy)
        record["market_cap_asof"] = ev_row.get("period_end_date")
        if ev_row.get("fx_applied"):
            note = ("roic.ai reports this row as already translated by the "
                    "vendor (fx_applied), so figures passed through one vendor "
                    "conversion before ours; spot-check against the filing.")
            if note not in fx["notes"]:
                fx["notes"].append(note)

    # 2. Statement rows, each converted with its own currency field.
    record["statements_original"] = {
        kind: [dict(r) for r in (record.get(kind) or [])]
        for kind in ("income", "balance", "cashflow")
    }
    for kind, fields in (("income", MONETARY_INCOME),
                        ("balance", MONETARY_BALANCE),
                        ("cashflow", MONETARY_CASHFLOW)):
        for row in record.get(kind) or []:
            ccy = row.get("currency")
            if fx["statement_currency"] is None:
                fx["statement_currency"] = ccy
            for f in fields:
                if row.get(f) is not None:
                    row[f] = _convert(row[f], ccy)

    record["fx"] = fx
    record["all_usd"] = True
    record["currency_mismatch"] = bool(
        fx["statement_currency"] and price_ccy
        and fx["statement_currency"] != price_ccy)
    return record
