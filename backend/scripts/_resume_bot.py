"""Clear the kill switch on the autonomous scheduler bot.

Reads BOT_ID and MARKET_DATA_DB_URL from the environment (provided by railway ssh).
Updates autonomous_bots.kill_switch_state to 'active' so the scheduler resumes.
"""
import os
from sqlalchemy import create_engine, text

bot_id = os.environ.get("BOT_ID", "bot-nifty-options")
url = os.environ.get("MARKET_DATA_DB_URL", "")
if not url:
    print("ERROR: MARKET_DATA_DB_URL not set on the service")
    exit(1)

engine = create_engine(url)
with engine.begin() as conn:
    # Verify bot exists and show current state
    row = conn.execute(
        text("SELECT kill_switch_state, kill_switch_reason, enabled FROM autonomous_bots WHERE bot_id = :b"),
        {"b": bot_id}
    ).fetchone()
    if row is None:
        print(f"Bot {bot_id} not found in autonomous_bots table")
        exit(1)
    print(f"Before: kill_switch={row[0]} reason={row[1]} enabled={row[2]}")

    # Clear kill switch (resume)
    conn.execute(text(
        "UPDATE autonomous_bots SET kill_switch_state = 'active', "
        "kill_switch_reason = NULL, kill_switch_halted_at = NULL, "
        "enabled = 'true', updated_at = NOW() WHERE bot_id = :b"
    ), {"b": bot_id})

    # Verify
    row = conn.execute(
        text("SELECT kill_switch_state, enabled FROM autonomous_bots WHERE bot_id = :b"),
        {"b": bot_id}
    ).fetchone()
    print(f"After: kill_switch={row[0]} enabled={row[1]}")
    print("Done. Scheduler will resume on next tick (~30s)")
