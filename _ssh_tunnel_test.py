import subprocess, time, os, sys, socket

# Manual SSH tunnel
key_path = os.path.expanduser('~/.ssh/railway_deploy_new')
ssh_cmd = (
    f'ssh -i {key_path} '
    '-L 5436:postgres.railway.internal:5432 '
    '-o StrictHostKeyChecking=no '
    '-o UserKnownHostsFile=/dev/null '
    '-o ConnectTimeout=30 '
    '-o ServerAliveInterval=10 '
    '-N -v '
    'ssh.railway.com'
)

print("Starting manual SSH tunnel to ssh.railway.com...")
tunnel = subprocess.Popen(
    ssh_cmd,
    shell=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1
)

for i in range(45):
    time.sleep(1)
    
    if tunnel.poll() is not None:
        stdout, stderr = tunnel.communicate()
        print(f"SSH exited at {i+1}s, code={tunnel.returncode}")
        print("SSH STDOUT:", stdout[:2000])
        print("SSH STDERR:", stderr[:3000])
        sys.exit(1)
    
    if i % 5 == 4:
        print(f"  {i+1}s: checking port 5436...")
        try:
            import psycopg2
            conn = psycopg2.connect(
                'postgresql://postgres:jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD@localhost:5436/railway',
                connect_timeout=3
            )
            cur = conn.cursor()
            cur.execute('SELECT count(*) FROM market_data')
            rows = cur.fetchone()[0]
            print(f"  DB OK! market_data rows={rows}")
            cur.close()
            conn.close()
            print("SSH_TUNNEL_READY")
            break
        except Exception as e:
            print(f"  DB: {str(e)[:100]}")

tunnel.terminate()
try:
    stdout, stderr = tunnel.communicate(timeout=5)
except:
    tunnel.kill()
    stdout, stderr = tunnel.communicate()
print("\nSSH tunnel stdout:", stdout[:500])
print("SSH tunnel stderr:", stderr[:500])
