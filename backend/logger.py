"""logger.py — Centralised logging for FormCheck.

All modules call `get_logger(__name__)` to get a pre-configured logger.

Log file:  backend/logs/formcheck.log
           10 MB per file, 5 rotating backups → up to 50 MB of history.

Console:   INFO and above (so normal runs aren't spammy).
File:      DEBUG and above (every detail for post-session analysis).

Usage:
    from logger import get_logger
    log = get_logger(__name__)
    log.info("something happened")
    log.debug("verbose detail: %s", value)
"""

import logging
import logging.handlers
from pathlib import Path

# ── Log directory ─────────────────────────────────────────────────────────────
_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "formcheck.log"

# ── Formatters ────────────────────────────────────────────────────────────────
_FILE_FMT = logging.Formatter(
    fmt="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(name)-22s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_CONSOLE_FMT = logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt="%H:%M:%S",
)

# ── Root handler setup (runs once) ────────────────────────────────────────────
_configured = False


def _configure_root() -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger("formcheck")
    root.setLevel(logging.DEBUG)

    # Rotating file handler — full DEBUG detail
    fh = logging.handlers.RotatingFileHandler(
        _LOG_FILE,
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_FILE_FMT)
    root.addHandler(fh)

    # Console handler — INFO+ only
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(_CONSOLE_FMT)
    root.addHandler(ch)

    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the 'formcheck' namespace.

    Pass __name__ from the calling module:
        log = get_logger(__name__)   →  formcheck.camera / formcheck.server / …
    """
    _configure_root()
    # Strip leading package path if running from the backend/ directory
    short = name.split(".")[-1] if "." in name else name
    return logging.getLogger(f"formcheck.{short}")


def log_path() -> Path:
    """Return the path to the current log file (useful for printing on startup)."""
    return _LOG_FILE
