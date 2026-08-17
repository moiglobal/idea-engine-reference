"""The SEC EDGAR filing check (Lesson 3, step 7).

Before any memo is emailed, the chosen company's headline figures are checked
against what the company actually filed with the US Securities and Exchange
Commission. EDGAR is free, needs no key, and holds the filings themselves,
which makes it the one check that does not depend on the data vendor.

Ground rules, from the course and the SEC's own requirements:
  Every request sends a User-Agent header identifying the caller by name and
  email (the SEC_USER_AGENT setting). Requests stay under 10 per second.
  Domestic filers tag their numbers under the us-gaap taxonomy; foreign
  filers under ifrs-full. Both are tried, in that order.

The check compares three figures for the same fiscal year: revenue, operating
cash flow, and capital expenditure. Comparison happens in the company's own
reporting currency, using EDGAR's matching currency unit, so an exchange rate
can never masquerade as a disagreement.

The outcome is ALWAYS one of three lines, written into the memo's data
caveats every time, never omitted:
  1. The two sources agree within 1 percent.
  2. They disagree: both figures and the percentage gap are shown.
  3. The company does not file with the SEC, so no independent check was
     possible. (A company that files but has no comparable figure for the
     fiscal year in question is reported under this outcome too, with the
     reason stated, because the honest summary is the same: nobody has
     independently confirmed these numbers.)
A disagreement above 5 percent is escalated to the top of the memo, not
buried in the caveats.
"""

from __future__ import annotations

import time

from .util import g, num

EDGAR_BASE = "https://data.sec.gov/api/xbrl/companyfacts"
AGREE_TOLERANCE = 0.01     # within 1 percent counts as agreement
ESCALATE_TOLERANCE = 0.05  # above 5 percent goes to the top of the memo
_MIN_INTERVAL = 0.12       # seconds between requests; stays under 10 per second
_last_call = [0.0]

# Candidate XBRL tags for each figure, tried in order. Companies choose their
# own tags from the taxonomy dictionary, so several names can mean the same
# line. These are the common ones; a company using none of them is reported
# as "not comparable" rather than guessed at.
TAGS = {
    "revenue": {
        "us-gaap": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                    "Revenues",
                    "RevenueFromContractWithCustomerIncludingAssessedTax",
                    "SalesRevenueNet"],
        "ifrs-full": ["Revenue", "RevenueFromContractsWithCustomers"],
    },
    "operating_cash_flow": {
        "us-gaap": ["NetCashProvidedByUsedInOperatingActivities",
                    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
        "ifrs-full": ["CashFlowsFromUsedInOperatingActivities"],
    },
    "capital_expenditure": {
        "us-gaap": ["PaymentsToAcquirePropertyPlantAndEquipment",
                    "PaymentsToAcquireProductiveAssets",
                    "PaymentsToAcquirePropertyPlantAndEquipmentIntangibleAssets"],
        "ifrs-full": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                      "AcquisitionsOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwill"],
    },
}


def _fetch_companyfacts(cik: str, user_agent: str, timeout: int = 60):
    """One EDGAR companyfacts document, politely rate-limited."""
    import requests  # lazy

    wait = _MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()

    cik10 = str(cik).lstrip("0").rjust(10, "0")  # EDGAR wants ten digits, zero-padded
    resp = requests.get(f"{EDGAR_BASE}/CIK{cik10}.json",
                        headers={"User-Agent": user_agent}, timeout=timeout)
    if resp.status_code == 404:
        return None
    if resp.status_code == 403:
        raise RuntimeError(
            "SEC EDGAR returned 403. The User-Agent header is missing or does "
            "not identify you; check SEC_USER_AGENT in .env (name and email).")
    resp.raise_for_status()
    return resp.json()


def _annual_value_for_year(facts: dict, figure: str, fiscal_end: str, currency: str):
    """The filed annual value of one figure for the fiscal year ending fiscal_end.

    Looks under us-gaap first and ifrs-full second, walks the candidate tags,
    and matches on the period end date (within a few days, to survive 52/53
    week calendars) and on an annual form (10-K, 20-F, 40-F). Only units in
    the requested currency are considered, so like is compared with like.
    Returns (value, tag, taxonomy) or (None, reason, None).
    """
    from .util import parse_date

    want_end = parse_date(fiscal_end)
    if want_end is None:
        return None, "no fiscal period end to match on", None
    all_facts = facts.get("facts", {})
    tried_taxonomy = False
    for taxonomy in ("us-gaap", "ifrs-full"):
        tax_facts = all_facts.get(taxonomy)
        if not tax_facts:
            continue
        tried_taxonomy = True
        for tag in TAGS[figure][taxonomy]:
            units = (tax_facts.get(tag) or {}).get("units") or {}
            entries = units.get(currency)
            if not entries:
                continue
            for e in entries:
                end = parse_date(e.get("end"))
                form = e.get("form") or ""
                fp = e.get("fp") or ""
                if (end and abs((end - want_end).days) <= 10
                        and form in ("10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A")
                        and fp == "FY" and e.get("val") is not None):
                    # Prefer a full-year duration where start is given.
                    start = parse_date(e.get("start"))
                    if start and (end - start).days < 300:
                        continue  # a quarterly figure inside an annual form
                    return num(e.get("val")), tag, taxonomy
    if not tried_taxonomy:
        return None, "filing carries neither us-gaap nor ifrs-full facts", None
    return None, f"no {currency} annual figure matching fiscal year end {fiscal_end}", None


