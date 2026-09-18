"""Post-deploy DB verification.

Checks:
- autonomous_bots.kill_switch_state == 'active' (bot is not halted)
- paper_deployments.options_enabled == true for NIFTY deployments
"""
import os
from sqlalchemy import create_engine, text

url = os.environ.get("MARKET_DATA_DB_URL", "")
if not url:
    print("ERROR: MARKET_DATA_DB_URL not set")
    exit(1)

engine = create_engine(url)
with engine.connect() as conn:
    # Check autonomous_bots
    rows = conn.execute(text(
        "SELECT bot_id, state, enabled, kill_switch_state, kill_switch_reason "
        "FROM autonomous_bots WHERE bot_id = 'bot-nifty-options'"
    ))
    for row in rows:
        status = "OK" if row[3] == "active" and row[2] == "true" else "FAIL"
        print(f"  [{status}] autonomous_bots: {row[0]} | state={row[1]} | enabled={row[2]} | kill={row[3]} | reason={row[4]}")

    # Check paper_deployments for options_enabled
    rows = conn.execute(text(
        "SELECT deployment_id, symbol, options_enabled FROM paper_deployments WHERE symbol LIKE '%NIFTY%'"
    ))
    for row in rows:
        status = "OK" if row[2] is True or row[2] == True else "FAIL"
        dep_id = str(row[0])[:8] if row[0] else "None"
        print(f"  [{status}] paper_deployments: {dep_id} | {row[1]} | options_enabled={row[2]}")

    print("\nVerification complete.")
