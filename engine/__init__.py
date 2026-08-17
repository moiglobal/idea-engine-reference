"""Idea Engine: a daily value-investing idea generator.

This package is the known-good reference implementation for the Claude Code
Crash Course, running on roic.ai for market data and SEC EDGAR for the
independent filing check. Each module maps to a lesson:

  roic.py       Lessons 1-2  roic.ai client: endpoints, errors, cache
  currency.py   Lesson 2   the single place currency is converted to dollars
  screen.py     Lesson 2   the staged hard filter over the universe
  metrics.py    Lesson 2   factor metrics computed from raw statements
  scoring.py    Lesson 2   percentile ranking and the weighted composite
  dedup.py      Lesson 3   no-repeat rule with a reporting-period reset
  sec_edgar.py  Lesson 3   the filing check against SEC EDGAR
  memo.py       Lesson 3   grounded one-page memo, protected sections enforced
  email_io.py   Lesson 4   send the memo, read replies
  feedback.py   Lesson 4   turn a reply into a config change, with undo
  run_engine    Lesson 5   orchestrates the full daily run

Read run_engine.py first; it calls everything in order.
"""

__version__ = "2.0.0"
