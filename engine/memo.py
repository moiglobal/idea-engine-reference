"""Write the one-page memo (Lesson 3).

The memo is built strictly from the numbers the engine computed, which are
saved first to a working data file under data/working/. The language model
may only phrase and interpret those numbers; it must not invent any. The
memo's structure and writing rules live in the idea-memo skill
(.claude/skills/idea-memo/SKILL.md), and the exact prompt sent to the model
lives in prompts/memo-prompt.md, both of which the owner can read and edit
without touching code.

Three things are enforced HERE, in code, after the model has written its
draft, because the guarantee must not depend on the model listening:

  1. The absolute valuation numbers. This section is REBUILT from the working
     data on every memo, not merely checked for. A draft that keeps the
     heading and compresses the figures into a sentence is the failure a
     heading test cannot see, and on a live run in August 2026 the model
     dropped the protected footer while following the rest of the skill, so
     the same thing can happen here.
  2. The closing footer stating this is not a recommendation.
  3. The SEC filing-check outcome, which the course says is never omitted. It
     is the only figure in the memo that does not come from the data vendor,
     and a draft cut short by the token ceiling loses it first, because the
     skill puts it near the end.

A draft that hits the model's token ceiling is discarded rather than patched:
it ends mid-sentence, and half a memo is worse than a plain one.

If the Anthropic call is unavailable or fails, a deterministic fallback memo
is produced from the same working data, so the engine always sends something.
"""

from __future__ import annotations

import json
import re
from datetime import date as _date

from .scoring import factor_table, weakest_factors
from .util import PROJECT_ROOT, WORKING_DIR, write_json

SKILL_PATH = PROJECT_ROOT / ".claude" / "skills" / "idea-memo" / "SKILL.md"
PROMPT_PATH = PROJECT_ROOT / "prompts" / "memo-prompt.md"

FOOTER = ("This is an idea to investigate, not a recommendation. Verify "
          "against primary filings. Reply to this email to refine the engine.")
ABS_HEADING = "Valuation in absolute terms"
# Every filing-check outcome sentence in sec_edgar.py opens with this, in all
# three of its forms, so it is what "the outcome is present" is tested on.
FILING_MARKER = "Independent check:"
MAX_MEMO_TOKENS = 4000
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# A horizontal rule ends a section too. The memo's footer sits under one, so
# without this a section that runs to the bottom of the page would swallow it
# and anything added to that section would land after the footer.
_RULE_RE = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$")


def _fmt_money(x, unit="$"):
    if x is None:
        return "data not available"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "data not available"
    a = abs(x)
    if a >= 1e12:
        return f"{unit}{x / 1e12:.2f}T"
    if a >= 1e9:
        return f"{unit}{x / 1e9:.2f}B"
    if a >= 1e6:
        return f"{unit}{x / 1e6:.1f}M"
    return f"{unit}{x:,.0f}"


def _pct(x, digits=1):
    try:
        return f"{float(x):.{digits}f}%"
    except (TypeError, ValueError):
        return "data not available"


def _x(x, digits=1):
    try:
        return f"{float(x):.{digits}f}x"
    except (TypeError, ValueError):
        return "data not available"


def read_skill() -> str:
    if SKILL_PATH.exists():
        return SKILL_PATH.read_text(encoding="utf-8")
    return ""


def _read_prompt_parts():
    """The system and user templates from prompts/memo-prompt.md."""
    system, template = "", ""
    if PROMPT_PATH.exists():
        text = PROMPT_PATH.read_text(encoding="utf-8")
        if "## System instruction" in text:
            after = text.split("## System instruction", 1)[1]
            system = after.split("## User message template", 1)[0].strip()
            if "## User message template" in after:
                template = after.split("## User message template", 1)[1].strip()
    if not system:
        system = ("You are writing a one-page investment idea memo for a "
                  "long-term value investor. Use only the figures in the DATA "
                  "block; if a figure is missing say 'data not available'. "
                  "Plain English, no em-dashes, and close with: " + FOOTER)
    if not template:
        template = ("MEMO SKILL:\n{SKILL}\n\nINVESTOR STYLE NOTES:\n{CLAUDE_MD}"
                    "\n\nDATA (the only facts you may use):\n{DATA}\n\n"
                    "Write the memo now in Markdown.")
    return system, template


