import psycopg2, json

DB_URL = "postgresql://postgres:jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD@altaria.proxy.rlwy.net:23861/railway"

conn = psycopg2.connect(DB_URL)
cur = conn.cursor()

cur.execute("""
    SELECT bot_id, payload_json, updated_at
    FROM autonomous_portfolio_state
    ORDER BY updated_at DESC
    LIMIT 1
""")
row = cur.fetchone()
if row:
    payload = json.loads(row[1])
    print(f"Updated: {row[2]}")
    print(f"Positions: {json.dumps(payload.get('positions', []), indent=2)}")
    print(f"Actions (last 5):")
    for a in payload.get('actions', [])[-5:]:
        print(f"  {a}")
else:
    print("No snapshot")

conn.close()
