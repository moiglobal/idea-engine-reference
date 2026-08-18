# Idea Engine: reference implementation

**New here, and not a programmer? Start with the course, not this repository.** This repository holds a finished copy of the engine built in the Claude Code Crash Course, kept as a worked example. It is written for someone comfortable reading code. If you are taking the course, work through the lessons on latticework.com (start with Lesson 0), and come back here only if your own build gets stuck and you want to compare against a version that works.

The engine is an idea funnel for your own research, not investment advice. Licensed MIT; see LICENSE.

It emails one idea on most working days. It runs every weekday and sends when
it has something new; on a thin day it tells you it has nothing rather than
repeating a name or lowering its standard. That wording is deliberate and it
is what the engine actually does.

This engine matches the course as revised on 10 August 2026: market data from roic.ai, an explicit currency-conversion step, and SEC EDGAR as the free, independent check on every idea. It was live-tested against both roic.ai and EDGAR on 10 August 2026.

This is an idea funnel for your own research. It is not investment advice. The memos are built from third-party data and, when an Anthropic key is present, a language model's synthesis of it, both of which can be wrong. Verify everything against primary filings before acting.

## If you are taking the course, start here

Do not type the commands in this file. The course's whole method is that you
work in plain English and Claude Code does the typing, and that works just as
well on this folder as on the one you are building yourself.

Download this repository (green **Code** button, then **Download ZIP**),
unzip it, then open the unzipped folder in Claude Code, set the permission
mode to Manual, and ask for what you want:

> Set up this project and run its offline self-test, then show me the output.

It will create the virtual environment, install what the engine needs, and
show you 31 checks passing. No keys and no internet connection are required
for that. From there, ask it to explain any file you are curious about, or to
compare a file against the one in your own engine. Reading `scoring.py`,
`metrics.py`, `currency.py`, `dedup.py` and `feedback.py` with Claude Code
explaining as you go is the fastest way to see how the pieces fit.

Two honest limits. This is a worked example, not a shortcut past the lessons:
the engine you build yourself is the one you will understand well enough to
change, and the settings here are the author's, not yours. And the rest of
this file is written for someone comfortable reading code, so let Claude Code
translate it rather than fighting it.

When you get stuck, ask Claude Code to explain what it sees, then post in the
comments under the lesson on latticework.com.

## What it does each run

1. Reads the engine mailbox for replies and applies your feedback to the config, with backup, log, and undo.
2. Loads the day's exchange rates, once. Every figure is converted to US dollars at fetch time; no ratio may straddle two currencies.
3. Screens a global universe in stages (tickers, then dollar market caps, then profiles for domicile and industry), reporting a count per stage. roic.ai has no screener endpoint; the funnel order is what keeps this affordable.
4. Pulls raw annual statements and scores every survivor on six weighted factors, computed from statement lines, never from vendor ratios.
5. Refreshes prices through the batch endpoint, one exchange walk instead of one call per company.
6. Drops names sent recently, unless the reporting calendar shows a new period.
7. Runs the SEC EDGAR filing check on the pick and writes the outcome into the memo, every time.
8. Writes a one-page memo grounded in a saved working data file, emails it, and logs everything.

## Folder map

```
idea-engine-reference/
  run_engine.py        # the daily orchestrator + offline self-test (start here)
  config/
    universe.yaml      # Lesson 2: the hard filter (countries, size, exclusions)
    scoring.yaml       # Lesson 2: the six factors and their weights (sum to 100)
    reference/         # roic.ai's real country/sector/industry/exchange labels
  engine/
    config.py          # loads .env and the YAML configs
    util.py            # paths, logging, JSON, safe field access
    roic.py            # roic.ai client: endpoints, errors, pagination, cache
    currency.py        # THE currency step; the only place currency is handled
    screen.py          # Lesson 2: the staged hard filter
    metrics.py         # Lesson 2: factor metrics from RAW statements
    scoring.py         # Lesson 2: percentile ranking + weighted composite
    dedup.py           # Lesson 3: no-repeat rule with reporting-period reset
    sec_edgar.py       # Lesson 3: the filing check against SEC EDGAR
    memo.py            # Lesson 3: grounded memo; protected sections enforced in code
    email_io.py        # Lesson 4: send memo (SMTP), read replies (IMAP)
    feedback.py        # Lesson 4: reply -> config change, with guards and undo
  prompts/
    memo-prompt.md     # the exact prompt the memo writer sends; edit freely
  .claude/skills/idea-memo/SKILL.md   # the memo format, with protected sections
  requirements.txt
  .env.example         # copy to .env and fill in
  RUNBOOK.md           # operations cheat sheet
```

The files worth reading first are `scoring.py`, `metrics.py`, `currency.py`, `dedup.py`, and `feedback.py`. Those hold the ideas.

## Offline self-test

```
python run_engine.py --selftest
```

No keys, no network. It unit-tests the currency conversion (including pence and a deliberately missing rate), the two locked definitions (enterprise-level free cash flow yield and ROIC with the tax-rate cap), the no-repeat rule, the filing-check comparator against canned filings, the code that rebuilds the memo's protected sections and guarantees the filing-check line, and the annualized share-count metric; then it runs the full scoring pipeline on six fictional companies, one of which keeps euro books under a dollar market cap. You should see 31 checks pass and the line "PASS: the cheapest, highest-quality company ranked first."

You need Python 3.10 or newer. Install dependencies first, in a virtual environment:

