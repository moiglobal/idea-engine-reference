"""Shared helpers: paths, logging, JSON I/O, and safe field access.

Nothing in here touches the network, so it is safe to import anywhere.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime
from pathlib import Path

# --- Project paths -----------------------------------------------------------
# This file lives at <project>/engine/util.py, so the project root is one up.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
REFERENCE_DIR = CONFIG_DIR / "reference"   # roic.ai's own label catalogues
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
WORKING_DIR = DATA_DIR / "working"         # the memo's working data files
BACKUP_DIR = PROJECT_ROOT / "config" / "backups"
OUTPUT_DIR = PROJECT_ROOT / "output"

HISTORY_FILE = DATA_DIR / "history.json"
FEEDBACK_LOG = DATA_DIR / "feedback-log.md"
COVERAGE_LOG = DATA_DIR / "coverage-log.json"  # feeds the Friday coverage report
RUN_LOG = DATA_DIR / "run.log"


def ensure_dirs() -> None:
    """Create the runtime folders if they do not exist yet."""
    for d in (DATA_DIR, CACHE_DIR, WORKING_DIR, BACKUP_DIR, OUTPUT_DIR, REFERENCE_DIR):
        d.mkdir(parents=True, exist_ok=True)


# --- Logging -----------------------------------------------------------------
def get_logger(name: str = "idea-engine") -> logging.Logger:
    """A logger that writes both to the screen and to data/run.log."""
    logger = logging.getLogger(name)
    if logger.handlers:  # already configured
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    try:
        ensure_dirs()
        fileh = logging.FileHandler(RUN_LOG, encoding="utf-8")
        fileh.setFormatter(fmt)
        logger.addHandler(fileh)
    except OSError:
        # If the log file cannot be opened (e.g. read-only test), screen is enough.
        pass
    return logger


# --- JSON helpers ------------------------------------------------------------
def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# --- Safe data access --------------------------------------------------------
def g(d: dict, *keys, default=None):
    """Return the first present, non-None value among several possible keys.

    Vendor field names can drift between endpoints and over time (for example
    a preferred share-count field with a fallback). Trying a few candidate
    names, in order of preference, keeps the engine robust.
    """
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def num(x):
    """Coerce a value to a finite float, or return None if it cannot be."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def safe_div(a, b):
    """a / b, returning None on bad inputs or division by zero."""
    a, b = num(a), num(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


def today_str() -> str:
    return date.today().isoformat()


def parse_date(s):
    """Parse a YYYY-MM-DD date string into a date, or None."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
