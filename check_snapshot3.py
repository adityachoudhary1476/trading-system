import psycopg2, json

conn = psycopg2.connect("postgresql://postgres:jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD@altaria.proxy.rlwy.net:23861/railway")
try:
    cur = conn.cursor()
    
    # Get column names
    cur.execute("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_schema='public' AND table_name='autonomous_portfolio_state'
        ORDER BY ordinal_position
    """)
    cols = [r[0] for r in cur.fetchall()]
    print(f"autonomous_portfolio_state columns: {cols}")
    
    # Query the snapshot data - find the data column
    cur.execute("SELECT * FROM autonomous_portfolio_state LIMIT 1")
    row = cur.fetchone()
    if row:
        for i, col in enumerate(cur.description):
            val = row[i]
            if isinstance(val, bytearray):
                try:
                    val = json.loads(bytes(val))
                except:
                    val = f"<binary {len(val)} bytes>"
            elif isinstance(val, bytes):
                try:
                    val = val.decode('utf-8')
                except:
                    pass
            print(f"  {col.name}: {val}")
finally:
    conn.close()
