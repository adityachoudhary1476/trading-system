"""Telegram notifications. Wired but inert unless configured.

Secrets (bot token, chat id) are read from environment variables at send time
and are NEVER stored in config or logs. Disabled by default.
"""
from __future__ import annotations

import os

import requests

from ..config import settings, log


def send_message(text: str) -> bool:
    """Send a Telegram message. No-ops (returns False) when disabled/unconfigured.

    Never logs the message body's sensitive content; only logs success/failure.
    """
    cfg = settings.telegram
    if not cfg.enabled:
        log.debug("Telegram disabled; skipping send.")
        return False
    token = os.getenv(cfg.token_env)
    chat_id = os.getenv(cfg.chat_id_env)
    if not token or not chat_id:
        log.warning("Telegram enabled but credentials missing in env; skip.")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4096]},
            timeout=15,
        )
        ok = resp.status_code == 200
        log.info("Telegram send %s", "ok" if ok else f"failed:{resp.status_code}")
        return ok
    except Exception as e:  # do not crash the pipeline over a notify failure
        log.error("Telegram send error: %s", e)
        return False
