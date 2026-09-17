import subprocess, time, os, sys

PORT = 15433
URL = f'postgresql://postgres:jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD@localhost:{PORT}/railway'

print(f"Starting tunnel on port {PORT}...")
tunnel = subprocess.Popen(
    f'railway connect postgres --tunnel-only --port {PORT}',
    shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
)

for i in range(30):
    time.sleep(1)
    if tunnel.poll() is not None:
        stdout, stderr = tunnel.communicate()
        print(f"Tunnel exited at {i+1}s with code {tunnel.returncode}")
        print("STDOUT:", stdout[:1000])
        print("STDERR:", stderr[:1000])
        sys.exit(1)
    # Try connecting
    try:
        import psycopg2
        conn = psycopg2.connect(URL, connect_timeout=3)
        cur = conn.cursor()
        cur.execute('SELECT count(*) FROM market_data')
        rows = cur.fetchone()[0]
        print(f"DB OK after {i+1}s! market_data rows: {rows}")
        conn.close()
        break
    except Exception as e:
        if i % 5 == 0:
            print(f"  {i+1}s: waiting... ({e})")

tunnel.terminate()
try:
    tunnel.communicate(timeout=5)
except:
    tunnel.kill()
print("Done.")