def build_working_file(company: dict, record: dict, filing_check: dict,
                       runners_up=None, eligible_count=None) -> dict:
    """Everything the memo may say, in one file the owner (or a checking
    subagent) can open. Raw statement rows, converted figures, computed
    metrics, currencies and rates, and the filing-check result."""
    a = company.get("absolutes", {})
    fx = company.get("fx") or {}
    working = {
        "generated": _date.today().isoformat(),
        "company": company.get("name"),
        "ticker": company.get("symbol"),
        "exchange": company.get("exchange"),
        "domicile_country": company.get("domicile_country"),
        "listing_country": company.get("listing_country"),
        "industry": company.get("industry"),
        "sector": company.get("sector"),
        "price": company.get("price"),
        "price_date": company.get("price_date"),
        "price_currency": company.get("price_currency_original"),
        "market_cap_usd": company.get("marketCap"),
        "market_cap_asof": company.get("market_cap_asof"),
        "statement_currency": fx.get("statement_currency"),
        "fx": fx,
        "years_of_history": company.get("years"),
        "latest_period": company.get("latest_period"),
        "composite_score": round(company.get("composite", 0), 1),
        "rank_today": company.get("rank"),
        "eligible_count": eligible_count,
        "runners_up": [{"ticker": r.get("symbol"), "name": r.get("name"),
                        "composite": round(r.get("composite", 0), 1)}
                       for r in (runners_up or [])],
        "factor_scores_0_100": {name: score for name, score, _ in factor_table(company)},
        "weakest_factors": [{"factor": n, "score_0_100": round(s * 100, 1)}
                            for n, s in weakest_factors(company)],
        "absolute_valuation": {
            "fcf_to_firm_yield_pct": a.get("fcf_to_firm_yield_pct"),
            "earnings_yield_ebit_ev_pct": a.get("earnings_yield_pct"),
            "ev_to_ebit": a.get("ev_ebit"),
            "price_to_tangible_book": a.get("ptbv"),
            "net_debt_to_ebitda": a.get("net_debt_to_ebitda"),
            "enterprise_value_usd": a.get("enterprise_value"),
            "avg_fcff_5y_usd": a.get("avg_fcff_5y"),
            "tangible_book_usd": a.get("tangible_book"),
        },
        "quality": {
            "roic_multi_year_avg_pct": a.get("roic_pct"),
            "effective_tax_rate_latest": a.get("effective_tax_rate_latest"),
            "operating_margin_pct": a.get("operating_margin_pct"),
            "gross_margin_pct": a.get("gross_margin_pct"),
            "share_count_change_annualized_pct": a.get("share_count_change_pct"),
            "share_count_change_window_years": a.get("share_count_window_years"),
            "share_count_change_total_over_window_pct": a.get("share_count_change_total_pct"),
            "revenue_cagr_pct": a.get("revenue_cagr_pct"),
            "interest_coverage": a.get("interest_coverage"),
        },
        "flags": company.get("flags", []),
        "filing_check": filing_check,
        "business_description": (record.get("description") or "")[:1500],
        "statements_original": record.get("statements_original"),
        "reports_per_year": company.get("reports_per_year"),
    }
    WORKING_DIR.mkdir(parents=True, exist_ok=True)
    path = WORKING_DIR / f"idea-{_date.today().isoformat()}-{company['symbol'].replace(':', '_')}.json"
    write_json(path, working)
    working["_path"] = str(path)
    return working


