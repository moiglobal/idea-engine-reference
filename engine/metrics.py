"""Compute factor metrics from RAW financial statements.

The engine deliberately does not use roic.ai's precomputed ratio endpoints.
You cannot see how a vendor ratio was built, what it did with leases, or how
it treated an unusual item. Everything here is derived from the income
statement, balance sheet, and cash flow statement lines, using these roic.ai
fields (verified against the openapi specification on 2026-08-10):

  Income statement: is_sales_revenue_turnover (revenue), is_gross_profit,
    is_oper_income (operating income, used as EBIT), is_pretax_income,
    is_inc_tax_exp (tax expense), is_int_expense (interest expense),
    is_net_income, is_sh_for_diluted_eps and is_avg_num_sh_for_eps (shares).
  Balance sheet: bs_cash_near_cash_item (cash), bs_st_borrow plus
    bs_lt_borrow ADDED TOGETHER for total debt (there is no single total-debt
    field), bs_total_equity, bs_goodwill, bs_disclosed_intangibles,
    bs_sh_out, bs_tot_asset.
  Cash flow: cf_cash_from_oper (operating cash flow), cf_cap_expenditures
    (capital expenditure), cf_depr_amort (depreciation and amortisation).

EBITDA is constructed as operating income plus depreciation and amortisation,
rather than read from a vendor field, for the same inspectability reason.

The input is one company record built by roic.get_company_record (or
fabricated by the self-test), already converted to US dollars by currency.py.
This module refuses to run on a record that has not been through conversion:
no calculation here may combine two figures unless both are in dollars.

A missing value and a zero are different things throughout. A company with no
reported capital expenditure is not a company with zero capital expenditure;
missing inputs make a metric missing, and the scorer treats missing as
neutral rather than as good or bad.
"""

from __future__ import annotations

import statistics
from typing import Optional

from .util import g, num, safe_div, parse_date

# Direction of "good" for each metric. The scorer inverts the "lower" ones.
METRIC_DIRECTION = {
    "fcf_to_firm_yield": "higher",
    "earnings_yield": "higher",
    "roic_multi_year_avg": "higher",
    "roe": "higher",
    "gross_margin": "higher",
    "operating_margin": "higher",
    "margin_stability": "higher",
    "share_count_change": "lower",
    "roic_consistency": "higher",
    "fcf_positive_frequency": "higher",
    "net_debt_to_ebitda": "lower",
    "interest_coverage": "higher",
    "revenue_cagr": "higher",
    "fcf_per_share_cagr": "higher",
}

MAX_YEARS = 10
AVG_YEARS = 5          # window for multi-year averages
TAX_RATE_CAP = 0.40    # effective tax rates are capped to a 0 to 40% band
MIN_YEARS = 3          # fewer years of statements than this fails sanity
SHARE_JUMP_LIMIT = 0.50  # a >50% one-year share-count move fails sanity


def _sorted_newest_first(rows):
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    rows.sort(key=lambda r: (parse_date(g(r, "period_end_date", "date"))
                             or _year_fallback(r)), reverse=True)
    return rows[:MAX_YEARS]


def _year_fallback(r):
    from datetime import date
    y = num(g(r, "fiscal_year", "year"))
    return date(int(y), 12, 31) if y else date(1900, 1, 1)


