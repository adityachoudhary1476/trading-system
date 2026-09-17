import subprocess, time, sys, os

# Start the tunnel
tunnel = subprocess.Popen(
    'railway connect postgres --tunnel-only --port 5433',
    shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
)

print("Tunnel PID:", tunnel.pid)
print("Waiting for tunnel to establish (15s)...")
time.sleep(15)

# Check if still running
if tunnel.poll() is not None:
    stdout, stderr = tunnel.communicate()
    print("TUNNEL_EXITED_EARLY with code", tunnel.returncode)
    print("STDOUT:", repr(stdout[:1000]))
    print("STDERR:", repr(stderr[:1000]))
    sys.exit(1)

print("Tunnel process still running. Attempting DB connection...")

try:
    import psycopg2
    conn = psycopg2.connect(
        'postgresql://postgres:jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD@localhost:5433/railway'
    )
    cur = conn.cursor()
    cur.execute('SELECT count(*) FROM market_data')
    rows = cur.fetchone()[0]
    print("ROWS:", rows)
    cur.execute("SELECT DISTINCT symbol, timeframe FROM market_data WHERE symbol LIKE 'NSE:NIFTY%' LIMIT 20")
    nifty_data = cur.fetchall()
    print("NIFTY_DATA:", nifty_data)
    cur.execute("SELECT DISTINCT symbol FROM market_data WHERE symbol LIKE 'NSE:NIFTY%' ORDER BY symbol")
    symbols = [r[0] for r in cur.fetchall()]
    print("NIFTY_SYMBOLS:", symbols)
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name")
    tables = [r[0] for r in cur.fetchall()]
    print("TABLES:", tables)
    conn.close()
    print("DB_CONNECTION=OK")
except Exception as e:
    print(f"DB_CONNECTION_FAILED: {e}")

# Cleanup tunnel
tunnel.terminate()
try:
    stdout, stderr = tunnel.communicate(timeout=10)
    print("TUNNEL_STDOUT:", repr(stdout[:500]))
    print("TUNNEL_STDERR:", repr(stderr[:500]))
except:
    tunnel.kill()
    print("TUNNEL_KILLED")
