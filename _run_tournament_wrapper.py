#!/usr/bin/env python3
"""
Tournament runner wrapper:
1. Starts SSH tunnel to Railway PostgreSQL
2. Verifies DB connection
3. Captures pre-tournament DB state
4. Runs run_tournament.py with the user's specified args (via MARKET_DATA_DB_URL env var)
5. Captures post-tournament DB state
6. Reports new records created
7. Cleans up tunnel
"""
import subprocess, time, sys, os

DB_PASSWORD = 'jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD'
TUNNEL_PORT = 15432
TUNNEL_URL = f'postgresql://postgres:{DB_PASSWORD}@localhost:{TUNNEL_PORT}/railway'
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

print("=" * 60)
print("PHASE 23 TOURNAMENT RUNNER (TUNNEL WRAPPER)")
print("=" * 60)

# ------------------------------------------------------------------ #
# Step 1: Start SSH tunnel to Railway PostgreSQL
# ------------------------------------------------------------------ #
print(f"\n[1/5] Starting SSH tunnel to Railway PostgreSQL (port {TUNNEL_PORT})...")
tunnel = subprocess.Popen(
    f'railway connect postgres --tunnel-only --port {TUNNEL_PORT}',
    shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
)

print(f"[2/5] Waiting 15s for tunnel to establish...")
time.sleep(15)

if tunnel.poll() is not None:
    stdout, stderr = tunnel.communicate()
    print("FATAL: Tunnel process exited early!")
    print("STDOUT:", stdout[:1000])
    print("STDERR:", stderr[:1000])
    sys.exit(1)
print("Tunnel established (PID=%d)." % tunnel.pid)

# ------------------------------------------------------------------ #
# Step 2: Verify DB connection + capture pre-state
# ------------------------------------------------------------------ #
print("\n[3/5] Verifying database connection and capturing pre-state...")
import psycopg2

conn = psycopg2.connect(TUNNEL_URL)
cur = conn.cursor()

cur.execute('SELECT count(*) FROM market_data')
md_rows = cur.fetchone()[0]
print(f"  market_data rows: {md_rows}")

cur.execute("SELECT DISTINCT symbol, timeframe FROM market_data WHERE symbol LIKE 'NSE:NIFTY%'")
nifty_data = cur.fetchall()
print(f"  NIFTY data available: {nifty_data}")

# Pre-state counts
cur.execute('SELECT count(*) FROM strategies')
pre_strategies = cur.fetchone()[0]
cur.execute('SELECT count(*) FROM strategy_evidence')
pre_evidence = cur.fetchone()[0]
cur.execute('SELECT count(*) FROM paper_deployments')
pre_deployments = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM strategy_evidence WHERE configuration_json->>'qualification_status' = 'PAPER_APPROVED'")
pre_paper_approved = cur.fetchone()[0]

# Check for existing "My Paper Deployment"
cur.execute("SELECT deployment_id, strategy_id, symbol, timeframe FROM paper_deployments LIMIT 50")
all_deployments = cur.fetchall()
print(f"  Existing paper_deployments ({pre_deployments}): {all_deployments}")

cur.execute("SELECT strategy_id, status FROM strategies LIMIT 50")
all_strategies = cur.fetchall()
print(f"  Existing strategies ({pre_strategies}): {all_strategies}")

cur.close()
conn.close()
print("  DB Connection: OK")

# ------------------------------------------------------------------ #
# Step 3: Run tournament
# ------------------------------------------------------------------ #
print("\n[4/5] Running Phase 23 tournament...")
print(f"  MARKET_DATA_DB_URL: PostgreSQL via SSH tunnel (localhost:{TUNNEL_PORT})")
print(f"  Tournament ID: phase23-production-20260916")
print(f"  Universe: nifty-only")
print(f"  Walk-forward: 5 folds")
print(f"  Bootstrap: 1000 iterations")
print(f"  Top-N deploy: 5")
print()
sys.stdout.flush()

