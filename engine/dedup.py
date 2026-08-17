"""The no-repeat rule with a reporting-period reset (Lesson 3).

A company is eligible today if it has never been sent, or if it has reported
a new financial period since it was last sent. Fresh numbers earn a fresh
look; without them, a name that keeps scoring well would land in the inbox
every day.

How a new period is detected, in order of trust:
  1. reporting_calendar.current_period_end from the company profile. This is
     a plain date, which makes it the primary signal: roic.ai's documentation
     contradicts itself on whether period labels look like "Q3" or "2026-Q2",
     so labels are never relied on for the comparison.
  2. earnings_schedule.last_release_date, also from the profile. Either date
     moving forward counts as a new period.
  3. Fallback, when a profile carries no reporting calendar at all: the
     period_end_date of the single newest annual income statement.

The method used is logged for every company, as Lesson 3 asks, so odd
behaviour (a fiscal-year change, a lagging vendor) can be diagnosed later.

The reports_per_year field is recorded with each history entry. The rule
itself never assumes a quarterly rhythm; it simply asks "is there newer data
than last time", which adapts to quarterly, half-yearly, and annual filers
alike. The recorded value is context for the log and for cache decisions.

History lives in data/history.json. Entries written by the pre-port engine
(which stored a single "last_period" date) are still understood.
"""

from __future__ import annotations

import re

from .util import HISTORY_FILE, parse_date, read_json, today_str, write_json

# Accepts "2026-Q2", "Q3", "FY2026" and similar label styles, used ONLY as a
# secondary sanity signal; the plain current_period_end date decides.
_PERIOD_LABEL = re.compile(r"^(?:FY)?(\d{4})?[-\s]?(?:Q([1-4]))?$", re.IGNORECASE)


def load_history() -> dict:
    return read_json(HISTORY_FILE, default={})


def save_history(history: dict) -> None:
    write_json(HISTORY_FILE, history)


def _dates_from_company(company: dict):
    """The comparison dates for a company, plus which method supplied them.

    Returns (period_end_signal, last_release_date, method). The period-end
    signal is, in order of trust: the profile's current_period_end; the
    single most recent statement period fetched as the Lesson 3 fallback when
    the profile has no reporting calendar; and finally the newest annual
    statement already in hand.
    """
    cpe = parse_date(company.get("current_period_end"))
    lrd = parse_date(company.get("last_release_date"))
    if cpe or lrd:
        return cpe, lrd, "reporting_calendar"
    fallback = parse_date(company.get("current_period_end_fallback"))
    if fallback:
        return fallback, None, "statement_endpoint_fallback"
    return parse_date(company.get("latest_period")), None, "newest_statement"


def is_eligible(company: dict, history: dict, logger=None) -> bool:
    symbol = company.get("symbol")
    prior = history.get(symbol)
    if not prior:
        return True  # never sent

    cpe, lrd, method = _dates_from_company(company)
    if logger:
        logger.info(f"No-repeat check for {symbol}: using {method}")

    # The stored reference is the best period-end date known at send time,
    # whichever field held it, so a company tracked through the fallback
    # method is never banished for lacking a reporting calendar. Entries
    # written by the pre-port engine stored a single "last_period" date.
    prior_dates = [parse_date(prior.get(k)) for k in
                   ("current_period_end", "newest_statement", "last_period")]
    prior_dates = [d for d in prior_dates if d]
    prior_ref = max(prior_dates) if prior_dates else None
    prior_lrd = parse_date(prior.get("last_release_date"))

    # Eligible when either signal has moved forward since the last send.
    if cpe and prior_ref and cpe > prior_ref:
        return True
    if lrd and prior_lrd and lrd > prior_lrd:
        return True
    if lrd and prior_lrd is None and prior_ref and lrd > prior_ref:
        # No release date was stored last time; a release after the stored
        # period end still means fresh numbers.
        return True
    # Nothing comparable moved, or nothing comparable exists on either side:
    # do not resend on no information.
    return False


def select_idea(ranked: list, history: dict, logger=None):
    """From the ranked list, return (chosen, runners_up, n_eligible).

    chosen is the highest-scoring eligible company; runners_up are the next
    four eligible names, for context in the memo and the log.
    """
    eligible = [c for c in ranked if is_eligible(c, history, logger=logger)]
    if not eligible:
        return None, [], 0
    return eligible[0], eligible[1:5], len(eligible)


def record_sent(company: dict, history: dict) -> None:
    """Write down what was known when this company was sent."""
    cpe, _, method = _dates_from_company(company)
    history[company["symbol"]] = {
        "last_sent": today_str(),
        # Store the period-end signal actually used, whichever method
        # supplied it, so the next eligibility check compares like with like.
        "current_period_end": cpe.isoformat() if cpe else None,
        "last_release_date": company.get("last_release_date"),
        "newest_statement": company.get("latest_period"),
        "reports_per_year": company.get("reports_per_year"),
        "method": method,
    }
    save_history(history)


def parse_period_label(label):
    """Best-effort parse of a period label like "Q3" or "2026-Q2".

    Only used for logging context. Returns (year or None, quarter or None).
    The eligibility comparison always uses plain dates, never these labels,
    because roic.ai's documented label formats contradict each other.
    """
    if not label:
        return None, None
    m = _PERIOD_LABEL.match(str(label).strip())
    if not m:
        return None, None
    year = int(m.group(1)) if m.group(1) else None
    quarter = int(m.group(2)) if m.group(2) else None
    return year, quarter
