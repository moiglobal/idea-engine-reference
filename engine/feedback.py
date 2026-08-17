"""Turn a plain-English email reply into a configuration change (Lesson 4).

A reply is interpreted into ONE constrained change, applied to one of four
places: config/universe.yaml (the hard filter), config/scoring.yaml (the
weights), the idea-memo skill (how the memo is written), or CLAUDE.md (a
standing judgment note). Every file about to change is backed up first, every
change is logged and announced in the next memo, and replying "undo" restores
the most recent backup of all four.

Safety rules, all from Lesson 4:

  A judgment ("stop showing me heavily indebted companies") must become a
  concrete filter or weight change with the chosen number stated, because the
  score is arithmetic and does not read prose notes. A note that changes
  nothing about ranking is the failure mode to watch for; when a note is all
  that can be done, the change record says so plainly.

  An ambiguous reply, or one needing a code change, applies NOTHING. The
  engine emails back what it understood and what it needs clarified.

  Before a tightening change is applied, it is counted against the cached
  universe on disk (a free, local count, not a fresh run). If fewer than 20
  companies would survive, the change is NOT applied and the count is emailed
  instead.

  An industry exclusion is checked against roic.ai's real label catalogue and
  against the current universe. A label that matches nothing is refused, with
  the closest real labels suggested, because a filter that silently matches
  nothing looks like it works and does not.

  Two parts of the memo are off limits whatever a reply asks: the absolute
  valuation section and the closing "not a recommendation" footer. A
  memo-format request that would remove or shorten either is applied WITHOUT
  that part, the two sections stay, and the change note says so. (Belt and
  suspenders: memo.py also enforces both sections in code on every memo.)

Note: auto-editing YAML rewrites the file and does not preserve its comments.
The pre-change copy in config/backups keeps the commented original recoverable.
"""

from __future__ import annotations

import difflib
import json
import shutil
from datetime import datetime

import yaml

from .util import (BACKUP_DIR, CONFIG_DIR, FEEDBACK_LOG, PROJECT_ROOT,
                   REFERENCE_DIR, read_json, today_str)

UNIVERSE = CONFIG_DIR / "universe.yaml"
SCORING = CONFIG_DIR / "scoring.yaml"
CLAUDE_MD = PROJECT_ROOT / "CLAUDE.md"
MEMO_SKILL = PROJECT_ROOT / ".claude" / "skills" / "idea-memo" / "SKILL.md"
SURVIVORS_FILE = PROJECT_ROOT / "data" / "universe-survivors.json"

MIN_SURVIVORS = 20  # a tightening change must leave at least this many

FACTOR_KEYS = [
    "valuation", "returns_on_capital", "margin_quality_and_stability",
    "capital_discipline_consistency", "balance_sheet_strength", "growth_and_reinvestment",
]

