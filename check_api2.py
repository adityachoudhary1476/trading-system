import urllib.request, json, time

# Use Vercel proxy with DB_ACCESS_TOKEN bypass
url = 'https://tradingsystem-zeta.vercel.app/api/paper/autonomous/portfolio'
token = 'jbWwmUMZIDGLRsHbmevHRdrLalxPBcVD'

for i in range(3):
    try:
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            p = data.get('portfolio', {})
            print(f'Attempt {i}:')
            for key in ['data_source', 'stale', 'updated_at', 'positions', 'open_position_count', 'available_count', 'status', 'bot_id', 'strategies']:
                val = p.get(key)
                if isinstance(val, dict):
                    print(f'  {key}: {json.dumps(val)}')
                elif isinstance(val, list):
                    print(f'  {key}: [{len(val)} items]')
                else:
                    print(f'  {key}: {val}')
            actions = p.get('actions', [])
            print(f'  actions_count: {len(actions)}')
            if actions:
                print(f'  last_action: {actions[-1].get("action")} at {actions[-1].get("timestamp")}')
            print()
            if p.get('positions'):
                break
    except Exception as e:
        print(f'Attempt {i} failed: {e}')
        time.sleep(2)
