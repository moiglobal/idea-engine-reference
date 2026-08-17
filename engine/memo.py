"""Write the one-page memo (Lesson 3).

The memo is built strictly from the numbers the engine computed, which are
saved first to a working data file under data/working/. The language model
may only phrase and interpret those numbers; it must not invent any. The
memo's structure and writing rules live in the idea-memo skill
(.claude/skills/idea-memo/SKILL.md), and the exact prompt sent to the model
lives in prompts/memo-prompt.md, both of which the owner can read and edit
without touching code.

Two sections are protected and are enforced HERE, in code, after the model
has written its draft: the absolute valuation numbers and the closing footer
stating this is not a recommendation. If a draft comes back without either,
the missing section is rebuilt deterministically from the working data and
appended. No instruction, prompt edit, or email reply can remove them,
because the guarantee does not depend on the model listening.

If the Anthropic call is unavailable or fails, a deterministic fallback memo
is produced from the same working data, so the engine always sends something.
"""

from __future__ import annotations

import json
from datetime import date as _date

from .scoring import factor_table, weakest_factors
from .util import PROJECT_ROOT, WORKING_DIR, write_json

SKILL_PATH = PROJECT_ROOT / ".claude" / "skills" / "idea-memo" / "SKILL.md"
PROMPT_PATH = PROJECT_ROOT / "prompts" / "memo-prompt.md"

FOOTER = ("This is an idea to investigate, not a recommendation. Verify "
          "against primary filings. Reply to this email to refine the engine.")
ABS_HEADING = "Valuation in absolute terms"


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
            "share_count_change_pct": a.get("share_count_change_pct"),
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
                max_tokens=2500,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            memo = "".join(getattr(b, "text", "") for b in resp.content).strip()
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


def _absolute_valuation_block(working: dict) -> str:
    """The protected section 6, built deterministically from the data."""
    v = working.get("absolute_valuation", {})
    lines = [f"## {ABS_HEADING}", ""]
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


def enforce_protected_sections(memo: str, working: dict, logger=None) -> str:
    """Guarantee sections 6 and 9 in code, whatever the model produced.

    The two protected sections are what stop a relative score from being read
    as a verdict and a screen output from being read as advice. If the draft
    lacks either, it is rebuilt from the working data and appended, with a
    note in the log. This runs on every memo, including the fallback.
    """
    lower = memo.lower()
    if ABS_HEADING.lower() not in lower:
        if logger:
            logger.warning("Memo draft was missing the absolute valuation "
                           "section; it was rebuilt from the data and appended.")
        memo = memo.rstrip() + "\n\n" + _absolute_valuation_block(working)
    if "idea to investigate, not a recommendation" not in lower:
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
    lines.append(f"- Share count change over the period: {_pct(q.get('share_count_change_pct'))}")
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
