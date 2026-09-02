"""Logging configuration.

The original used ``print``, so nothing carried a timestamp and nothing
survived the terminal window. A bot left running for a week needs both.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str = "INFO", log_dir: str | Path | None = None) -> None:
    """Send logs to the console, and to a rotating file when a directory is given.

    Rotating rather than plain: a bot polling every minute for a month
    otherwise leaves a log nobody can open.
    """
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, TIME_FORMAT))
    root.addHandler(console)

    if log_dir is None:
        return

    directory = Path(log_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "mt5bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
    except OSError as exc:
        root.warning("file logging disabled: cannot use %s (%s)", directory, exc)
        return

    handler.setFormatter(logging.Formatter(FILE_FORMAT, TIME_FORMAT))
    root.addHandler(handler)
