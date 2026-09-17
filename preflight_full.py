import os
import sys
sys.path.insert(0, '.')
from sqlalchemy import create_engine, text

# Use Railway public proxy
db_url = 'postgresql://postgres:jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD@altaria.proxy.rlwy.net:23861/railway'

print(f"DB URL: {db_url[:50]}...", flush=True)

engine = create_engine(db_url)
with engine.connect() as conn:
    # 1. Connectivity
    result = conn.execute(text('SELECT 1'))
    print('A. PostgreSQL connection: PASS', flush=True)
    
    # 2. Schema version
    result = conn.execute(text('SELECT version FROM schema_version WHERE id = 1'))
    row = result.fetchone()
    if row:
        print(f'C. Schema: PASS + version {row[0]}', flush=True)
    else:
        print('C. Schema: FAIL - no version row', flush=True)
    
    # 3. market_data table
    result = conn.execute(text("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'market_data')"))
    row = result.fetchone()
    market_data_exists = row[0]
    print(f'market_data table exists: {market_data_exists}', flush=True)
    
    if market_data_exists:
        # 4. NIFTY 1d data
        result = conn.execute(text("""
            SELECT COUNT(*), MIN(timestamp), MAX(timestamp) 
            FROM market_data 
            WHERE symbol = 'NSE:NIFTY' AND timeframe = '1d'
        """))
        row = result.fetchone()
        count = row[0] if row else 0
        min_ts = row[1] if row else None
        max_ts = row[2] if row else None
        print(f'B. NIFTY 1d data: {count} rows + date range {min_ts} to {max_ts}', flush=True)
        
        # 5. Available timeframes
        result = conn.execute(text("""
            SELECT timeframe, COUNT(*) 
            FROM market_data 
            WHERE symbol = 'NSE:NIFTY' 
            GROUP BY timeframe
        """))
        print('Available timeframes for NSE:NIFTY:', flush=True)
        for row in result:
            print(f'  {row[0]}: {row[1]} rows', flush=True)
    
    # 6. Strategies
    result = conn.execute(text("SELECT COUNT(*) FROM strategies"))
    row = result.fetchone()
    print(f'Strategies count: {row[0]}', flush=True)
    
    result = conn.execute(text("SELECT strategy_id, name, symbol, status FROM strategies"))
    print('Strategies:', flush=True)
    for row in result:
        print(f'  {row[0][:16]}... | {row[1]} | {row[2]} | {row[3]}', flush=True)
    
    # 7. PAPER_APPROVED evidence
    result = conn.execute(text("""
        SELECT COUNT(*) FROM strategy_evidence 
        WHERE configuration_json LIKE '%PAPER_APPROVED%'
    """))
    row = result.fetchone()
    paper_approved_count = row[0] if row else 0
    print(f'D. Existing PAPER_APPROVED: {paper_approved_count}', flush=True)
    
    # 8. Paper deployments
    result = conn.execute(text("SELECT COUNT(*) FROM paper_deployments"))
    row = result.fetchone()
    print(f'Paper deployments count: {row[0]}', flush=True)
    
    result = conn.execute(text("SELECT deployment_id, strategy_id, symbol, status FROM paper_deployments"))
    print('Paper deployments:', flush=True)
    for row in result:
        print(f'  {row[0]} | {row[1]} | {row[2]} | {row[3]}', flush=True)

print('\\n=== PREFLIGHT COMPLETE ===', flush=True)