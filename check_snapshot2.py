import psycopg2, json

conn = psycopg2.connect("postgresql://postgres:jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD@altaria.proxy.rlwy.net:23861/railway")
try:
    cur = conn.cursor()
    
    # Check autonomous_portfolio_state
    cur.execute("SELECT MAX(updated_at) FROM autonomous_portfolio_state")
    row = cur.fetchone()
    print(f"autonomous_portfolio_state: last_updated={row[0]}")
    
    # Get column names
    cur.execute("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_schema='public' AND table_name='autonomous_portfolio_state'
        ORDER BY ordinal_position
    """)
    cols = [r[0] for r in cur.fetchall()]
    print(f"autonomous_portfolio_state columns: {cols}")
    
    # Query the snapshot data
    data_col = [c for c in cols if c not in ('session_id', 'updated_at')][0] if len(cols) > 2 else 'data'
    cur.execute(f"SELECT {data_col} FROM autonomous_portfolio_state LIMIT 1")
    row = cur.fetchone()
    if row and row[0]:
        snap = json.loads(row[0])
        print(f"\nSnapshot keys: {list(snap.keys())}")
        print(f"position: {json.dumps(snap.get('position', {}), indent=2)}")
        print(f"positions: {json.dumps(snap.get('positions', []), indent=2)}")
        print(f"capital: {snap.get('capital')}")
        print(f"strategies: {snap.get('strategies', {})}")
        print(f"actions_count: {len(snap.get('actions', []))}")
finally:
    conn.close()
