# Idea Engine Runbook

Fill in the bracketed parts for your own setup. This is what you reach for in
six months when you have forgotten the details.

## What it does
Emails one investment idea on most weekdays at [7am, my timezone], chosen by a
hard-filter screen and a six-factor value-and-quality score over a [global / US]
universe, never repeating a name until it reports a new period, with every
memo carrying an SEC filing-check outcome and absolute valuation figures. It
runs every weekday and sends when it has something new; on a thin day it tells
you it has nothing rather than repeating a name or lowering its standard.

## Where it lives
- Server: [provider], IP [address]. Log in: `ssh [user]@[address]`
  (or start a Claude Code session with the SSH environment pointed at it)
- Project folder: `~/idea-engine`
- Schedule: cron, weekdays. See it with `crontab -l`. Expected line:
  `0 7 * * 1-5  cd ~/idea-engine && venv/bin/python run_engine.py >> data/cron.log 2>&1`

## Keys and accounts (where they are, never the secret values)
- roic.ai Professional: account [email]. Key in `~/idea-engine/.env` as ROIC_API_KEY.
- SEC EDGAR: no account. Identification string in `.env` as SEC_USER_AGENT
  (your name and email; the SEC blocks anonymous requests).
- Anthropic: platform.claude.com, account [email]. Key in `.env`.
- Engine Gmail: [address]. App password in `.env`. Sends to RECIPIENT_EMAIL [your inbox].

## Settings that shape the output
- `config/universe.yaml`: the hard filter (country basis, size, exclusions)
- `config/scoring.yaml`: the six factor weights (must sum to 100)
- `.claude/skills/idea-memo/SKILL.md`: how the memo is written
- `prompts/memo-prompt.md`: the exact prompt sent to the memo model
- `CLAUDE.md`: standing instructions
- `config/backups/`: timestamped copies, written before any auto-applied change

## Run it by hand
- Offline logic check: `python run_engine.py --selftest`
  (no keys, no network; 27 unit checks plus the full scoring pipeline)
- Quick live test, no email: `python run_engine.py --dry-run --limit 50`
- Full run now: `python run_engine.py`
- Force fresh data (ignore cache): add `--refresh`
- Skip the big-run confirmation in a script: add `--yes` (cron never asks)

## How to change it
- Small tweaks: reply to any memo in plain English (excluded industry, size
  floor, factor weight, memo format). Reply "undo" to revert the last change.
- Bigger changes: edit the YAML configs or the memo skill directly, or ask
  Claude Code, then redeploy with Git.

## When something is wrong
- No memo arrived: check spam; on the server run `tail -50 data/cron.log` and
  `tail -50 data/run.log`; confirm the roic.ai and Anthropic accounts are
  funded and active.
- Got a "Run FAILED" email: it names the failed step. Open Claude Code and paste it.
- roic.ai 401: the key stopped working. Regenerate it at roic.ai and update `.env`.
- roic.ai 402: your plan no longer covers an endpoint the engine uses (the
  alert names it). Both 401 and 402 mean no memo until you act.
- roic.ai 429: too many requests per minute. The engine waits and retries by
  itself; frequent 429s on the Individual plan mean the universe has outgrown
  the plan's 300 requests/minute.
- SEC EDGAR 403: the User-Agent header is missing or does not identify you.
  Check SEC_USER_AGENT in `.env` (name and email).
- A bad idea was surfaced: reply to tighten a filter, or add a sanity rule in
  `engine/metrics.py` that would have caught it.
- "No new idea today" alert: every top name was sent recently and none has new
  numbers. Normal occasionally; if frequent, widen the universe.
- "Only N eligible" warning in the memo: your filter is too tight. Loosen it.
- Every company suddenly passes the size floor: check currency. Market caps
  arrive in each company's own currency and are converted before the filter;
  if the conversion step broke, the run log will show missing-rate exclusions
  or an error, not silence.
- A number looks wrong: check it against the SEC filing before believing it.
  The memo's filing-check line says whether that was already done.

## Monthly check
- Confirm roic.ai and Anthropic billing are current.
- Skim `data/feedback-log.md` and `output/` for anything off.
- Read the Friday coverage report in the memo: is non-US coverage holding up?
- Apply any security updates the server offers.

## Files that hold state (do not commit these, except history.json)
- `data/history.json`      names sent and their reporting periods. KEEP this
                           in Git; lose it and the engine re-sends old names.
- `data/cache/`            cached tickers, profiles, statements, prices, rates
- `data/working/`          the memo working data files, one per idea
- `data/universe-survivors.json`  the last screened universe, used by the
                           reply loop's over-tightening guard
- `data/coverage-log.json` feeds the Friday coverage report
- `data/feedback-log.md`   every methodology change and what triggered it
- `config/backups/`        pre-change config copies (used by "undo")
- `output/`                every memo generated
