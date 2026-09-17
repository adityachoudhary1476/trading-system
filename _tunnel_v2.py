import subprocess, time, os, sys

key_path = os.path.expanduser('~/.ssh/railway_deploy_new')
DB_PASSWORD = 'jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD'
TUNNEL_PORT = 5439
LOCAL_DB_URL = f'postgresql://postgres:{DB_PASSWORD}@localhost:{TUNNEL_PORT}/railway'

# Try WITHOUT -N, using 'sleep' as remote command to keep session alive
ssh_cmd = (
    f'ssh -i {key_path} '
    f'-L {TUNNEL_PORT}:postgres.railway.internal:5432 '
    '-o StrictHostKeyChecking=no '
    '-o UserKnownHostsFile=/dev/null '
    '-o ConnectTimeout=30 '
    'ssh.railway.com '
    '"sleep 600"'
)

print(f"Starting SSH tunnel on port {TUNNEL_PORT} (with sleep command)...")
tunnel = subprocess.Popen(
    ssh_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
)
print(f"SSH PID: {tunnel.pid}")

for i in range(60):
    time.sleep(1)
    if tunnel.poll() is not None:
        stdout, stderr = tunnel.communicate()
        print(f"SSH exited at {i+1}s, code={tunnel.returncode}")
        print(f"STDOUT: {stdout[:500]}")
        print(f"STDERR: {stderr[:800]}")
        sys.exit(1)
    try:
        import psycopg2
        conn = psycopg2.connect(LOCAL_DB_URL, connect_timeout=3)
        cur = conn.cursor()
        cur.execute('SELECT count(*) FROM market_data')
        rows = cur.fetchone()[0]
        cur.close()
        conn.close()
        print(f"DB OK after {i+1}s! market_data rows: {rows}")
        break
    except Exception as e:
        if i % 5 == 4:
            print(f"  {i+1}s: {str(e)[:80]}")
else:
    print("Tunnel failed after 60s")
    tunnel.terminate()
    sys.exit(1)

# Keep tunnel alive - terminate after use
print(f"\nTunnel working on port {TUNNEL_PORT}")
print("TUNNEL_READY")

# Clean up
tunnel.terminate()
try:
    tunnel.communicate(timeout=5)
except:
    tunnel.kill()
print("Tunnel cleaned up.")