def run_filing_check(company: dict, record: dict, user_agent: str, logger=None) -> dict:
    """Compare roic.ai's figures with the company's own SEC filing.

    company: the scored company dict (carries cik and latest_period).
    record: the full data record, whose statements_original rows hold the
      UNCONVERTED figures in the reporting currency; comparing those against
      EDGAR's same-currency units keeps exchange rates out of the comparison.

    Returns a dict with:
      outcome: "agree" | "disagree" | "no_check"
      caveat_line: the sentence for the memo's data caveats (never empty)
      escalation: a sentence for the top of the memo, or None
      details: per-figure comparison rows
    """
    cik = company.get("cik") or record.get("cik")
    if not cik:
        return {
            "outcome": "no_check",
            "caveat_line": ("Independent check: this company does not file with "
                            "the SEC, so no independent check of these figures "
                            "was possible. Read its annual report before acting."),
            "escalation": None,
            "details": [],
        }

    originals = record.get("statements_original") or {}
    inc_rows = originals.get("income") or []
    cf_rows = originals.get("cashflow") or []
    if not inc_rows:
        return {
            "outcome": "no_check",
            "caveat_line": ("Independent check: not possible, because no roic.ai "
                            "statement was available to compare against."),
            "escalation": None,
            "details": [],
        }
    newest_inc = inc_rows[0]
    newest_cf = cf_rows[0] if cf_rows else {}
    fiscal_end = g(newest_inc, "period_end_date", "date")
    currency = newest_inc.get("currency") or "USD"

    roic_values = {
        "revenue": num(newest_inc.get("is_sales_revenue_turnover")),
        "operating_cash_flow": num(newest_cf.get("cf_cash_from_oper")),
        "capital_expenditure": num(newest_cf.get("cf_cap_expenditures")),
    }

    try:
        facts = _fetch_companyfacts(cik, user_agent)
    except Exception as exc:
        if logger:
            logger.warning(f"Filing check could not reach EDGAR: {exc}")
        return {
            "outcome": "no_check",
            "caveat_line": (f"Independent check: SEC EDGAR could not be reached "
                            f"({exc}); no independent check was possible today."),
            "escalation": None,
            "details": [],
        }
    if facts is None:
        return {
            "outcome": "no_check",
            "caveat_line": ("Independent check: this company's CIK returned no "
                            "SEC filing, so no independent check was possible. "
                            "Read its annual report before acting."),
            "escalation": None,
            "details": [],
        }
    return evaluate_against_facts(roic_values, facts, fiscal_end, currency)


def evaluate_against_facts(roic_values: dict, facts: dict, fiscal_end: str,
                           currency: str) -> dict:
    """Compare roic.ai figures with an already-fetched EDGAR facts document.

    Pure function, no network: the offline self-test exercises the agree,
    disagree, and not-comparable outcomes through here with canned filings.
    """
    details, gaps = [], []
    for figure, roic_val in roic_values.items():
        if roic_val is None:
            details.append({"figure": figure, "status": "roic.ai value missing"})
            continue
        filed, tag_or_reason, taxonomy = _annual_value_for_year(
            facts, figure, fiscal_end, currency)
        if filed is None:
            details.append({"figure": figure, "status": f"not comparable: {tag_or_reason}"})
            continue
        # Capital expenditure signs differ between sources (an outflow can be
        # negative or positive), so magnitudes are compared.
        a, b = abs(roic_val), abs(filed)
        gap = abs(a - b) / max(a, b) if max(a, b) > 0 else 0.0
        details.append({
            "figure": figure, "roic": roic_val, "filed": filed,
            "currency": currency, "gap_pct": round(gap * 100, 2),
            "tag": tag_or_reason, "taxonomy": taxonomy,
        })
        gaps.append((figure, roic_val, filed, gap))

    if not gaps:
        reasons = "; ".join(d.get("status", "") for d in details if d.get("status"))
        return {
            "outcome": "no_check",
            "caveat_line": (f"Independent check: this company files with the SEC, "
                            f"but no comparable figures could be matched for the "
                            f"fiscal year ending {fiscal_end} ({reasons}). No "
                            "independent confirmation was possible; read the filing."),
            "escalation": None,
            "details": details,
        }

    worst = max(g_ for _, _, _, g_ in gaps)
    if worst <= AGREE_TOLERANCE:
        line = (f"Independent check: revenue, operating cash flow and capital "
                f"expenditure for the fiscal year ending {fiscal_end} agree with "
                f"the company's SEC filing within 1 percent.")
        return {"outcome": "agree", "caveat_line": line, "escalation": None,
                "details": details}

    parts = []
    for figure, rv, fv, gap in gaps:
        if gap > AGREE_TOLERANCE:
            parts.append(f"{figure.replace('_', ' ')}: roic.ai {rv:,.0f} vs "
                         f"filed {fv:,.0f} {currency}, a {gap * 100:.1f}% gap")
    line = ("Independent check: roic.ai and the company's SEC filing DISAGREE "
            f"for the fiscal year ending {fiscal_end}. " + "; ".join(parts) +
            ". Believe the filing.")
    escalation = None
    if worst > ESCALATE_TOLERANCE:
        escalation = (f"DATA WARNING: roic.ai and the company's SEC filing "
                      f"disagree by up to {worst * 100:.1f}% on headline figures. "
                      "Details in the data caveats. Verify against the filing "
                      "before spending time on this idea.")
    return {"outcome": "disagree", "caveat_line": line, "escalation": escalation,
            "details": details}
