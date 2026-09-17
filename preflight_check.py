import os
import sys
sys.path.insert(0, '.')
from sqlalchemy import create_engine, text

db_url = os.environ.get('MARKET_DATA_DB_URL')
print(f"DB URL: {db_url[:50]}...", flush=True)

engine = create_engine(db_url)
with engine.connect() as conn:
    result = conn.execute(text('SELECT 1'))
    print('Connectivity: PASS', flush=True)
    
    result = conn.execute(text('SELECT version FROM schema_version WHERE id = 1'))
    row = result.fetchone()
    if row:
        print(f'Schema version: {row[0]}', flush=True)
    else:
        print('Schema version: NOT FOUND', flush=True)
    
    result = conn.execute(text("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'market_data')"))
    row = result.fetchone()
    print(f'market_data table exists: {row[0]}', flush=True)