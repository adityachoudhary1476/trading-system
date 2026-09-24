import psycopg2, json

DB_URL = "postgresql://postgres:jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD@altaria.proxy.rlwy.net:23861/railway"

conn = psycopg2.connect(DB_URL)
cur = conn.cursor()

# Get the latest snapshot
cur.execute("""
    SELECT bot_id, payload_json, updated_at
    FROM autonomous_portfolio_state
    ORDER BY updated_at DESC
    LIMIT 1
""")
row = cur.fetchone()
if row:
    print(f"Bot ID: {row[0]}")
    print(f"Updated: {row[2]}")
    payload = json.loads(row[1])
    print(f"\nSnapshot keys: {list(payload.keys())}")
    print(f"Status: {payload.get('status')}")
    print(f"Source: {payload.get('data_source')}")
    print(f"Positions count: {len(payload.get('positions', []))}")
    print(f"Positions: {json.dumps(payload.get('positions', []), indent=2)}")
    print(f"Actions count: {len(payload.get('actions', []))}")
    if payload.get('actions'):
        print(f"Last 3 actions:")
        for a in payload['actions'][-3:]:
            print(f"  - {a.get('action')}: {a.get('symbol')} | {a.get('strategy_id')} | {a.get('reason')} | qty={a.get('quantity')} | price={a.get('price')}")
    print(f"\nCapital: {json.dumps(payload.get('capital', {}), indent=2)}")
    print(f"PnL: {json.dumps(payload.get('pnl', {}), indent=2)}")
else:
    print("No snapshot found")

conn.close()