env = os.environ.copy()
env['MARKET_DATA_DB_URL'] = TUNNEL_URL
env['PAPER_TRADING_ENABLED'] = 'true'

tournament_script = os.path.join(PROJECT_ROOT, 'run_tournament.py')
tournament_proc = subprocess.Popen(
    [sys.executable, tournament_script,
     '--universe', 'nifty-only',
     '--wf-folds', '5',
     '--bootstrap-n', '1000',
     '--top-n-deploy', '5',
     '--tournament-id', 'phase23-production-20260916'],
    env=env,
    cwd=PROJECT_ROOT,
    stdout=sys.stdout,
    stderr=sys.stderr,
)

tournament_proc.wait()
tournament_exit = tournament_proc.returncode

# ------------------------------------------------------------------ #
# Step 4: Capture post-state and report new records
# ------------------------------------------------------------------ #
print("\n[5/5] Capturing post-state...")
conn = psycopg2.connect(TUNNEL_URL)
cur = conn.cursor()

cur.execute('SELECT count(*) FROM strategies')
post_strategies = cur.fetchone()[0]
cur.execute('SELECT count(*) FROM strategy_evidence')
post_evidence = cur.fetchone()[0]
cur.execute('SELECT count(*) FROM paper_deployments')
post_deployments = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM strategy_evidence WHERE configuration_json->>'qualification_status' = 'PAPER_APPROVED'")
post_paper_approved = cur.fetchone()[0]

# New PAPER_APPROVED strategies
cur.execute("""
    SELECT s.strategy_id, s.status, e.configuration_json->>'candidate_id' as candidate_id,
           e.configuration_json->>'score' as score, e.configuration_json->>'qualification_status' as status
    FROM strategies s
    JOIN strategy_evidence e ON s.spec_hash = e.strategy_spec_hash
    WHERE e.configuration_json->>'qualification_status' = 'PAPER_APPROVED'
    AND e.created_at >= NOW() - INTERVAL '1 hour'
    ORDER BY e.created_at DESC
""")
new_paper_approved = cur.fetchall()
print(f"  New PAPER_APPROVED strategies: {len(new_paper_approved)}")
for row in new_paper_approved:
    print(f"    strategy_id={row[0]}, candidate_id={row[1]}, score={row[2]}, status={row[3]}")

# New deployments
cur.execute("""
    SELECT deployment_id, strategy_id, symbol, timeframe, status
    FROM paper_deployments
    WHERE created_at >= NOW() - INTERVAL '1 hour'
    ORDER BY created_at DESC
""")
new_deployments = cur.fetchall()
print(f"  New paper deployments: {len(new_deployments)}")
for row in new_deployments:
    print(f"    deployment_id={row[0]}, strategy_id={row[1]}, symbol={row[2]}, timeframe={row[3]}, status={row[4]}")

# Verify existing "My Paper Deployment" is unchanged
cur.execute("SELECT deployment_id, strategy_id, symbol, status FROM paper_deployments")
final_deployments = cur.fetchall()
print(f"  All deployments ({len(final_deployments)}): {final_deployments}")

cur.close()
conn.close()

# Cleanup tunnel
tunnel.terminate()
try:
    tunnel.communicate(timeout=10)
except:
    tunnel.kill()
    print("Tunnel force-killed.")

print(f"\n{'=' * 60}")
print(f"Tournament exit code: {tournament_exit}")
print(f"Strategies: {pre_strategies} -> {post_strategies} (delta: {post_strategies - pre_strategies})")
print(f"Evidence: {pre_evidence} -> {post_evidence} (delta: {post_evidence - pre_evidence})")
print(f"Deployments: {pre_deployments} -> {post_deployments} (delta: {post_deployments - pre_deployments})")
print(f"PAPER_APPROVED: {pre_paper_approved} -> {post_paper_approved} (delta: {post_paper_approved - pre_paper_approved})")
print(f"{'=' * 60}")

sys.exit(tournament_exit)
