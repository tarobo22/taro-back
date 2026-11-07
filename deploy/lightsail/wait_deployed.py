"""Make GitHub Actions fail unless the public server has activated this release."""
import json
import sys
import time
import urllib.error
import urllib.request

component, tag = sys.argv[1:]
base = 'https://yeoun-43-201-209-249.sslip.io'
for attempt in range(60):
    try:
        req = urllib.request.Request(base + '/deployment-status.json', headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=15) as response:
            status = json.load(response)
        if status.get(component + '_error', {}).get('tag') == tag:
            raise SystemExit('Server rejected this release. Check yeoun-deploy service logs.')
        current = status.get(component, {})
        if current.get('tag') == tag and current.get('status') == 'deployed':
            for path in ['/', '/api/health', '/api/v2/counseling/capabilities']:
                with urllib.request.urlopen(base + path, timeout=15) as response:
                    if response.status != 200:
                        raise SystemExit('Public smoke check failed')
            print(f'Live {component}: {current["sha"]}')
            break
    except (OSError, ValueError, urllib.error.URLError):
        pass
    time.sleep(10)
else:
    raise SystemExit('Server did not activate this release within ten minutes')