def build_memo(company: dict, env: dict, working: dict, claude_md: str = "",
               methodology_note=None, notice=None, logger=None) -> str:
    """The memo, via the Anthropic API with a deterministic fallback."""
    data = {k: v for k, v in working.items()
            if k not in ("statements_original", "_path")}
    memo = None
    if env.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # lazy
            client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])
            system, template = _read_prompt_parts()
            user = (template
                    .replace("{SKILL}", read_skill() or "(skill file missing)")
                    .replace("{CLAUDE_MD}", claude_md or "(none provided)")
                    .replace("{DATA}", json.dumps(data, indent=2, default=str)))
            resp = client.messages.create(
                model=env.get("ANTHROPIC_MODEL", "claude-opus-5"),
                max_tokens=MAX_MEMO_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            memo = "".join(getattr(b, "text", "") for b in resp.content).strip()
            # A draft that ran into the ceiling stops mid-sentence, and what
            # it loses is whatever the skill puts last: the data caveats and
            # the filing-check line with them. Throw it away and send the
            # deterministic memo, which is complete, rather than a memo that
            # trails off.
            if getattr(resp, "stop_reason", None) == "max_tokens":
                if logger:
                    logger.warning(
                        f"The memo draft hit the {MAX_MEMO_TOKENS}-token "
                        "ceiling and was cut off mid-sentence, so it was "
                        "discarded and the deterministic memo sent instead. Raise "
                        "MAX_MEMO_TOKENS in engine/memo.py if this repeats.")
                memo = None
        except Exception as exc:
            if logger:
                logger.warning(f"Memo via Anthropic failed ({exc}); using the "
                               "deterministic fallback memo.")
    elif logger:
        logger.info("No ANTHROPIC_API_KEY set; using the deterministic fallback memo.")
    if not memo:
        memo = _fallback_memo(company, working)
    memo = enforce_protected_sections(memo, working, logger=logger)
    filing = working.get("filing_check") or {}
    if filing.get("escalation"):
        memo = f"> **{filing['escalation']}**\n\n" + memo
    return _prepend_note(memo, methodology_note, notice)


def _prepend_note(memo: str, methodology_note, notice=None) -> str:
    """Banners at the top: an applied change offers undo; a warning does not.

    The two are kept apart deliberately. Only a change the reply loop
    actually applied may say "Reply undo to revert"; a warning such as a
    thinning eligible pool changed nothing, so offering an undo for it would
    mislead.
    """
    if notice:
        memo = f"> **Note.** {notice}\n\n" + memo
    if methodology_note:
        memo = (f"> **Methodology update.** {methodology_note} "
                f"Reply \"undo\" to revert.\n\n") + memo
    return memo


def _absolute_valuation_body(working: dict) -> str:
    """The figures of protected section 6, without their heading."""
    v = working.get("absolute_valuation", {})
    lines = []
    lines.append(f"- Free cash flow (firm) yield: {_pct(v.get('fcf_to_firm_yield_pct'))}")
    lines.append(f"- Earnings yield (EBIT/EV): {_pct(v.get('earnings_yield_ebit_ev_pct'))}")
    lines.append(f"- EV / EBIT: {_x(v.get('ev_to_ebit'))}")
    lines.append(f"- Price / tangible book: {_x(v.get('price_to_tangible_book'))}")
    lines.append(f"- Net debt / EBITDA: {_x(v.get('net_debt_to_ebitda'))}")
    lines.append(f"- Enterprise value: {_fmt_money(v.get('enterprise_value_usd'))} "
                 f"(USD, market cap as of {working.get('market_cap_asof') or 'n/a'})")
    lines.append(f"- 5y average free cash flow to the firm: {_fmt_money(v.get('avg_fcff_5y_usd'))} (USD)")
    lines.append("")
    lines.append("These are absolute figures, not percentile ranks. A high "
                 "relative score does not mean cheap in absolute terms.")
    return "\n".join(lines)


def _absolute_valuation_block(working: dict) -> str:
    """The protected section 6, heading and all, built from the data."""
    return f"## {ABS_HEADING}\n\n" + _absolute_valuation_body(working)


def _find_section(memo: str, heading_text: str):
    """Locate a markdown section by a phrase in its heading.

    Returns (lines, start, end) where start is the heading's line index and
    end is the first line of the next heading at the same or a shallower
    level, or None when no heading carries the phrase. Matching is on a
    phrase rather than the whole line so a model that writes
    "## 6. Valuation in absolute terms" is still found.
    """
    lines = memo.splitlines()
    start = level = None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and heading_text.lower() in m.group(2).lower():
            start, level = i, len(m.group(1))
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _RULE_RE.match(lines[j]):
            end = j
            break
        m = _HEADING_RE.match(lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    return lines, start, end


def _replace_section_body(memo: str, heading_text: str, new_body: str):
    """Swap a section's body for new_body, keeping the model's own heading.

    Returns the rewritten memo, or None when the heading is absent.
    """
    found = _find_section(memo, heading_text)
    if found is None:
        return None
    lines, start, end = found
    rebuilt = lines[:start + 1] + [""] + new_body.splitlines() + [""] + lines[end:]
    return "\n".join(rebuilt).rstrip() + "\n"


def _append_to_section(memo: str, heading_text: str, text: str):
    """Add a line to the end of a section. Returns None when it is absent."""
    found = _find_section(memo, heading_text)
    if found is None:
        return None
    lines, _start, end = found
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    rebuilt = lines[:end] + ["", text] + lines[end:]
    return "\n".join(rebuilt).rstrip() + "\n"


def enforce_protected_sections(memo: str, working: dict, logger=None) -> str:
    """Guarantee the three protected things in code, whatever the model wrote.

    Run on every memo, including the deterministic one, where it is a no-op.

    Section 6 is REBUILT rather than checked. Testing that the heading exists
    passes a draft that keeps the heading and drops every figure, which is
    exactly what an email reply asking for a shorter memo invites. The
    absolute numbers are what stop a relative score being read as a verdict,
    so they are written from the working data every time and the model's
    prose under that heading is discarded.

    The footer and the SEC filing-check outcome are appended when absent. The
    filing check is the one figure in the memo that does not come from the
    data vendor, and the course says its outcome is never omitted.
    """
    body = _absolute_valuation_body(working)
    rebuilt = _replace_section_body(memo, ABS_HEADING, body)
    if rebuilt is None:
        if logger:
            logger.warning("Memo draft was missing the absolute valuation "
                           "section; it was rebuilt from the data and appended.")
        memo = memo.rstrip() + "\n\n" + _absolute_valuation_block(working)
    else:
        if logger and body.strip() not in memo:
            logger.warning("Memo draft did not carry the absolute valuation "
                           "figures in full; the section was rebuilt from the "
                           "working data.")
        memo = rebuilt

    caveat = ((working.get("filing_check") or {}).get("caveat_line") or "").strip()
    if caveat and FILING_MARKER.lower() not in memo.lower():
        if logger:
            logger.warning("Memo draft was missing the SEC filing-check "
                           "outcome; it was added. The outcome is never omitted.")
        placed = _append_to_section(memo, "Data caveats", caveat)
        memo = placed if placed is not None else (memo.rstrip() + "\n\n" + caveat)

    if "idea to investigate, not a recommendation" not in memo.lower():
        if logger:
            logger.warning("Memo draft was missing the protected footer; it was appended.")
        memo = memo.rstrip() + "\n\n---\n" + FOOTER
    return memo


def _fallback_memo(company: dict, working: dict) -> str:
    """A complete nine-section memo with no language model involved."""
    v = working.get("absolute_valuation", {})
    q = working.get("quality", {})
    fx = working.get("fx") or {}
    filing = working.get("filing_check") or {}
    lines = []

    # 1. Header
    lines.append(f"# {working.get('company')} ({working.get('ticker')})")
    lines.append("")
    price_bits = "Price data not available"
    if working.get("price") is not None:
        price_bits = (f"Price {working['price']:,.2f} {working.get('price_currency') or ''}"
                      f" on {working.get('price_date') or 'n/a'}").strip()
    lines.append(f"{working.get('exchange') or 'n/a'} | Domicile {working.get('domicile_country') or 'n/a'} | "
                 f"{_date.today().isoformat()} | {price_bits} | "
                 f"Market cap {_fmt_money(working.get('market_cap_usd'))} USD "
                 f"(as of {working.get('market_cap_asof') or 'n/a'})")
    lines.append("")

    # 2. Why today
    lines.append("## Why today")
    lines.append(f"Composite score {working.get('composite_score')} of 100, ranked "
                 f"#{working.get('rank_today')} of {working.get('eligible_count') or 'n/a'} "
                 f"eligible companies. Factor scores (0-100):")
    lines.append("")
    lines.append("| Factor | Score | Weight |")
    lines.append("| --- | ---: | ---: |")
    for name, score, weight in factor_table(company):
        lines.append(f"| {name.replace('_', ' ')} | {score:.1f} | {weight} |")
    lines.append("")

    # 3. Where it falls short
    lines.append("## Where it falls short")
    weak = ", ".join(f"{n.replace('_', ' ')} ({s * 100:.0f}/100)"
                     for n, s in weakest_factors(company))
    lines.append(f"Weakest dimensions: {weak}. Aim your own work here first.")
    lines.append("")

    # 4. The business
    lines.append("## The business")
    desc = (working.get("business_description") or "").strip()
    if desc and len(desc) > 700:
        desc = desc[:700].rsplit(" ", 1)[0].rstrip(",;:") + " ..."
    lines.append(desc or "Business description not available from the data provider.")
    lines.append("")

    # 5. The quantitative case
    lines.append("## The quantitative case")
    lines.append(f"- ROIC (multi-year avg, goodwill included): {_pct(q.get('roic_multi_year_avg_pct'))}")
    lines.append(f"- Operating margin (avg): {_pct(q.get('operating_margin_pct'))}")
    lines.append(f"- Gross margin (avg): {_pct(q.get('gross_margin_pct'))}")
    win = q.get("share_count_change_window_years")
    total = q.get("share_count_change_total_over_window_pct")
    window_txt = f" a year over {win} years" if win else " a year"
    total_txt = f" ({_pct(total)} in total)" if total is not None else ""
    lines.append(f"- Share count change: "
                 f"{_pct(q.get('share_count_change_annualized_pct'))}"
                 f"{window_txt}{total_txt}")
    lines.append(f"- Revenue growth (CAGR): {_pct(q.get('revenue_cagr_pct'))}")
    lines.append(f"- Interest coverage: {_x(q.get('interest_coverage'))}")
    lines.append("")

    # 6. Valuation in absolute terms (protected)
    lines.append(_absolute_valuation_block(working))
    lines.append("")

    # 7. Bear case
    lines.append("## Bear case, and what would have to be true")
    weakest = [n.replace("_", " ") for n, _ in weakest_factors(company)][:2]
    lines.append(f"The numbers flag {' and '.join(weakest) if weakest else 'no single factor'} "
                 "as the weak points; the bear case starts there. This section is "
                 "generated from the figures and common risks, not researched. "
                 "Treat it as prompts for your own thinking, and read the filings "
                 "for the risks the numbers cannot show.")
    lines.append("")

    # 8. Data caveats
    lines.append("## Data caveats")
    lines.append(f"Based on {working.get('years_of_history')} years of annual statements "
                 f"(latest period {working.get('latest_period') or 'n/a'}). Multi-year "
                 "averages (free cash flow, ROIC) use up to the five most recent "
                 "fiscal years.")
    stmt_ccy, price_ccy = fx.get("statement_currency"), fx.get("price_currency")
    rates_used = fx.get("rates_used") or {}
    if stmt_ccy or price_ccy:
        rate_bits = ", ".join(f"{c} at {r:.4f} USD" for c, r in sorted(rates_used.items())
                              if c != "USD")
        line = f"Statements reported in {stmt_ccy or 'n/a'}; price figures in {price_ccy or 'n/a'}."
        if rate_bits:
            line += f" Converted to US dollars at {rate_bits} on {fx.get('rate_date') or 'n/a'}."
        lines.append(line)
    for note in fx.get("notes") or []:
        lines.append(note)
    for flag in working.get("flags") or []:
        lines.append(f"Flag: {flag}.")
    lines.append(filing.get("caveat_line") or
                 "Independent check: outcome missing. Treat this memo as unverified.")
    lines.append("The score is relative to today's universe; a high rank does not "
                 "mean cheap in absolute terms. Non-US data can be thinner; verify "
                 "against primary filings.")
    lines.append("")

    # 9. Footer (protected)
    lines.append("---")
    lines.append(FOOTER)
    return "\n".join(lines)
