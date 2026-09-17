#!/usr/bin/env python3
"""
Robust SSH tunnel + Phase 23 tournament runner.
Tries multiple keys/ports with -N flag.
Once tunnel is up, runs the tournament against Railway PostgreSQL.
"""
import subprocess, time, os, sys

DB_PASSWORD = 'jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD'
KEYS = ['railway_deploy_new', 'railway_deploy']
PORTS = [5436, 5437, 5438, 5439, 5440, 5441, 5442, 5443]

def try_tunnel(port, key_name):
    """Try SSH tunnel with -N. Returns (tunnel_proc, port) or (None, None)."""
    key_path = os.path.expanduser(f'~/.ssh/{key_name}')
    local_url = f'postgresql://postgres:{DB_PASSWORD}@localhost:{port}/railway'
    ssh_cmd = (
        f'ssh -i {key_path} -L {port}:postgres.railway.internal:5432 '
        '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null '
        '-o ConnectTimeout=30 -o ServerAliveInterval=10 '
        f'-N ssh.railway.com'
    )
    print(f"  Starting: ssh -N -L {port}:...:{key_name}")
    tunnel = subprocess.Popen(
        ssh_cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    
    for i in range(30):
        time.sleep(1)
        rc = tunnel.poll()
        if rc is not None:
            stderr = tunnel.stderr.read()[:500] if tunnel.stderr else ""
            print(f"  SSH exited at {i+1}s (code={rc}). stderr: {stderr[:200]}")
            return None
        
        # Try DB connection
        try:
            import psycopg2
            conn = psycopg2.connect(local_url, connect_timeout=5)
            cur = conn.cursor()
            cur.execute('SELECT count(*) FROM market_data')
            rows = cur.fetchone()[0]
            cur.close()
            conn.close()
            print(f"  Tunnel established after {i+1}s on port {port}!")
            print(f"  market_data rows: {rows}")
            return tunnel, local_url
        except Exception as e:
            if i % 10 == 9:
                err = str(e)
                print(f"  {i+1}s: {err[:60]}")
    
    tunnel.terminate()
    try:
        tunnel.wait(timeout=5)
    except:
        tunnel.kill()
    return None

# --- Try to establish tunnel ---
print("=" * 60)
print("ESTABLISHING SSH TUNNEL TO RAILWAY POSTGRESQL")
print("=" * 60)

tunnel = None
local_url = None
for key in KEYS:
    for port in PORTS:
        result = try_tunnel(port, key)
        if result is not None:
            tunnel, local_url = result
            break
        # Small delay between attempts
        time.sleep(2)
    if tunnel is not None:
        break

if tunnel is None:
    print("\nERROR: All SSH tunnel attempts failed.")
    print("The Railway PostgreSQL is not accessible from this machine.")
    print("Alternative: deploy updated Dockerfile.worker and use railway run in a Docker container.")
    sys.exit(1)

print(f"\nSSH tunnel established! Using DB URL: {local_url[:40]}...")

# --- Capture pre-tournament DB state ---
print("\n" + "=" * 60)
print("PRE-TOURNAMENT DATABASE STATE")
print("=" * 60)

pre_state = {}
import psycopg2
conn = psycopg2.connect(local_url, connect_timeout=10)
cur = conn.cursor()
for table in ['strategies', 'strategy_evidence', 'paper_deployments', 
              'paper_orders', 'paper_sessions']:
    try:
        cur.execute(f'SELECT count(*) FROM {table}')
        pre_state[table] = cur.fetchone()[0]
        print(f"  {table}: {pre_state[table]} rows")
    except Exception as e:
        pre_state[table] = -1
        print(f"  {table}: ERROR - {e}")

# Check for existing "My Paper Deployment"
cur.execute("SELECT strategy_id FROM paper_deployments WHERE strategy_id ILIKE '%my%' OR strategy_id ILIKE '%manual%' OR strategy_id = 'my-paper-deployment'")
existing_deployments = cur.fetchall()
if existing_deployments:
    print(f"  Existing manual deployments found: {existing_deployments}")
else:
    print("  No existing manual deployments found")
cur.close()
conn.close()

# --- Run tournament ---
print("\n" + "=" * 60)
print("RUNNING PHASE 23 NIFTY-ONLY TOURNAMENT")
print("=" * 60)
print(f"  Tournament ID: phase23-production-20260916")
print(f"  Universe: nifty-only")
print(f"  Walk-forward folds: 5")
print(f"  Bootstrap iterations: 1000")
print(f"  Top-N deploy: 5")
print(f"  DB: Railway PostgreSQL (via SSH tunnel)")
print()

env = os.environ.copy()
env['MARKET_DATA_DB_URL'] = local_url
env['PAPER_TRADING_ENABLED'] = 'true'

tournament_script = os.path.join(os.getcwd(), 'run_tournament.py')
tournament_cmd = [
    sys.executable, tournament_script,
    '--universe', 'nifty-only',
    '--wf-folds', '5',
    '--bootstrap-n', '1000',
    '--top-n-deploy', '5',
    '--tournament-id', 'phase23-production-20260916',
]

print(f"Running: {tournament_cmd}")
print()

t0 = time.time()
tournament_proc = subprocess.Popen(
    tournament_cmd,
    env=env,
    stdout=sys.stdout,
    stderr=sys.stderr,
)
exit_code = tournament_proc.wait()
elapsed = time.time() - t0
print(f"\nTournament finished in {elapsed:.1f}s with exit code {exit_code}")

# --- Capture post-tournament DB state ---
print("\n" + "=" * 60)
print("POST-TOURNAMENT DATABASE STATE")
print("=" * 60)

post_state = {}
conn = psycopg2.connect(local_url, connect_timeout=10)
cur = conn.cursor()
for table in ['strategies', 'strategy_evidence', 'paper_deployments',
              'paper_orders', 'paper_sessions']:
    try:
        cur.execute(f'SELECT count(*) FROM {table}')
        post_state[table] = cur.fetchone()[0]
        delta = post_state[table] - pre_state.get(table, 0)
        print(f"  {table}: {post_state[table]} rows (delta: +{delta})")
    except Exception as e:
        print(f"  {table}: ERROR - {e}")

# Check PAPER_APPROVED strategies
try:
    cur.execute("""
        SELECT s.strategy_id, s.spec_name, s.spec_hash, ev.dataset_id, ev.created_at
        FROM strategies s
        JOIN strategy_evidence ev ON s.strategy_id = ev.strategy_id
        WHERE s.status = 'PAPER_APPROVED'
        ORDER BY ev.created_at DESC
    """)
    approved = cur.fetchall()
    print(f"\n  PAPER_APPROVED strategies: {len(approved)}")
    for row in approved:
        print(f"    {row[0]} | {row[1]} | dataset: {row[3]} | created: {row[4]}")
except Exception as e:
    print(f"\n  Error querying PAPER_APPROVED: {e}")

# Check evidence from this tournament
try:
    cur.execute("""
        SELECT strategy_id, evidence_type, dataset_id, created_at
        FROM strategy_evidence
        WHERE dataset_id LIKE 'tournament-%'
        ORDER BY created_at DESC
        LIMIT 20
    """)
    evidence = cur.fetchall()
    print(f"\n  Tournament evidence records (last 20): {len(evidence)}")
    for row in evidence:
        print(f"    {row[0]} | {row[1]} | {row[2]} | {row[3]}")
except Exception as e:
    print(f"\n  Error querying tournament evidence: {e}")

cur.close()
conn.close()

# --- Cleanup ---
print("\n" + "=" * 60)
print("CLEANUP")
print("=" * 60)
tunnel.terminate()
try:
    tunnel.wait(timeout=10)
    print("SSH tunnel terminated cleanly.")
except:
    tunnel.kill()
    print("SSH tunnel force-killed.")

print("\n" + "=" * 60)
print(f"DONE! Exit code: {exit_code}")
print("=" * 60)

sys.exit(exit_code)
