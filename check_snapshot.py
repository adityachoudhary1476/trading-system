import psycopg2, datetime, json

conn = psycopg2.connect("postgresql://postgres:jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD@altaria.proxy.rlwy.net:23861/railway")
try:
    cur = conn.cursor()
    
    # Check autonomous_portfolio_state
    cur.execute("SELECT MAX(updated_at) FROM autonomous_portfolio_state")
    row = cur.fetchone()
    print(f"autonomous_portfolio_state: last_updated={row[0]}")
    
    # Check paper_positions
    cur.execute("SELECT COUNT(*) FROM paper_positions")
    print(f"paper_positions: {cur.fetchone()[0]} rows")
    
    # Check sessions
    cur.execute("SELECT COUNT(*) FROM sessions WHERE bot_id='bot-nifty-options'")
    print(f"sessions for bot-nifty-options: {cur.fetchone()[0]}")
    
    # Check recent actions
    cur.execute("""
        SELECT action_type, symbol, strategy_id, created_at 
        FROM paper bot_actions 
        WHERE bot_id='bot-nifty-options'
        ORDER BY created_at DESC LIMIT 10
    """)
    actions = cur.fetchall()
    print(f"bot_actions: {len(actions)} rows")
    for a in actions:
        print(f"  {a[0]} {a[1]} {a[2]} {a[3]}")
    
    # Check deployments
    cur.execute("""
        SELECT deployment_id, status, notes, dataset_id, created_at, activated_at
        FROM paper_deployments 
        WHERE dataset_id LIKE '%bot-nifty-options%'
    """)
    deps = cur.fetchall()
    print(f"deployments: {len(deps)}")
    for d in deps:
        print(f"  {d[0]} status={d[1]} notes={d[2]} dataset={d[3]} created={d[4]} activated={d[5]}")
    
finally:
    conn.close()
