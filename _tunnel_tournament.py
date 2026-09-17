import subprocess, time, os, sys, threading

DB_PASSWORD = 'jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD'
PORT = 5444
LOCAL_DB_URL = f'postgresql://postgres:{DB_PASSWORD}@localhost:{PORT}/railway'
key_path = '~/.ssh/railway_deploy_new'

# Try with dev.new command (as suggested by the error message)
ssh_cmd = (
    f'ssh -i {key_path} -L {PORT}:postgres.railway.internal:5432 '
    '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null '
    '-o ConnectTimeout=30 -o ServerAliveInterval=10 '
    f'ssh.railway.com dev.new'
)

print(f"Starting SSH tunnel (port {PORT}) with 'dev.new' command...")
tunnel = subprocess.Popen(
    ssh_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
)

# Read stderr in a thread to capture SSH debug output
stderr_lines = []
def stderr_reader():
    while True:
        line = tunnel.stderr.readline()
        if not line:
            break
        stderr_lines.append(line)
        print(f"[SSH] {line.rstrip()}", flush=True)

t = threading.Thread(target=stderr_reader, daemon=True)
t.start()

for i in range(60):
    time.sleep(1)
    if tunnel.poll() is not None:
        print(f"SSH exited at {i+1}s (code={tunnel.returncode})")
        break
    try:
        import psycopg2
        conn = psycopg2.connect(LOCAL_DB_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute('SELECT count(*) FROM market_data')
        rows = cur.fetchone()[0]
        cur.close()
        conn.close()
        print(f"DB OK after {i+1}s! rows={rows}")
        print("TUNNEL_WORKS")

        # Now run the actual tournament
        print("\n--- Starting tournament ---")
        env = os.environ.copy()
        env['MARKET_DATA_DB_URL'] = LOCAL_DB_URL
        env['PAPER_TRADING_ENABLED'] = 'true'

        proc = subprocess.Popen(
            [sys.executable, 'run_tournament.py',
             '--universe', 'nifty-only', '--wf-folds', '5',
             '--bootstrap-n', '1000', '--top-n-deploy', '5',
             '--tournament-id', 'phase23-production-20260916'],
            env=env, cwd=os.getcwd(),
            stdout=sys.stdout, stderr=sys.stderr
        )
        exit_code = proc.wait()
        print(f"\n--- Tournament finished (exit={exit_code}) ---")

        tunnel.terminate()
        sys.exit(exit_code)
    except Exception as e:
        msg = str(e)
        if "refused" in msg:
            tag = "REFUSED"
        elif "timeout" in msg.lower():
            tag = "TIMEOUT"
        else:
            tag = msg[:50]
        if i % 10 == 9:
            print(f"  {i+1}s: {tag}", flush=True)

print("\nTunnel failed. Cleaning up.")
tunnel.terminate()
t.join(timeout=5)
sys.exit(1)