INTERPRET_SYSTEM = """You convert an investor's plain-English reply about an investment screen into
structured changes. Respond with a single JSON object and nothing else:

{
  "changes": [one to three change objects, in the order they should apply]
}

Each change object:
{
  "change_type": one of ["exclude_industry","exclude_keyword","set_market_cap_min",
                          "set_market_cap_max","add_country","exclude_country",
                          "set_weight","edit_memo_format","note","unclear","none"],
  "value": string or number appropriate to the change, or null,
  "factor": one of [valuation, returns_on_capital, margin_quality_and_stability,
                    capital_discipline_consistency, balance_sheet_strength,
                    growth_and_reinvestment] for set_weight only, else null,
  "human_summary": "one plain sentence describing the change and the number chosen",
  "clarification_needed": "for change_type unclear: what you understood and what
                           you need the investor to specify, else null"
}

A reply like "raise valuation to 45 and cut growth to zero" is TWO set_weight
changes. A reply making one request is a single-element list.

Rules:
- A judgment (for example "avoid heavy debt") must become a CONCRETE change:
  pick a filter or weight, choose a number, and say in human_summary that the
  number is your suggestion for the investor to correct.
- For exclude_industry, value MUST be one of the exact roic.ai industry labels
  from the list provided in the user message. If none fits, use exclude_keyword
  with a lowercase substring instead, and say so.
- For edit_memo_format, value is the format instruction to record. If the reply
  asks to remove or shorten the absolute valuation numbers or the closing
  "not a recommendation" footer, DROP that part from value (those sections are
  protected), keep the rest, and note the refusal in human_summary.
- Market caps are in whole US dollars. For set_weight, value is the new weight
  (0-95); the other weights rescale automatically.
- If the reply is ambiguous or would need a code change, use "unclear" and fill
  clarification_needed. If there is no actionable request, use "none"."""


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _dump(path, data):
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def backup_configs() -> str:
    """Copy every file the reply loop may touch into a timestamped folder."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for src in (UNIVERSE, SCORING, CLAUDE_MD, MEMO_SKILL):
        if src.exists():
            shutil.copy2(src, dest / src.name)
    return stamp


def undo_last() -> str:
    backups = sorted([d for d in BACKUP_DIR.glob("*") if d.is_dir()])
    if not backups:
        return "Nothing to undo (no backups found)."
    latest = backups[-1]
    restored = []
    for name, target in (("universe.yaml", UNIVERSE), ("scoring.yaml", SCORING),
                         ("CLAUDE.md", CLAUDE_MD), ("SKILL.md", MEMO_SKILL)):
        src = latest / name
        if src.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            restored.append(name)
    return (f"Reverted {', '.join(restored)} to the configuration backed up at "
            f"{latest.name}.")


def _industry_catalogue() -> list:
    rows = read_json(REFERENCE_DIR / "industries.json", default=[])
    return [r.get("name") for r in rows if isinstance(r, dict) and r.get("name")]


def interpret(reply_text: str, env: dict, logger=None) -> list:
    """Map a reply to a list of structured changes using the Anthropic API.

    A reply can carry more than one request ("raise valuation to 45 and cut
    growth to zero" is two weight changes), so this returns a list, applied
    in order. The real industry labels are included in the prompt so an
    exclusion lands on an exact roic.ai string rather than on the investor's
    word for it. Without an API key the reply is saved as a note for review;
    nothing is guessed.
    """
    labels = _industry_catalogue()
    try:
        import anthropic  # lazy
        client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])
        user = (f"The valid roic.ai industry labels are:\n{json.dumps(labels)}\n\n"
                f"The investor replied:\n{reply_text}")
        resp = client.messages.create(
            model=env.get("ANTHROPIC_MODEL", "claude-opus-5"),
            max_tokens=800,
            system=INTERPRET_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        raw = "".join(getattr(b, "text", "") for b in resp.content).strip()
        start, end = raw.find("{"), raw.rfind("}")
        parsed = json.loads(raw[start:end + 1])
        changes = parsed.get("changes")
        if isinstance(changes, list) and changes:
            return changes[:3]
        return [parsed]  # tolerate a model that returned one bare change
    except Exception as exc:
        if logger:
            logger.warning(f"Could not interpret feedback via the API ({exc}); "
                           "saving your words as a note for review.")
        return [{"change_type": "note", "value": reply_text, "factor": None,
                 "human_summary": ("Saved your message as a note for review; it "
                                   "changes how memos are written but does NOT "
                                   "change how companies are ranked.")}]


# --- The over-tightening guard ----------------------------------------------
def _survivors_after(change: dict, universe_cfg: dict) -> int | None:
    """Count survivors under the proposed change, against the cached universe.

    A local count against data already on disk; costs nothing and requires no
    network. Returns None when there is no cached universe to count against
    (first run), in which case the guard cannot fire.
    """
    blob = read_json(SURVIVORS_FILE, default=None)
    survivors = (blob or {}).get("survivors") or []
    if not survivors:
        return None
    ctype, value = change.get("change_type"), change.get("value")
    country_cfg = universe_cfg.get("country") or {}
    basis = (country_cfg.get("basis") or "domicile").lower()
    country_key = "domicile_country" if basis == "domicile" else "listing_country"

    def keep(s):
        if ctype == "exclude_industry":
            return (s.get("industry") or "") != str(value)
        if ctype == "exclude_keyword":
            hay = f"{s.get('name', '')} {s.get('industry', '')}".lower()
            return str(value).lower() not in hay
        if ctype == "set_market_cap_min":
            cap = s.get("market_cap_usd")
            return cap is not None and cap >= float(value)
        if ctype == "set_market_cap_max":
            cap = s.get("market_cap_usd")
            return cap is not None and cap <= float(value)
        if ctype == "exclude_country":
            return (s.get(country_key) or "").upper() != str(value).upper()
        if ctype == "add_country" and not (country_cfg.get("include") or []):
            # Adding a country to an EMPTY include list narrows "the whole
            # world" down to one country, which is a tightening move.
            return (s.get(country_key) or "").upper() == str(value).upper()
        return True

    return sum(1 for s in survivors if keep(s))


def _is_tightening(change: dict, universe_cfg: dict) -> bool:
    ctype = change.get("change_type")
    if ctype in ("exclude_industry", "exclude_keyword", "set_market_cap_min",
                 "set_market_cap_max", "exclude_country"):
        return True
    if ctype == "add_country":
        # Widens an existing include list, but NARROWS an empty one.
        return not ((universe_cfg.get("country") or {}).get("include") or [])
    return False


def _set_weight(factor: str, new_weight) -> bool:
    if factor not in FACTOR_KEYS:
        return False
    scoring = _load(SCORING)
    factors = scoring.get("factors", {})
    if factor not in factors:
        return False
    new_weight = max(0.0, min(95.0, float(new_weight)))
    old_weight = float(factors[factor].get("weight", 0))
    others_total = 100.0 - old_weight
    remaining = 100.0 - new_weight
    scale = (remaining / others_total) if others_total > 0 else 0.0
    for name, fac in factors.items():
        if name == factor:
            fac["weight"] = round(new_weight, 2)
        else:
            fac["weight"] = round(float(fac.get("weight", 0)) * scale, 2)
    drift = 100.0 - sum(float(f["weight"]) for f in factors.values())
    factors[factor]["weight"] = round(factors[factor]["weight"] + drift, 2)
    _dump(SCORING, scoring)
    return True


def _append_note(text: str) -> None:
    header = "\n\n## Investor refinements (auto-added)\n"
    existing = CLAUDE_MD.read_text(encoding="utf-8") if CLAUDE_MD.exists() else "# CLAUDE.md\n"
    if "## Investor refinements (auto-added)" not in existing:
        existing += header
    existing += f"- ({today_str()}) {text}\n"
    CLAUDE_MD.write_text(existing, encoding="utf-8")


def _append_memo_format(instruction: str) -> str:
    """Record a memo-format adjustment in the skill file, append-only.

    Adjustments are appended under the skill's "Format adjustments" section,
    dated, so the format's history stays readable. The protected-sections
    clause higher in the file outranks anything appended here, and memo.py
    enforces the two protected sections in code regardless.
    """
    text = MEMO_SKILL.read_text(encoding="utf-8") if MEMO_SKILL.exists() else ""
    if "## Format adjustments" not in text:
        text += "\n\n## Format adjustments (from email replies)\n"
    text = text.rstrip() + f"\n- ({today_str()}) {instruction}\n"
    MEMO_SKILL.parent.mkdir(parents=True, exist_ok=True)
    MEMO_SKILL.write_text(text, encoding="utf-8")
    return "Memo format updated; the protected valuation section and footer stay."


def apply_change(change: dict, env: dict, logger=None, mailer=None) -> str:
    """Apply one interpreted change. Returns the human-readable change note.

    mailer(subject, body) is called instead of applying anything when the
    reply was unclear or a guard fired; in the daily run this sends an email
    back to the investor.
    """
    ctype = change.get("change_type", "none")
    value = change.get("value")
    summary = change.get("human_summary") or ctype

    if ctype == "none":
        return "No actionable change found in your reply."

    if ctype == "unclear":
        detail = change.get("clarification_needed") or "It was not clear what to change."
        if mailer:
            mailer("[Idea Engine] Your reply needs clarification",
                   f"Nothing was changed. {detail}\n\nReply again with the detail "
                   "and it will be applied on the next run.")
        return f"Applied nothing: {detail}"

    if ctype == "note":
        _append_note(str(value))
        return summary

    if ctype == "edit_memo_format":
        note = _append_memo_format(str(value))
        return f"{summary} ({note})"

    if ctype == "set_weight":
        ok = _set_weight(change.get("factor"), value)
        return summary if ok else f"Could not set that weight; saved as a note instead. ({summary})"

    universe = _load(UNIVERSE)

    # Guard 1: an industry label must be real and must match something.
    if ctype == "exclude_industry":
        labels = _industry_catalogue()
        blob = read_json(SURVIVORS_FILE, default=None) or {}
        in_universe = {s.get("industry") for s in blob.get("survivors") or []}
        if labels and str(value) not in labels:
            close = difflib.get_close_matches(str(value), labels, n=3, cutoff=0.4)
            msg = (f"{value!r} is not a roic.ai industry label, so excluding it "
                   f"would do nothing. Closest real labels: {', '.join(close) or 'none found'}.")
            if mailer:
                mailer("[Idea Engine] Exclusion not applied", msg +
                       "\n\nReply with the exact label and it will be applied.")
            return f"Applied nothing: {msg}"
        if in_universe and str(value) not in in_universe:
            close = difflib.get_close_matches(str(value), [i for i in in_universe if i],
                                              n=3, cutoff=0.3)
            summary += (f" Note: {value!r} currently matches no company in your "
                        "universe; the exclusion is recorded and will bite if one "
                        f"appears. Labels that ARE in your universe and look "
                        f"close: {', '.join(close) or 'none'}.")

    # Guard 2: never tighten the filter below a workable pool.
    if _is_tightening(change, universe):
        remaining = _survivors_after(change, universe)
        if remaining is not None and remaining < MIN_SURVIVORS:
            msg = (f"That change would leave only {remaining} companies in the "
                   f"screened universe (threshold: {MIN_SURVIVORS}). It was NOT "
                   "applied.")
            if mailer:
                mailer("[Idea Engine] Change not applied: pool too small",
                       msg + "\n\nIf you want it anyway, loosen another filter "
                             "first, or reply confirming a smaller pool is fine.")
            if logger:
                logger.warning(msg)
            return f"Applied nothing: {msg}"

    if ctype == "exclude_industry":
        universe.setdefault("exclude_industries", []).append(str(value))
    elif ctype == "exclude_keyword":
        universe.setdefault("exclude_keywords", []).append(str(value).lower())
    elif ctype == "set_market_cap_min":
        universe.setdefault("market_cap", {})["min_usd"] = int(float(value))
    elif ctype == "set_market_cap_max":
        universe.setdefault("market_cap", {})["max_usd"] = int(float(value))
    elif ctype == "add_country":
        universe.setdefault("country", {}).setdefault("include", []).append(str(value).upper())
    elif ctype == "exclude_country":
        universe.setdefault("country", {}).setdefault("exclude", []).append(str(value).upper())
    else:
        _append_note(str(value))
        return f"Saved as a note ({ctype} not recognized)."
    _dump(UNIVERSE, universe)
    return summary


def log_feedback(reply_text: str, summary: str) -> None:
    FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(FEEDBACK_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n## {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"**You wrote:** {reply_text}\n\n")
        f.write(f"**Applied:** {summary}\n")


def _remove_backup(stamp: str) -> None:
    dest = BACKUP_DIR / stamp
    if dest.is_dir():
        shutil.rmtree(dest, ignore_errors=True)


def process_replies(replies: list, env: dict, logger=None, mailer=None) -> list:
    """Apply each reply. Returns human-readable change summaries.

    A backup is kept only when something actually changed. A reply that
    applied nothing (unclear, none, or a guard refusal) leaves no backup
    behind, so a later "undo" always restores the state before the last REAL
    change rather than silently restoring the already-changed files.
    """
    summaries = []
    for reply in replies:
        stripped = reply.strip().lower()
        if stripped in ("undo", "revert", "undo last", "please undo"):
            summary = undo_last()
            log_feedback(reply, summary)
            summaries.append(summary)
            if logger:
                logger.info(f"Feedback: {summary}")
            continue
        stamp = backup_configs()
        changes = interpret(reply, env, logger=logger)
        applied_any = False
        reply_summaries = []
        for change in changes:
            summary = apply_change(change, env, logger=logger, mailer=mailer)
            reply_summaries.append(summary)
            if not (summary.startswith("Applied nothing")
                    or summary.startswith("No actionable change")):
                applied_any = True
        if not applied_any:
            _remove_backup(stamp)
        summary = " ".join(reply_summaries)
        log_feedback(reply, summary)
        summaries.append(summary)
        if logger:
            logger.info(f"Feedback: {summary}")
    return summaries
