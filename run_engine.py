#!/usr/bin/env python3
"""Idea Engine: the daily run (Lesson 5 orchestrator).

Order of operations:
  1. Read email replies and apply any feedback (skippable with --no-feedback).
  2. Load the day's exchange rates (the single currency step).
  3. Screen the universe in stages (hard filter), reporting a count per stage.
  4. Pull fundamentals, convert to dollars, and score every survivor.
  5. Refresh prices for the survivors via the batch endpoint.
  6. Apply the no-repeat rule and pick the best eligible company.
  7. Run the SEC EDGAR filing check on the pick.
  8. Write the memo, grounded in a saved working data file.
  9. Email it (skipped with --dry-run).
 10. Record history, coverage statistics, timings, and a summary.

If anything fails, it logs the error and emails an alert instead of dying
quietly. A roic.ai 401 (dead key) or 402 (plan no longer covers an endpoint)
is fatal and the alert says which one it was.

Useful commands:
  python run_engine.py --selftest             # offline check, no keys, no network
  python run_engine.py --dry-run --limit 50   # quick live run, writes memo, no email
  python run_engine.py                        # the real daily run
  python run_engine.py --refresh              # ignore caches and refetch
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date as _date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import config, currency, dedup, memo as memo_mod, metrics, roic, scoring, screen
from engine import email_io, sec_edgar
from engine import feedback as feedback_mod
from engine.util import (COVERAGE_LOG, DATA_DIR, OUTPUT_DIR, ensure_dirs,
                         get_logger, read_json, today_str, write_json)

LOW_POOL_THRESHOLD = 20


# ---------------------------------------------------------------------------
# Live daily run
# ---------------------------------------------------------------------------
def run_live(args) -> int:
    logger = get_logger()
    ensure_dirs()
    env = config.load_env()

    need_email = not args.dry_run
    missing = config.missing_settings(env, include_email=need_email)
    if missing:
        logger.error(
            f"Missing or still-unedited settings in .env: {', '.join(missing)}. "
            "Copy .env.example to .env and replace the example values with "
            "your own. A setting left at its example value counts as missing.")
        return 2
    if not env.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY is not set: the memo will use the "
                       "deterministic fallback and reply feedback will be "
                       "saved as notes rather than interpreted.")

    def mailer(subject, body):
        if need_email:
            email_io.send_alert(env, subject, body, logger=logger)
        else:
            logger.info(f"(dry run) Would have emailed: {subject}")

    timings = []

    def timed(label):
        timings.append([label, time.time()])

    def finish_timing():
        for i, entry in enumerate(timings):
            end = timings[i + 1][1] if i + 1 < len(timings) else time.time()
            entry[1] = end - entry[1]

    try:
        universe_cfg = config.load_universe()
        scoring_cfg = config.load_scoring()

        # 1. Feedback
        timed("feedback")
        methodology_note = None
        if not args.no_feedback and env["ENGINE_EMAIL_ADDRESS"]:
            replies = email_io.read_replies(env, logger=logger)
            if replies:
                summaries = feedback_mod.process_replies(replies, env, logger=logger,
                                                         mailer=mailer)
                methodology_note = " ".join(summaries) if summaries else None
                # Configs may have changed; reload them.
                universe_cfg = config.load_universe()
                scoring_cfg = config.load_scoring()

        # 2. The day's exchange rates: the single currency step.
        timed("exchange rates")
        rates = currency.load_rates(env["ROIC_API_KEY"], logger=logger,
                                    force=args.refresh)

        # 2b. The field-existence check from Lesson 2: confirm every roic.ai
        # field the engine relies on still exists in the live specification,
        # so a renamed field surfaces as a message instead of a quiet zero.
        missing_fields = roic.verify_fields(logger=logger,
                                            ttl_days=0 if args.refresh else 7)
        if missing_fields:
            mailer("[Idea Engine] Data field check FAILED",
                   "roic.ai's specification no longer contains these fields "
                   "the engine relies on:\n\n" + "\n".join(missing_fields) +
                   "\n\nAffected metrics will quietly score as neutral until "
                   "the engine is updated. Treat today's memo with suspicion.")

        # 3. Screen, in stages, with counts.
        timed("screen")
        # The limit goes INTO the screen, not onto its result. Nearly all the
        # cost of a run is one enterprise-value request per ticker and one
        # profile per size survivor, so trimming the survivor list afterwards
        # would save nothing and still take the better part of an hour.
        survivors, stage_counts, exclusions = screen.run_screen(
            env["ROIC_API_KEY"], universe_cfg, rates, logger=logger,
            refresh=args.refresh, assume_yes=(args.yes or not sys.stdin.isatty()),
            limit=args.limit)

        # 4. Pull, convert, and score.
        timed("pull and score")
        scored, drop_reasons = [], {}
        fx_dropped_by_ccy = {}
        for i, cand in enumerate(survivors, 1):
            try:
                rec = roic.get_company_record(env["ROIC_API_KEY"], cand, rates,
                                              refresh=args.refresh, logger=logger)
                m = metrics.compute_metrics(rec)
                m["description"] = rec.get("description")
                if m["sanity_ok"]:
                    scored.append(m)
                else:
                    country = m.get("country") or "??"
                    # Group by the machine-readable code, not the human
                    # sentence, so "how many dropped and why" really groups.
                    for code in (m.get("drop_codes") or ["other"]):
                        drop_reasons.setdefault(code, {})
                        drop_reasons[code][country] = drop_reasons[code].get(country, 0) + 1
                    logger.info(f"Dropped {cand['symbol']}: "
                                f"{'; '.join(m['sanity_reasons'])}")
            except currency.MissingRateError as exc:
                fx_dropped_by_ccy[exc.currency] = fx_dropped_by_ccy.get(exc.currency, 0) + 1
                logger.warning(f"Excluded {cand['symbol']}: no USD rate for {exc.currency}")
            except roic.RoicFatalError:
                raise
            except Exception as exc:
                logger.warning(f"Skipped {cand['symbol']}: {exc}")
            if i % 50 == 0:
                logger.info(f"Processed {i}/{len(survivors)} companies "
                            f"({len(scored)} scored so far)")
        logger.info(f"Scored {len(scored)} companies after sanity checks.")
        if drop_reasons:
            for code, by_country in sorted(drop_reasons.items()):
                total = sum(by_country.values())
                by = ", ".join(f"{c}: {n}" for c, n in sorted(by_country.items()))
                logger.info(f"Dropped {total} for {code.replace('_', ' ')} ({by})")
        if fx_dropped_by_ccy:
            detail = ", ".join(f"{c}: {n}" for c, n in sorted(fx_dropped_by_ccy.items()))
            logger.warning(f"Currency exclusions at the scoring stage ({detail}); "
                           "these companies were never compared unconverted.")

        if not scored:
            if need_email:
                email_io.send_alert(env, "[Idea Engine] No companies to score",
                                    "The screen returned nothing scorable today. "
                                    "Check your filters.", logger=logger)
            logger.error("Nothing to score. Stopping.")
            return 1

        # 5. Prices for the survivors, via the batch endpoint (one walk per
        # exchange, pages of up to 2,000, rather than one call per company).
        timed("price refresh")
        try:
            price_map = roic.fetch_latest_prices(
                env["ROIC_API_KEY"],
                [c.get("exchange") for c in scored],
                ttl_days=0 if args.refresh else 1, logger=logger)
        except roic.RoicFatalError:
            raise
        for m in scored:
            p = price_map.get(m["symbol"]) or {}
            # A null price means the share has not traded in 10 days:
            # missing data, never zero.
            m["price"] = p.get("close")
            m["price_date"] = p.get("date")
            if p.get("currency"):
                m["price_currency_original"] = p.get("currency")

        ranked = scoring.score_universe(scored, scoring_cfg)

        # 6. Select under the no-repeat rule.
        timed("select")
        history = dedup.load_history()
        chosen, runners, n_eligible = dedup.select_idea(ranked, history, logger=logger)
        if chosen is None:
            msg = ("Every top name was sent recently and none has reported a "
                   "new period. The engine reports having nothing new rather "
                   "than repeating a name or lowering its standard.")
            logger.warning(msg)
            if need_email:
                email_io.send_alert(env, "[Idea Engine] No new idea today", msg,
                                    logger=logger)
            return 0

        # A thin pool is a warning, not an applied change, so it travels as a
        # separate notice: it must never carry the "reply undo" offer. A
        # sampled run travels the same way, and it matters more, because the
        # memo otherwise reads as the best idea in the market when it is the
        # best idea in a few dozen tickers.
        notices = []
        if args.limit:
            notices.append(
                f"SAMPLE RUN. Started with --limit {args.limit}, so the engine "
                f"stopped screening at {len(scored)} scored companies in ticker "
                "order instead of searching the whole universe. The company "
                "below is the best of that sample, not the best available "
                "idea. Run without --limit for a real one.")
        low_pool = n_eligible < LOW_POOL_THRESHOLD
        if low_pool and not args.limit:
            notices.append(f"Only {n_eligible} eligible companies today. Your "
                           "filter may be too tight; consider loosening it.")
        notice = " ".join(notices) or None
        if notice:
            logger.warning(notice)

        # 7. The filing check, before any memo is emailed.
        timed("filing check")
        chosen_record = roic.get_company_record(env["ROIC_API_KEY"], chosen, rates,
                                                logger=logger)
        filing = sec_edgar.run_filing_check(chosen, chosen_record,
                                            env["SEC_USER_AGENT"], logger=logger)
        logger.info(f"Filing check outcome: {filing['outcome']}")

        # 8. The memo, grounded in a saved working file.
        timed("memo")
        claude_md = config.read_claude_md()
        working = memo_mod.build_working_file(chosen, chosen_record, filing,
                                              runners_up=runners,
                                              eligible_count=n_eligible)
        memo_md = memo_mod.build_memo(chosen, env, working, claude_md=claude_md,
                                      methodology_note=methodology_note,
                                      notice=notice, logger=logger)
        memo_md = _append_friday_coverage(memo_md, drop_reasons, fx_dropped_by_ccy,
                                          exclusions, logger)
        memo_path = OUTPUT_DIR / f"memo-{today_str()}-{chosen['symbol'].replace(':', '_')}.md"
        memo_path.write_text(memo_md, encoding="utf-8")
        logger.info(f"Memo written: {memo_path}")

        # 9. Email.
        subject = f"[Idea Engine] {chosen.get('name')} ({chosen['symbol']})"
        if need_email:
            email_io.send_memo(env, subject, memo_md, logger=logger)
        else:
            logger.info("Dry run: skipped sending email.")

        # 10. History, coverage, and the summary.
        timed("record and summarize")
        if need_email:
            dedup.record_sent(chosen, history)
        else:
            # Nothing was sent, so nothing is recorded as sent. Writing here
            # would quietly retire the company under the no-repeat rule and
            # the reader would never see it in his inbox, with no way to tell
            # why his test run had cost him the idea.
            logger.info(f"Dry run: {chosen['symbol']} was NOT recorded as sent, "
                        "so it stays eligible for a real run.")
        _append_coverage_log(drop_reasons, fx_dropped_by_ccy)
        finish_timing()
        summary = {
            "date": today_str(),
            "chosen": chosen["symbol"],
            "name": chosen.get("name"),
            "composite": round(chosen["composite"], 1),
            "eligible_count": n_eligible,
            "scored_count": len(scored),
            "stage_counts": stage_counts,
            "exclusions": exclusions,
            "filing_check": filing["outcome"],
            "runners_up": [r["symbol"] for r in runners],
            "low_pool_warning": low_pool,
            "cache": dict(roic.CACHE_STATS),
            "timings_seconds": {label: round(t, 1) for label, t in timings},
        }
        write_json(DATA_DIR / "last_run.json", summary)
        print("\n=== Run summary ===")
        for stage, count in stage_counts:
            print(f"  {stage}: {count}")
        print(f"Chosen: {chosen.get('name')} ({chosen['symbol']})  "
              f"composite {summary['composite']}/100")
        print(f"Runners-up: {', '.join(summary['runners_up']) or 'none'}")
        print(f"Eligible today: {n_eligible} | Scored: {len(scored)}")
        print(f"Filing check: {filing['outcome']}")
        print(f"Cache: {roic.CACHE_STATS['hits']} from cache, "
              f"{roic.CACHE_STATS['fetches']} refetched")
        print("Step timings: " + ", ".join(f"{label} {t:.0f}s" for label, t in timings))
        return 0

    except roic.RoicFatalError as exc:
        logger.error(str(exc))
        if need_email:
            email_io.send_alert(env, "[Idea Engine] FATAL: roic.ai access problem",
                                f"{exc}\n\nNo memo can be produced until this is "
                                "fixed.", logger=logger)
        return 1
    except Exception as exc:
        logger.exception(f"Run failed: {exc}")
        if need_email:
            email_io.send_alert(env, "[Idea Engine] Run FAILED",
                                f"The engine failed today:\n\n{exc}\n\nCheck data/run.log.",
                                logger=logger)
        return 1


def _append_coverage_log(drop_reasons: dict, fx_dropped: dict) -> None:
    """One line of coverage statistics per run, for the Friday report."""
    log = read_json(COVERAGE_LOG, default=[])
    log.append({"date": today_str(), "drops": drop_reasons, "fx": fx_dropped})
    write_json(COVERAGE_LOG, log[-30:])  # keep roughly six weeks


def _append_friday_coverage(memo_md: str, drop_reasons, fx_dropped, exclusions,
                            logger) -> str:
    """On Fridays, add the week's coverage report to the memo.

    Shows how many companies were dropped this week for missing cash flow
    data or failed currency conversion, by country, so a quiet degradation in
    non-US coverage is seen rather than suffered.
    """
    if _date.today().weekday() != 4:  # Friday
        return memo_md
    log = read_json(COVERAGE_LOG, default=[])
    recent = []
    for e in log:
        try:
            if (_date.today() - _date.fromisoformat(e.get("date", ""))).days <= 7:
                recent.append(e)
        except ValueError:
            continue
    lines = ["", "## Weekly coverage report", ""]
    drops_total: dict = {}
    fx_total: dict = {}
    for e in recent:
        for reason, by_country in (e.get("drops") or {}).items():
            for country, n in by_country.items():
                key = (reason, country)
                drops_total[key] = drops_total.get(key, 0) + n
        for ccy, n in (e.get("fx") or {}).items():
            fx_total[ccy] = fx_total.get(ccy, 0) + n
    if not drops_total and not fx_total:
        lines.append("No companies were dropped this week for missing cash flow "
                     "data or failed currency conversion.")
    else:
        for (reason, country), n in sorted(drops_total.items()):
            lines.append(f"- {country}: {n} dropped for {str(reason).replace('_', ' ')}")
        for ccy, n in sorted(fx_total.items()):
            lines.append(f"- {n} excluded because no USD rate was available for {ccy}")
        lines.append("")
        lines.append("This is the honest map of where the engine can and cannot "
                     "see. If non-US coverage is degrading, it shows up here first.")
    if logger:
        logger.info("Friday coverage report appended to the memo.")
    return memo_md + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Offline self-test (no keys, no network)
# ---------------------------------------------------------------------------
FAKE_RATE_ROWS = [
    {"symbol": "FX:EURUSD", "close": 1.10, "date": "2026-08-10"},
    {"symbol": "FX:USDJPY", "close": 160.0, "date": "2026-08-10"},
    {"symbol": "FX:GBPUSD", "close": 1.35, "date": "2026-08-10"},
]


def _synth(symbol, name, *, rev0, growth, op_margin, gross_margin, shares0,
           share_drift, debt, cash, equity, capex_ratio, market_cap, price,
           ocf_mult=1.1, industry="Test industry",
           stmt_ccy="USD", price_ccy="USD"):
    """A fictional company in roic.ai's field shapes, for the self-test."""
    S = 1_000_000  # express inputs in millions
    income, balance, cashflow = [], [], []
    for i in range(6):  # i = 0 is newest
        exp = 5 - i
        rev = rev0 * ((1 + growth) ** exp) * S
        ebit = rev * op_margin
        pretax = ebit * 0.95
        shares = shares0 * ((1 + share_drift) ** exp) * S
        fy = 2025 - i
        income.append({
            "fiscal_year": fy, "period_end_date": f"{fy}-12-31", "currency": stmt_ccy,
            "is_sales_revenue_turnover": rev,
            "is_gross_profit": rev * gross_margin,
            "is_oper_income": ebit,
            "is_pretax_income": pretax,
            "is_inc_tax_exp": pretax * 0.21,
            "is_int_expense": debt * S * 0.05,
            "is_net_income": ebit * 0.75,
            "is_sh_for_diluted_eps": shares,
        })
        balance.append({
            "fiscal_year": fy, "period_end_date": f"{fy}-12-31", "currency": stmt_ccy,
            "bs_st_borrow": debt * S * 0.2, "bs_lt_borrow": debt * S * 0.8,
            "bs_cash_near_cash_item": cash * S,
            "bs_total_equity": equity * S,
            "bs_goodwill": equity * S * 0.10,
            "bs_disclosed_intangibles": equity * S * 0.05,
            "bs_tot_asset": (equity + debt) * S + rev * 0.3,
            "bs_sh_out": shares,
        })
        cashflow.append({
            "fiscal_year": fy, "period_end_date": f"{fy}-12-31", "currency": stmt_ccy,
            "cf_cash_from_oper": ebit * ocf_mult,
            "cf_cap_expenditures": -rev * capex_ratio,  # negative, as vendors often report
            "cf_depr_amort": ebit * 0.2,
        })
    return {
        "symbol": symbol, "name": name, "exchange": "TEST",
        "domicile_country": "US", "listing_country": "US",
        "sector": "Test sector", "industry": industry,
        "description": f"{name} is a fictional company used to test the scoring engine.",
        "cik": None, "reports_per_year": 4,
        "current_period_end": "2025-12-31", "last_release_date": "2026-02-15",
        "ev_row": {"market_cap": market_cap * S, "price_currency": price_ccy,
                   "currency": stmt_ccy, "period_end_date": "2025-12-31",
                   "enterprise_value": None, "short_and_long_term_debt": debt * S},
        "income": income, "balance": balance, "cashflow": cashflow,
    }