def _pstdev(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    return statistics.pstdev(vals)


def _build_year_rows(record: dict):
    """Align the three statements by fiscal year (newest first) into rows."""
    inc = {g(r, "fiscal_year"): r for r in _sorted_newest_first(record.get("income"))}
    bal = {g(r, "fiscal_year"): r for r in _sorted_newest_first(record.get("balance"))}
    cf = {g(r, "fiscal_year"): r for r in _sorted_newest_first(record.get("cashflow"))}
    years = [y for y in inc if y is not None and y in bal and y in cf]
    years.sort(reverse=True)
    rows = []
    for y in years[:MAX_YEARS]:
        I, B, C = inc[y], bal[y], cf[y]
        st_debt = num(g(B, "bs_st_borrow"))
        lt_debt = num(g(B, "bs_lt_borrow"))
        # Total debt is short-term plus long-term borrowings, added. When one
        # side is reported and the other is absent, the absent side is treated
        # as zero, because vendors commonly omit a borrowing line the company
        # does not have. When BOTH are absent, total debt is missing, not zero.
        if st_debt is None and lt_debt is None:
            debt = None
        else:
            debt = (st_debt or 0.0) + (lt_debt or 0.0)
        rows.append({
            "fiscal_year": y,
            "date": g(I, "period_end_date", "date"),
            "revenue": num(g(I, "is_sales_revenue_turnover")),
            "gross": num(g(I, "is_gross_profit")),
            "ebit": num(g(I, "is_oper_income")),
            "pretax": num(g(I, "is_pretax_income")),
            "tax": num(g(I, "is_inc_tax_exp")),
            "interest": num(g(I, "is_int_expense")),
            "net_income": num(g(I, "is_net_income")),
            "shares": num(g(I, "is_sh_for_diluted_eps", "is_avg_num_sh_for_eps")),
            "debt": debt,
            "cash": num(g(B, "bs_cash_near_cash_item")),
            "equity": num(g(B, "bs_total_equity")),
            "goodwill": num(g(B, "bs_goodwill")),
            "intangibles": num(g(B, "bs_disclosed_intangibles")),
            "assets": num(g(B, "bs_tot_asset")),
            "sh_out": num(g(B, "bs_sh_out")),
            "ocf": num(g(C, "cf_cash_from_oper")),
            "capex": num(g(C, "cf_cap_expenditures")),
            "da": num(g(C, "cf_depr_amort")),
        })
    return rows


def _split_fiscal_years(record: dict) -> set:
    """Fiscal years in which a stock split executed, from roic.ai's splits list.

    Matches a split's execution date to the fiscal-year windows implied by
    the income statement period end dates, so a 4:1 split in March lands in
    the right fiscal year whatever the company's year end.
    """
    from datetime import timedelta
    splits = record.get("splits") or []
    if not splits:
        return set()
    ends = {}
    for r in record.get("income") or []:
        fy, end = r.get("fiscal_year"), parse_date(r.get("period_end_date"))
        if fy and end:
            ends[fy] = end
    years = set()
    for s in splits:
        ex = parse_date(s.get("execution_date"))
        if not ex:
            continue
        for fy, end in ends.items():
            if end - timedelta(days=365) < ex <= end + timedelta(days=366):
                years.add(fy)
    return years


def _effective_tax_rate(row) -> Optional[float]:
    """The company's own effective tax rate for one year, capped to 0 to 40%.

    Computed from the tax expense and pre-tax income lines, never assumed
    from a statutory rate. A loss year, or a year missing either line, has no
    computable rate and returns None; the caller decides what that means for
    each metric.
    """
    pretax, tax = row["pretax"], row["tax"]
    if pretax is None or tax is None or pretax <= 0:
        return None
    return min(max(tax / pretax, 0.0), TAX_RATE_CAP)


def _fcff_for_row(row, flags) -> Optional[float]:
    """Free cash flow to the FIRM for one year.

    Definition, locked in the course: operating cash flow, PLUS after-tax
    interest expense, MINUS capital expenditure. The add-back matters:
    operating cash flow is reported after interest has been paid, so it
    belongs to shareholders alone, while enterprise value prices the whole
    business including its debt. Dividing one by the other flatters leveraged
    companies. Adding back the after-tax interest makes the numerator the
    cash available to all providers of capital, matching the denominator.

    Handling the inputs honestly:
      Operating cash flow or capital expenditure missing: no figure this year.
      Interest expense missing (some companies stopped disclosing it): the
        add-back is zero and the company is flagged; this can only understate
        the yield, never inflate it.
      Tax rate not computable (a loss year): the cap of the band (40%) is
        used for the add-back only, which is the conservative bound because a
        higher assumed rate means a smaller add-back.
      Capital expenditure sign varies by source, so its magnitude is
        subtracted.
    """
    if row["ocf"] is None or row["capex"] is None:
        return None
    interest = row["interest"]
    if interest is None:
        flags.add("interest_missing_treated_as_zero")
        after_tax_interest = 0.0
    else:
        rate = _effective_tax_rate(row)
        if rate is None:
            rate = TAX_RATE_CAP
        after_tax_interest = abs(interest) * (1.0 - rate)
    return row["ocf"] + after_tax_interest - abs(row["capex"])


def _roic_for_row(row) -> Optional[float]:
    """Return on invested capital for one year.

    Definition, locked in the course: NOPAT over invested capital, where
    NOPAT is EBIT times one minus the company's own effective tax rate
    (capped to 0 to 40%), and invested capital is total equity plus total
    debt minus cash. Goodwill is INCLUDED in invested capital: acquisitions
    are capital the owners paid for, and excluding goodwill flatters serial
    acquirers by pretending that money was never spent.

    A year whose tax rate cannot be computed contributes no observation;
    there is no statutory-rate substitute.
    """
    ebit = row["ebit"]
    rate = _effective_tax_rate(row)
    if ebit is None or rate is None:
        return None
    nopat = ebit * (1.0 - rate)
    if row["equity"] is None or row["debt"] is None or row["cash"] is None:
        return None
    invested = row["equity"] + row["debt"] - row["cash"]
    if invested <= 0:
        return None
    return nopat / invested


def compute_metrics(record: dict) -> dict:
    if not record.get("all_usd"):
        raise ValueError(
            f"{record.get('symbol')}: record has not been through currency "
            "conversion. No calculation may combine unconverted figures; "
            "run it through currency.convert_record first.")

    rows = _build_year_rows(record)
    years = len(rows)
    market_cap = num(record.get("market_cap_usd"))
    flags: set = set()
    n_income = len([r for r in record.get("income") or [] if isinstance(r, dict)])
    n_cashflow = len([r for r in record.get("cashflow") or [] if isinstance(r, dict)])

    out = {
        "symbol": record.get("symbol"),
        "name": record.get("name"),
        "country": record.get("domicile_country") or record.get("listing_country"),
        "domicile_country": record.get("domicile_country"),
        "listing_country": record.get("listing_country"),
        "exchange": record.get("exchange"),
        "sector": record.get("sector"),
        "industry": record.get("industry"),
        "cik": record.get("cik"),
        "marketCap": market_cap,
        "market_cap_asof": record.get("market_cap_asof"),
        "price": None,          # filled by the caller from the price store
        "price_date": None,
        "price_currency_original": (record.get("fx") or {}).get("price_currency"),
        "statement_currency_original": (record.get("fx") or {}).get("statement_currency"),
        "fx": record.get("fx"),
        "currency_mismatch": bool(record.get("currency_mismatch")),
        "reports_per_year": record.get("reports_per_year"),
        "current_period_end": record.get("current_period_end"),
        "last_release_date": record.get("last_release_date"),
        "current_period_end_fallback": record.get("current_period_end_fallback"),
        "years": years,
        "latest_period": (rows[0]["date"] if rows else None),
        "metrics": {k: None for k in METRIC_DIRECTION},
        "absolutes": {},
        "flags": [],
        "sanity_ok": True,
        "sanity_reasons": [],
        "drop_codes": [],
    }
    m, a = out["metrics"], out["absolutes"]

    def fail(code, reason):
        out["sanity_ok"] = False
        out["sanity_reasons"].append(reason)
        out["drop_codes"].append(code)

    # --- Sanity gate ---------------------------------------------------------
    if years < MIN_YEARS:
        # Say WHICH statement is short: a company with income statements but
        # few cash flow years is the coverage gap the Friday report tracks.
        if n_cashflow < min(MIN_YEARS, n_income):
            fail("missing_cash_flow",
                 f"missing or incomplete cash flow data ({n_cashflow} cash flow "
                 f"years against {n_income} income years)")
        else:
            fail("too_few_years", f"only {years} years of statements")
    latest = rows[0] if rows else None
    if latest is None or not latest["revenue"] or latest["revenue"] <= 0:
        fail("missing_revenue", "missing or non-positive revenue")
    if market_cap is None or market_cap <= 0:
        fail("missing_market_cap", "missing market cap")
    # A share count that moved more than 50% in one adjacent fiscal year is
    # either a stock split or a data error. The splits list from roic.ai
    # tells the two apart: a move with a split executed in that window is
    # real and flagged; a move with no split is treated as a data error and
    # dropped, with the reason stated.
    split_years = _split_fiscal_years(record)
    for newer, older in zip(rows, rows[1:]):
        if (newer["shares"] and older["shares"]
                and newer["fiscal_year"] and older["fiscal_year"]
                and newer["fiscal_year"] - older["fiscal_year"] == 1):
            move = abs(newer["shares"] - older["shares"]) / abs(older["shares"])
            if move > SHARE_JUMP_LIMIT:
                if newer["fiscal_year"] in split_years or older["fiscal_year"] in split_years:
                    flags.add(f"share count moved {move * 100:.0f}% in fiscal "
                              f"{newer['fiscal_year']}, explained by a stock split")
                else:
                    fail("share_count_jump",
                         f"share count moved {move * 100:.0f}% in fiscal "
                         f"{newer['fiscal_year']} with no matching stock split; "
                         "possible data error, excluded pending a manual check")
                    break
    if not out["sanity_ok"]:
        return out

    # --- Enterprise value ----------------------------------------------------
    # The enterprise_value field from roic.ai's enterprise-value endpoint is
    # used when present (converted to dollars): it is internally consistent
    # with the market cap on the same row and includes preferred equity and
    # minority interest. When the vendor figure is absent, EV is constructed
    # from converted pieces (market cap plus total debt minus cash) and the
    # construction is flagged. Net debt still comes from the statement lines.
    debt, cash = latest["debt"], latest["cash"]
    ev_reported = num(record.get("ev_reported_usd"))
    ev_constructed = None
    if debt is not None and cash is not None:
        ev_constructed = market_cap + debt - cash
        a["net_debt"] = debt - cash
    if ev_reported and ev_reported > 0:
        ev = ev_reported
        a["ev_source"] = "roic.ai enterprise_value field"
    elif ev_constructed is not None:
        ev = ev_constructed
        a["ev_source"] = "constructed: market cap + total debt - cash"
        flags.add("vendor enterprise value missing; EV constructed from the balance sheet")
    else:
        fail("missing_enterprise_value",
             "no usable enterprise value (vendor field absent and debt or "
             "cash missing from the latest balance sheet)")
        return out
    a["enterprise_value"] = ev
    if ev <= 0:
        fail("non_positive_ev", "non-positive enterprise value")
        return out
    if latest["revenue"] and ev / latest["revenue"] > 500:
        fail("absurd_ev", f"absurd enterprise value ({ev / latest['revenue']:.0f}x revenue); "
             "likely a data error")
        return out

    # Cross-check the two EV readings against each other when both exist.
    # A wide gap flags a data problem worth a look.
    if ev_reported and ev_constructed and ev_reported > 0:
        gap = abs(ev_constructed / ev_reported - 1.0)
        a["ev_reported_usd"] = ev_reported
        a["ev_constructed_usd"] = ev_constructed
        if gap > 0.15:
            flags.add(f"constructed EV differs from roic.ai's by {gap * 100:.0f}% "
                      "(different as-of dates, preferred equity or minority "
                      "interest can explain part of it)")

    # --- Valuation -----------------------------------------------------------
    # Multi-year averages use the AVG_YEARS most recent fiscal years (five by
    # default), not the five most recent values wherever they sit, so the
    # window means what it says even when a year is missing.
    fcffs_all = [_fcff_for_row(r, flags) for r in rows]
    fcffs_present = [x for x in fcffs_all if x is not None]
    fcffs_window = [x for x in fcffs_all[:AVG_YEARS] if x is not None]
    avg_fcff = statistics.mean(fcffs_window) if fcffs_window else None
    a["avg_fcff_5y"] = avg_fcff
    a["avg_window_years"] = len(fcffs_window)
    m["fcf_to_firm_yield"] = safe_div(avg_fcff, ev)
    m["earnings_yield"] = safe_div(latest["ebit"], ev)
    a["fcf_to_firm_yield_pct"] = m["fcf_to_firm_yield"] * 100 if m["fcf_to_firm_yield"] is not None else None
    a["earnings_yield_pct"] = m["earnings_yield"] * 100 if m["earnings_yield"] is not None else None
    a["ev_ebit"] = safe_div(ev, latest["ebit"])

    # --- Returns on capital --------------------------------------------------
    # The average uses the AVG_YEARS most recent fiscal years; the
    # consistency measure uses every available year, since steadiness is a
    # property of the whole history.
    roics_all = [_roic_for_row(r) for r in rows]
    roics = [x for x in roics_all if x is not None]
    roics_window = [x for x in roics_all[:AVG_YEARS] if x is not None]
    m["roic_multi_year_avg"] = statistics.mean(roics_window) if roics_window else None
    m["roic_consistency"] = (-_pstdev(roics)) if _pstdev(roics) is not None else None
    m["roe"] = safe_div(latest["net_income"], latest["equity"])
    a["roic_pct"] = m["roic_multi_year_avg"] * 100 if m["roic_multi_year_avg"] is not None else None
    a["effective_tax_rate_latest"] = _effective_tax_rate(latest)

    # --- Margins -------------------------------------------------------------
    gms = [x for x in (safe_div(r["gross"], r["revenue"]) for r in rows) if x is not None]
    oms = [x for x in (safe_div(r["ebit"], r["revenue"]) for r in rows) if x is not None]
    m["gross_margin"] = statistics.mean(gms) if gms else None
    m["operating_margin"] = statistics.mean(oms) if oms else None
    m["margin_stability"] = (-_pstdev(oms)) if _pstdev(oms) is not None else None
    a["operating_margin_pct"] = m["operating_margin"] * 100 if m["operating_margin"] is not None else None
    a["gross_margin_pct"] = m["gross_margin"] * 100 if m["gross_margin"] is not None else None

    # --- Capital discipline and consistency ----------------------------------
    oldest = rows[-1]
    s_new, s_old = latest["shares"], oldest["shares"]
    if s_new is not None and s_old not in (None, 0):
        m["share_count_change"] = (s_new - s_old) / abs(s_old)
        a["share_count_change_pct"] = m["share_count_change"] * 100
    pos = sum(1 for x in fcffs_present if x > 0)
    m["fcf_positive_frequency"] = safe_div(pos, len(fcffs_present))

    # --- Balance sheet -------------------------------------------------------
    # EBITDA is constructed: operating income plus depreciation and
    # amortisation from the cash flow statement. Both lines must be present.
    ebitda = (latest["ebit"] + latest["da"]
              if latest["ebit"] is not None and latest["da"] is not None else None)
    a["ebitda"] = ebitda
    if ebitda and ebitda > 0 and debt is not None and cash is not None:
        m["net_debt_to_ebitda"] = (debt - cash) / ebitda
        a["net_debt_to_ebitda"] = m["net_debt_to_ebitda"]
    interest = latest["interest"]
    if interest is not None and interest > 0 and latest["ebit"] is not None:
        m["interest_coverage"] = latest["ebit"] / interest
    elif latest["ebit"] is not None and latest["ebit"] > 0 and debt == 0:
        # Borrowings are reported and are exactly zero, so a missing interest
        # line here means an interest bill of zero, a known fact rather than
        # missing data. The 100x stand-in ranks the debt-free company at the
        # strong end of coverage, which is where it belongs.
        m["interest_coverage"] = 100.0
    # Interest missing while debt exists (or debt itself missing) stays
    # missing: we do not know the coverage and will not pretend to.
    a["interest_coverage"] = m["interest_coverage"]

    # --- Growth --------------------------------------------------------------
    span = min(AVG_YEARS, years - 1)
    if span >= 1:
        old_rev, new_rev = rows[span]["revenue"], latest["revenue"]
        if old_rev and old_rev > 0 and new_rev and new_rev > 0:
            m["revenue_cagr"] = (new_rev / old_rev) ** (1 / span) - 1
            a["revenue_cagr_pct"] = m["revenue_cagr"] * 100
        # Per-share free cash flow growth uses the EQUITY definition
        # (operating cash flow minus capital expenditure), because per-share
        # growth measures what accrues to an owner of one share. The
        # enterprise-level rule applies to the yield against enterprise
        # value, not to a per-share growth rate.
        def _fcfe_ps(r):
            if r["ocf"] is None or r["capex"] is None:
                return None
            return safe_div(r["ocf"] - abs(r["capex"]), r["shares"])
        new_ps, old_ps = _fcfe_ps(latest), _fcfe_ps(rows[span])
        if new_ps and old_ps and new_ps > 0 and old_ps > 0:
            m["fcf_per_share_cagr"] = (new_ps / old_ps) ** (1 / span) - 1

    # --- Tangible book, for the memo's price to tangible book ----------------
    # Tangible book is equity minus goodwill and disclosed intangibles. A
    # missing goodwill or intangibles line is treated as zero here because
    # companies without them simply do not report the line; equity itself
    # must be present.
    if latest["equity"] is not None:
        tangible = latest["equity"] - (latest["goodwill"] or 0.0) - (latest["intangibles"] or 0.0)
        a["tangible_book"] = tangible
        if tangible > 0:
            a["ptbv"] = market_cap / tangible
        else:
            a["ptbv"] = None
            flags.add("tangible book is negative; price to tangible book not meaningful")

    if record.get("currency_mismatch"):
        fxi = record.get("fx") or {}
        flags.add(f"statement currency {fxi.get('statement_currency')} differs from "
                  f"price currency {fxi.get('price_currency')}; both converted, spot-check worthy")
    out["flags"] = sorted(flags)
    return out
