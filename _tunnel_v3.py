import subprocess, time, os, sys

key_path = '~/.ssh/railway_deploy_new'
DB_PASSWORD = 'jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD'
PORT = 5441
LOCAL_DB_URL = f'postgresql://postgres:{DB_PASSWORD}@localhost:{PORT}/railway'

# ── Step 1: Test SSH connectivity ──────────────────────────────────────────
print("[1] Testing SSH connectivity to ssh.railway.com...")
test_ssh = subprocess.run(
    f'ssh -i {key_path} -o BatchMode=yes -o ConnectTimeout=15 '
    '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null '
    'ssh.railway.com echo SSH_CONNECTED',
    shell=True, capture_output=True, text=True, timeout=20
)

if test_ssh.returncode == 0 and 'SSH_CONNECTED' in test_ssh.stdout:
    print("  SSH connection: OK")
else:
    print(f"  SSH connection: FAILED (code={test_ssh.returncode})")
    print(f"  stdout: {test_ssh.stdout[:200]}")
    print(f"  stderr: {test_ssh.stderr[:500]}")
    sys.exit(1)

# ── Step 2: Start SSH tunnel with sleep command ────────────────────────────
print(f"\n[2] Starting SSH tunnel on port {PORT} with 'sleep 600'...")
tunnel = subprocess.Popen(
    f'ssh -i {key_path} -L {PORT}:postgres.railway.internal:5432 '
    '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null '
    '-o ConnectTimeout=30 -o ServerAliveInterval=10 '
    f'ssh.railway.com sleep 600',
    shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
)
print(f"  Tunnel PID: {tunnel.pid}")

# ── Step 3: Wait for tunnel and test DB ─────────────────────────────────────
print(f"\n[3] Waiting for tunnel + PostgreSQL to respond...")
db_ok = False
for i in range(60):
    time.sleep(1)

    if tunnel.poll() is not None:
        stdout, stderr = tunnel.communicate()
        print(f"  Tunnel exited at {i+1}s (code={tunnel.returncode})")
        print(f"  stderr: {stderr[-800:]}")
        break

    try:
        import psycopg2
        conn = psycopg2.connect(LOCAL_DB_URL, connect_timeout=8)
        cur = conn.cursor()
        cur.execute('SELECT count(*) FROM market_data')
        rows = cur.fetchone()[0]
        cur.close()
        conn.close()
        print(f"  DB OK after {i+1}s! market_data rows: {rows}")
        db_ok = True
        break
    except psycopg2.OperationalError as e:
        msg = str(e)
        if "refused" in msg:
            tag = "REFUSED"
        elif "timeout" in msg or "timed out" in msg:
            tag = "TIMEOUT"
        else:
            tag = msg[:50]
        if i % 10 == 9:
            print(f"  {i+1}s: {tag}")
    except Exception as e:
        if i % 10 == 9:
            print(f"  {i+1}s: {str(e)[:80]}")

if not db_ok:
    print("\n  ERROR: Could not establish DB connection via tunnel")
    tunnel.terminate()
    try:
        stdout, stderr = tunnel.communicate(timeout=10)
        print(f"  Tunnel stderr:\n{stderr[-1000:]}")
    except:
        tunnel.kill()
    sys.exit(1)

print(f"\n  Tunnel is LIVE on port {PORT}")
print(f"  DB URL: {LOCAL_DB_URL[:45]}...")
print(f"  Do not kill this process — tunnel is active")
print("\n  Now run the tournament in a SEPARATE terminal with:")
print(f"  MARKET_DATA_DB_URL={LOCAL_DB_URL} python3 run_tournament.py --universe nifty-only --wf-folds 5 --bootstrap-n 1000 --top-n-deploy 5 --tournament-id phase23-production-20260916")

# Keep the tunnel alive
print("\n  Keeping tunnel alive. Press Ctrl+C or kill to stop.")
try:
    # Read from tunnel subprocess to keep it alive
    while tunnel.poll() is None:
        time.sleep(1)
        # Keep stdin alive to prevent the shell from exiting
        if i % 30 == 0:
            print(f"  Tunnel alive... ({i}s)")
        i += 1
except KeyboardInterrupt:
    pass

tunnel.terminate()
try:
    tunnel.communicate(timeout=10)
except:
    tunnel.kill()
print("\nTunnel terminated.")