def run_selftest() -> int:
    print("Running offline self-test (no API keys, no network)...\n")
    checks = 0

    def ok(condition, label):
        nonlocal checks
        assert condition, f"FAILED: {label}"
        checks += 1
        print(f"  ok  {label}")

    # --- 1. Currency conversion, the highest-risk part of the port ----------
    rates = currency.build_rates(FAKE_RATE_ROWS, "2026-08-10")
    usd, rate, note = currency.to_usd(1000.0, "JPY", rates)
    ok(abs(usd - 6.25) < 1e-9, "JPY converts through the inverted USDJPY pair (1000 JPY -> $6.25)")
    usd, rate, note = currency.to_usd(500.0, "GBX", rates)
    ok(abs(usd - 6.75) < 1e-9 and note, "a per-share GBX price converts via GBP at 1/100, with a note")
    usd, rate, note = currency.amount_to_usd(500.0, "GBX", rates)
    ok(abs(usd - 675.0) < 1e-9 and note,
       "an aggregate amount labeled GBX converts at the GBP rate (roic.ai "
       "states aggregates in the major unit; verified against price x shares)")
    try:
        currency.to_usd(100.0, "NOK", rates)
        ok(False, "missing rate must raise")
    except currency.MissingRateError as exc:
        ok(exc.currency == "NOK", "a missing NOK rate excludes rather than comparing unconverted")

    # --- 2. The two locked definitions, on hand-computed fixtures -----------
    row = {"ocf": 110.0, "capex": -30.0, "interest": 8.0, "pretax": 100.0,
           "tax": 21.0, "ebit": 90.0, "equity": 300.0, "debt": 200.0, "cash": 100.0}
    flags = set()
    fcff = metrics._fcff_for_row(row, flags)
    ok(abs(fcff - (110.0 + 8.0 * 0.79 - 30.0)) < 1e-9,
       "FCFF = OCF + after-tax interest - capex (110 + 6.32 - 30 = 86.32)")
    roic_val = metrics._roic_for_row({**row, "tax": 50.0})
    ok(abs(roic_val - (90.0 * 0.60) / 400.0) < 1e-9,
       "ROIC caps a 50% effective tax rate to 40% (NOPAT 54 / invested 400)")
    ok(metrics._roic_for_row({**row, "pretax": -10.0}) is None,
       "a loss year contributes no ROIC observation (no statutory guess)")

    # --- 3. The no-repeat rule -----------------------------------------------
    hist = {}
    comp = {"symbol": "TST", "current_period_end": "2026-06-30",
            "last_release_date": "2026-07-30", "latest_period": "2025-12-31"}
    ok(dedup.is_eligible(comp, hist), "a never-sent company is eligible")
    dedup_entry = {"last_sent": "2026-08-01", "current_period_end": "2026-06-30",
                   "last_release_date": "2026-07-30"}
    ok(not dedup.is_eligible(comp, {"TST": dedup_entry}),
       "no new period since the last send means not eligible")
    newer = {**comp, "current_period_end": "2026-09-30"}
    ok(dedup.is_eligible(newer, {"TST": dedup_entry}),
       "a newer current_period_end makes it eligible again")
    no_cal = {"symbol": "TST", "current_period_end": None, "last_release_date": None,
              "latest_period": "2026-06-30"}
    ok(dedup._dates_from_company(no_cal)[2] == "newest_statement",
       "a profile with no reporting calendar falls back to the newest statement date")
    # A company tracked through the fallback must become eligible again when
    # newer statements appear; it is never banished for lacking a calendar.
    # The history file is pointed at a throwaway path for this check so the
    # self-test can never touch the engine's real no-repeat memory.
    import tempfile
    from pathlib import Path as _Path
    real_history_file = dedup.HISTORY_FILE
    try:
        dedup.HISTORY_FILE = _Path(tempfile.gettempdir()) / "selftest-history.json"
        fallback_hist = {}
        old_fallback = {"symbol": "FBK", "current_period_end": None,
                        "last_release_date": None, "latest_period": "2025-12-31"}
        dedup.record_sent(old_fallback, fallback_hist)
        year_later = {**old_fallback, "latest_period": "2026-12-31"}
        ok(dedup.is_eligible(year_later, fallback_hist),
           "a fallback-tracked company with new statements is eligible again")
        ok(not dedup.is_eligible(old_fallback, fallback_hist),
           "the same company without new statements stays ineligible")
    finally:
        dedup.HISTORY_FILE = real_history_file

    # --- 3b. The share-count check tells splits from data errors -------------
    split_rec = _synth("SPLT", "Split Co", rev0=800, growth=0.05, op_margin=0.20,
                       gross_margin=0.50, shares0=100, share_drift=0.0, debt=100,
                       cash=200, equity=500, capex_ratio=0.05, market_cap=2000, price=20)
    for row in split_rec["income"]:
        if row["fiscal_year"] >= 2023:  # a 2:1 split doubled the count
            row["is_sh_for_diluted_eps"] *= 2
    for row in split_rec["balance"]:
        if row["fiscal_year"] >= 2023:
            row["bs_sh_out"] *= 2
    unexplained = currency.convert_record(json.loads(json.dumps(split_rec)), rates)
    m_bad = metrics.compute_metrics(unexplained)
    ok(not m_bad["sanity_ok"] and "share_count_discontinuity" in m_bad["drop_codes"],
       "a doubled share count with NO split on record is dropped as a discontinuity")
    split_rec["splits"] = [{"execution_date": "2023-06-15", "split_from": 1,
                            "split_to": 2, "factor": 2.0}]
    explained = currency.convert_record(split_rec, rates)
    m_ok = metrics.compute_metrics(explained)
    ok(m_ok["sanity_ok"] and any("split" in f for f in m_ok["flags"]),
       "the same move WITH a recorded stock split is kept and flagged, not dropped")

    # --- 4. The filing-check comparator, on canned filings -------------------
    canned = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"start": "2025-01-01", "end": "2025-12-31", "form": "10-K", "fp": "FY",
         "fy": 2025, "val": 1000.0}]}}}}}
    vals = {"revenue": 1005.0, "operating_cash_flow": None, "capital_expenditure": None}
    res = sec_edgar.evaluate_against_facts(vals, canned, "2025-12-31", "USD")
    ok(res["outcome"] == "agree", "a 0.5% revenue gap counts as agreement")
    res = sec_edgar.evaluate_against_facts({**vals, "revenue": 1100.0}, canned,
                                           "2025-12-31", "USD")
    ok(res["outcome"] == "disagree" and res["escalation"],
       "a 9% gap disagrees AND escalates to the top of the memo")
    res = sec_edgar.run_filing_check({"cik": None}, {}, "test agent")
    ok(res["outcome"] == "no_check" and "does not file" in res["caveat_line"],
       "no CIK reports the third outcome, never silence")

    # --- 5. Protected sections are enforced in code --------------------------
    bare = "# Some Memo\n\nA draft that lost its guardrails."
    working_min = {"absolute_valuation": {}, "market_cap_asof": None}
    enforced = memo_mod.enforce_protected_sections(bare, working_min)
    ok("Valuation in absolute terms" in enforced,
       "a memo missing the absolute valuation section gets it restored")
    ok("idea to investigate, not a recommendation" in enforced,
       "a memo missing the footer gets it restored")

    # The failure a heading test cannot see: the model keeps the heading,
    # numbers its sections its own way, and compresses the figures into a
    # sentence. Rebuilding rather than checking is what makes this safe.
    working_full = {
        "absolute_valuation": {
            "fcf_to_firm_yield_pct": 9.2, "earnings_yield_ebit_ev_pct": 11.5,
            "ev_to_ebit": 8.7, "price_to_tangible_book": 5.9,
            "net_debt_to_ebitda": -0.5, "enterprise_value_usd": 2.8e9,
            "avg_fcff_5y_usd": 2.565e8},
        "market_cap_asof": "2025-12-31",
        "filing_check": {"caveat_line": "Independent check: revenue and cash "
                                        "flow agree with the SEC filing within "
                                        "1 percent."},
    }
    shortened = ("# Alpha Co\n\n## 6. Valuation in absolute terms\n\n"
                 "Cheap on the numbers.\n\n## 7. Bear case\n\nRates.\n\n"
                 "## 8. Data caveats\n\nSix years of statements.\n\n---\n"
                 "This is an idea to investigate, not a recommendation.\n")
    rebuilt = memo_mod.enforce_protected_sections(shortened, working_full)
    ok(all(label in rebuilt for label in
           ("Free cash flow (firm) yield", "Earnings yield (EBIT/EV)",
            "EV / EBIT", "Price / tangible book", "Net debt / EBITDA",
            "Enterprise value", "5y average free cash flow")),
       "a draft that keeps the heading but drops the figures gets all seven back")
    ok("Cheap on the numbers." not in rebuilt,
       "the model's prose under the protected heading is replaced, not appended to")
    ok("## 7. Bear case" in rebuilt and "## 6. Valuation" in rebuilt,
       "rebuilding section 6 leaves the surrounding sections and numbering alone")

    # The filing-check outcome is the one figure that does not come from the
    # data vendor, and the course says it is never omitted.
    no_filing = ("# Alpha Co\n\n## Data caveats\n\nSix years.\n\n---\n"
                 "This is an idea to investigate, not a recommendation.\n")
    with_filing = memo_mod.enforce_protected_sections(no_filing, working_full)
    ok("Independent check:" in with_filing,
       "a memo missing the SEC filing-check outcome gets it added")
    ok(with_filing.index("Independent check:") < with_filing.index("---"),
       "the filing-check outcome lands inside the data caveats, not after the footer")
    ok(memo_mod.enforce_protected_sections(with_filing, working_full).count(
           "Independent check:") == 1,
       "enforcing twice does not duplicate the filing-check outcome")

    # --- 5c. Share count is annualized, so history length cannot drive it ---
    # The regression this guards: measuring the raw move from the oldest row
    # available made an identical buyback look more than twice as good on six
    # years of data as on three, and the percentile ranking compared the two
    # directly. roic.ai's history depth varies by domicile, so the old metric
    # quietly paid companies for being American.
    def _buyback(n_years, drift):
        rec = _synth(f"BB{n_years}", f"BB{n_years}", rev0=1000, growth=0.08,
                     op_margin=0.25, gross_margin=0.55, shares0=100,
                     share_drift=drift, debt=500, cash=200, equity=2000,
                     capex_ratio=0.04, market_cap=3000, price=30)
        for kind in ("income", "balance", "cashflow"):
            rec[kind] = rec[kind][:n_years]
        return metrics.compute_metrics(currency.convert_record(rec, rates))

    short_hist, long_hist = _buyback(3, -0.03), _buyback(6, -0.03)
    ok(abs(short_hist["metrics"]["share_count_change"]
           - long_hist["metrics"]["share_count_change"]) < 1e-6,
       "the same buyback rate scores the same on three and on six years of history")
    ok(abs(short_hist["metrics"]["share_count_change"] + 0.03) < 1e-6,
       "a 3% annual buyback reads as -3% a year, not as its cumulative total")
    ok(_buyback(3, -0.05)["metrics"]["share_count_change"]
       < _buyback(6, -0.02)["metrics"]["share_count_change"],
       "the faster buyer ranks ahead of the slower one whatever their histories")
    ok(short_hist["absolutes"]["share_count_window_years"] == 2
       and long_hist["absolutes"]["share_count_window_years"] == 5,
       "the memo can name the window, because the window is recorded")

    # --- 6. The full pipeline on six fictional companies ---------------------
    records = [
        _synth("QVCO", "Quality Value Co", rev0=800, growth=0.10, op_margin=0.25,
               gross_margin=0.55, shares0=100, share_drift=-0.03, debt=100, cash=300,
               equity=600, capex_ratio=0.04, market_cap=3000, price=30),
        _synth("CMPD", "Compounder Inc", rev0=1000, growth=0.12, op_margin=0.28,
               gross_margin=0.60, shares0=150, share_drift=-0.01, debt=200, cash=400,
               equity=900, capex_ratio=0.05, market_cap=30000, price=200),
        _synth("DEEP", "Deep Value Corp", rev0=900, growth=0.0, op_margin=0.08,
               gross_margin=0.25, shares0=120, share_drift=0.0, debt=200, cash=50,
               equity=500, capex_ratio=0.05, market_cap=600, price=5),
        _synth("AVRG", "Average Industries", rev0=1000, growth=0.05, op_margin=0.15,
               gross_margin=0.40, shares0=150, share_drift=0.0, debt=400, cash=100,
               equity=700, capex_ratio=0.06, market_cap=6000, price=40,
               stmt_ccy="EUR", price_ccy="USD"),  # books in euros, shares in dollars
        _synth("LEVR", "Leveraged Ltd", rev0=1200, growth=0.03, op_margin=0.12,
               gross_margin=0.30, shares0=200, share_drift=0.03, debt=2500, cash=80,
               equity=400, capex_ratio=0.07, market_cap=4000, price=20),
        _synth("JUNK", "Expensive Junk Co", rev0=1000, growth=0.01, op_margin=0.05,
               gross_margin=0.20, shares0=200, share_drift=0.04, debt=2000, cash=50,
               equity=300, capex_ratio=0.09, market_cap=20000, price=100),
    ]
    scored = []
    for r in records:
        r = currency.convert_record(r, rates)   # the real conversion path
        m = metrics.compute_metrics(r)
        m["description"] = r["description"]
        if m["sanity_ok"]:
            scored.append(m)
        else:
            print(f"  {r['symbol']} failed sanity: {m['sanity_reasons']}")
    avrg = next(c for c in scored if c["symbol"] == "AVRG")
    ok(avrg["currency_mismatch"] and any("differs" in f for f in avrg["flags"]),
       "a euro-books, dollar-shares company is converted and flagged, not trusted")

    scoring_cfg = config.load_scoring()
    ranked = scoring.score_universe(scored, scoring_cfg)

    print(f"\n{'Rank':<5}{'Ticker':<7}{'Composite':<11}{'Valuation':<11}{'Returns':<10}{'BalSheet':<10}")
    for c in ranked:
        f = c["factors"]
        print(f"{c['rank']:<5}{c['symbol']:<7}{c['composite']:<11.1f}"
              f"{f['valuation']['score'] * 100:<11.1f}{f['returns_on_capital']['score'] * 100:<10.1f}"
              f"{f['balance_sheet_strength']['score'] * 100:<10.1f}")

    winner = ranked[0]["symbol"]
    print(f"\nTop idea: {ranked[0]['name']} ({winner}), composite {ranked[0]['composite']:.1f}/100")
    assert winner == "QVCO", f"Expected QVCO to win, got {winner}"
    print("PASS: the cheapest, highest-quality company ranked first.\n")

    # --- 7. A full fallback memo, protected sections and filing line included -
    print("--- Sample memo (deterministic fallback, no API key needed) ---\n")
    chosen = ranked[0]
    chosen_record = next(r for r in records if r["symbol"] == "QVCO")
    filing = sec_edgar.run_filing_check(chosen, chosen_record, "selftest agent")
    working = memo_mod.build_working_file(chosen, chosen_record, filing,
                                          runners_up=ranked[1:5],
                                          eligible_count=len(ranked))
    memo_md = memo_mod.build_memo(chosen, env={}, working=working,
                                  claude_md="Value investor. Plain English.")
    print(memo_md)
    # Assert on the figures themselves, not on the heading above them. A
    # heading test passes a memo that has lost every number under it.
    for label in ("Free cash flow (firm) yield", "Earnings yield (EBIT/EV)",
                  "EV / EBIT", "Price / tangible book", "Net debt / EBITDA",
                  "Enterprise value", "5y average free cash flow"):
        assert label in memo_md, f"protected valuation figure missing: {label}"
    assert "idea to investigate, not a recommendation" in memo_md
    assert "Independent check" in memo_md
    print(f"\nSelf-test complete: {checks} checks passed, ranking verified, "
          "memo carries every protected valuation figure, the footer and the "
          "filing-check line.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily value-investing idea engine.")
    parser.add_argument("--selftest", action="store_true", help="offline check, no keys needed")
    parser.add_argument("--dry-run", action="store_true", help="run live but do not send email")
    parser.add_argument("--no-feedback", action="store_true", help="skip reading email replies")
    parser.add_argument("--limit", type=int, default=0, help="score at most N survivors (quick tests)")
    parser.add_argument("--refresh", action="store_true", help="ignore the cache and refetch")
    parser.add_argument("--yes", action="store_true",
                        help="skip the request-count confirmation on big first runs")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