macOS or Linux:
```
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```
Windows (PowerShell):
```
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe run_engine.py --selftest
```
The Windows lines call the environment's own Python directly instead of
activating it. Activating means running `venv\Scripts\Activate.ps1`, and a
default Windows PowerShell refuses to run scripts at all, which stops people
here more often than anything else in this file. Calling the executable
sidesteps the question.

## Live runs

Copy `.env.example` to `.env` and fill in your roic.ai key, your SEC
identification (name and email; the SEC blocks anonymous requests), and, for
real daily use, your Anthropic key and the engine's email settings. Then:

```
python run_engine.py --dry-run --limit 50   # quick live test: memo, no email
python run_engine.py                        # the real daily run
python run_engine.py --refresh              # ignore caches and refetch
```

`--limit 50` stops every stage of the screen once 50 companies have passed
it, which turns a first run from something near an hour into about a minute.
It is a test of the machinery, not a search of the market: it walks the
ticker list in order, so it reports on the first companies alphabetically
rather than the best ones available. The run says so in the log, and the memo
carries the same warning at the top. Drop the flag for a real run.

A dry run does not record the company it picks, so testing never costs you an
idea you have not read yet.

Without an Anthropic key the engine still runs end to end and writes a
deterministic memo from the same data; the language model only ever phrases
numbers the engine computed.

The first full run is the expensive one: one enterprise-value request per
candidate, one profile per size survivor, then four requests per scored
survivor (three statements and the stock-splits list, plus a fifth when a
company's profile carries no reporting calendar). Measured on 18 August 2026
against a US-only, $2B universe: 8,551 tickers, 1,809 above the floor, 1,336
through the full filter, 10,469 requests, about 35 minutes. That is roughly
300 requests a minute, which is the Individual plan's ceiling, so on that
plan expect the engine to spend part of the run waiting. The engine prints
the arithmetic before starting and, at a keyboard, asks before a big cold
run. Everything is cached under
`data/cache/`, so later runs refetch only what has aged out: statements
refresh when a company reports a new period or after 30 days, market caps
daily, prices daily in bulk, the ticker list weekly.

## Configuration

`config/universe.yaml` is the hard filter. It ships scoped to US-domiciled companies with a $2B floor so the first run is fast; widen it from there. Two settings deserve attention. `country.basis` chooses what "country" means, domicile or listing, because roic.ai's listing country is the country of the exchange while a company's domicile lives on its profile; most investors mean domicile. And the industry exclusions must use roic.ai's exact labels, which are in `config/reference/industries.json`; the engine warns loudly about a label that matches nothing. roic.ai has no "SPAC" industry label, so blank-check companies are caught by the keyword filter.

`config/scoring.yaml` holds the six factors and their weights, which must sum to 100. The defaults are value-first: valuation 35, returns on capital 25, margin quality 15, capital discipline 15, balance sheet 5, growth 5. Three definitions are pinned in `engine/metrics.py` with the reasoning in comments: free cash flow yield is enterprise-level (after-tax interest added back), ROIC uses the company's own effective tax rate capped to 0-40% with goodwill included in invested capital, and nothing is compared across currencies unconverted.

One deliberate limitation: this scorer cannot rank financial companies. Every valuation metric divides by enterprise value, which has little meaning for a bank. Exclude financials in `universe.yaml`, or analyse them separately; do not leave them in and trust the number.

## Refining by email

Reply to any memo in plain English. On the next run the engine interprets your reply, changes the right file (backing it up first), logs it to `data/feedback-log.md`, announces the change at the top of the next memo, and reverts if you reply "undo". It can change the hard filter, the factor weights, the memo format, or record a standing note. Four guards run every time: an ambiguous reply changes nothing and emails you back; a tightening change that would leave fewer than 20 companies is refused with the count; an industry label that matches nothing is refused with the closest real labels suggested; and the memo's absolute valuation section and closing footer survive every request, including "make it shorter", both in the reply loop and in the memo generator itself.

## Scheduling

On a server, run it every weekday morning with cron:
```
0 7 * * 1-5   cd ~/idea-engine && venv/bin/python run_engine.py >> data/cron.log 2>&1
```
Lesson 5 covers provisioning the server and getting the timezone right.

## Honest limitations

- The score is relative to the day's universe. A top rank is not "cheap" in absolute terms, which is why the memo always prints raw valuation figures next to the percentiles.
- Data quality varies, especially outside the US. Ratios are computed from raw statements with sanity checks, but bad data can still slip through. When a junk name reaches the top, add the sanity check that would have caught it.
- roic.ai states market cap as of the latest reported period end, not as of today. The memo prints that as-of date; the price line is current. For a size filter this is fine; do not read the market cap as this morning's.
- SEC EDGAR covers only SEC registrants, so the independent check is strongest exactly where the data was already strongest. For most non-US names the memo will honestly report that no independent check was possible; that is the check working, not failing.
- With `country.basis: domicile` and an include list, the engine prefilters by listing country to avoid pricing every listed company on earth. A company domiciled in your countries but listed only elsewhere is missed; `prefilter_by_listing: false` runs the complete version at real cost.
- Auto-editing the YAML config does not preserve its comments. The pre-change copy in `config/backups/` keeps the commented original recoverable.
- The bear case in the memo is generated from the numbers and common risks, not researched. Treat it as prompts for your own work.

## Costs

roic.ai Professional at about $89/month is the data source the course assumes ($29 Individual works with a 300 requests/minute cap and 5 years of history; multi-year averages get noisier). Anthropic API usage is roughly ten cents per memo. A small always-on server is $5 to $7/month. SEC EDGAR is free.
