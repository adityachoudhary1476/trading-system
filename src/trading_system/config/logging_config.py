"""Structured logging setup.

Produces level-tagged, contextual logs without ever exposing secrets.
Logs are written both to stdout (console) and to a rotating file under
the configured log directory. Secret values are never passed through here.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .settings import LoggingConfig, settings


def configure_logging(cfg: LoggingConfig | None = None) -> logging.Logger:
    cfg = cfg or settings.logging
    logger = logging.getLogger("trading_system")
    if logger.handlers:
        # Idempotent: do not attach handlers twice.
        return logger

    logger.setLevel(cfg.level)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = cfg.log_dir / "trading_system.log"
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger


# A module-level logger usable after configure_logging() has run.
log = logging.getLogger("trading_system")
