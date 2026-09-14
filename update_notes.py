import os
import sys
from sqlalchemy import create_engine, text

db_url = os.environ.get('MARKET_DATA_DB_URL')
print(f"DB URL: {db_url[:60] if db_url else 'NOT SET'}", flush=True)

engine = create_engine(db_url)
with engine.connect() as conn:
    result = conn.execute(text("SELECT deployment_id, notes FROM paper_deployments WHERE symbol = :symbol AND timeframe = :timeframe"), {'symbol': 'NSE:NIFTY', 'timeframe': '1d'})
    rows = list(result)
    print(f"Found {len(rows)} deployments", flush=True)
    for row in rows:
        print(f"  deployment_id: {row[0]}, notes: {row[1]}", flush=True)
        if not row[1] or row[1] != 'bot:bot-nifty-options':
            conn.execute(text("UPDATE paper_deployments SET notes = :notes WHERE deployment_id = :id"), {'notes': 'bot:bot-nifty-options', 'id': row[0]})
            conn.commit()
            print(f"  -> updated to: bot:bot-nifty-options", flush=True)
    print("Done", flush=True)