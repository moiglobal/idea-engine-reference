"""Load configuration: the .env secrets and the two YAML config files.

dotenv is used if installed, but we fall back to a tiny parser so the offline
self-test runs with only PyYAML present.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .util import CONFIG_DIR, PROJECT_ROOT

ENV_PATH = PROJECT_ROOT / ".env"

# Settings that must be present for a real run (not the self-test).
# ROIC_API_KEY fetches the data; SEC_USER_AGENT identifies you to SEC EDGAR
# for the filing check, which runs on every idea. ANTHROPIC_API_KEY is
# strongly recommended but not blocking: without it the memo falls back to a
# deterministic template and reply feedback is saved as notes, with a warning.
REQUIRED_FOR_RUN = ["ROIC_API_KEY", "SEC_USER_AGENT"]
RECOMMENDED_FOR_RUN = ["ANTHROPIC_API_KEY"]
REQUIRED_FOR_EMAIL = ["ENGINE_EMAIL_ADDRESS", "ENGINE_EMAIL_APP_PASSWORD", "RECIPIENT_EMAIL"]

# The values .env.example ships with. Copying the example to .env and running
# it before filling anything in is the commonest first mistake, and none of
# these strings is empty, so a plain emptiness test waves them through into a
# live run that then fails somewhere far from the cause. An unedited setting
# is a missing setting, and gets the same clear sentence.
PLACEHOLDER_VALUES = {
    "your_roic_key_here",
    "sk-ant-your_key_here",
    "Your Name your.email@example.com",
    "yourname.ideaengine@gmail.com",
    "your_16_character_app_password",
    "you@example.com",
}


def _load_env_file(path: Path) -> None:
    """Populate os.environ from a .env file without overwriting real env vars."""
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(path, override=False)
        return
    except Exception:
        pass  # fall back to the manual parser below
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_env() -> dict:
    """Return all engine settings as a dict, with sensible email defaults."""
    _load_env_file(ENV_PATH)
    return {
        "ROIC_API_KEY": os.environ.get("ROIC_API_KEY", ""),
        "SEC_USER_AGENT": os.environ.get("SEC_USER_AGENT", ""),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", "claude-opus-5"),
        "ENGINE_EMAIL_ADDRESS": os.environ.get("ENGINE_EMAIL_ADDRESS", ""),
        "ENGINE_EMAIL_APP_PASSWORD": os.environ.get("ENGINE_EMAIL_APP_PASSWORD", ""),
        "RECIPIENT_EMAIL": os.environ.get("RECIPIENT_EMAIL", ""),
        "SMTP_HOST": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "SMTP_PORT": int(os.environ.get("SMTP_PORT", "465")),
        "IMAP_HOST": os.environ.get("IMAP_HOST", "imap.gmail.com"),
        "IMAP_PORT": int(os.environ.get("IMAP_PORT", "993")),
    }


def missing_settings(env: dict, include_email: bool = True) -> list:
    """Which required settings are still blank or still the example value."""
    needed = list(REQUIRED_FOR_RUN)
    if include_email:
        needed += REQUIRED_FOR_EMAIL
    return [k for k in needed
            if not env.get(k) or str(env.get(k)).strip() in PLACEHOLDER_VALUES]


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_universe() -> dict:
    cfg = load_yaml(CONFIG_DIR / "universe.yaml")
    _validate_universe(cfg)
    return cfg


def _validate_universe(cfg: dict) -> None:
    country = cfg.get("country", {}) or {}
    basis = (country.get("basis") or "domicile").strip().lower()
    if basis not in ("domicile", "listing"):
        raise ValueError(
            f"country.basis must be 'domicile' or 'listing', not {basis!r}. "
            "Edit config/universe.yaml.")


def load_scoring() -> dict:
    cfg = load_yaml(CONFIG_DIR / "scoring.yaml")
    _validate_scoring(cfg)
    return cfg


def _validate_scoring(cfg: dict) -> None:
    """Refuse to run if the weights do not sum to 100."""
    factors = cfg.get("factors", {})
    total = sum(float(f.get("weight", 0)) for f in factors.values())
    if abs(total - 100.0) > 0.01:
        raise ValueError(
            f"Scoring weights must sum to 100, but they sum to {total:.1f}. "
            f"Edit config/scoring.yaml.")


def read_claude_md() -> str:
    """The investor's style and approach notes, if present."""
    for name in ("CLAUDE.md", "CLAUDE.MD", "claude.md"):
        p = PROJECT_ROOT / name
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""
