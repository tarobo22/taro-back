"""Make GitHub Actions fail unless the public server has activated this release."""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

base = os.environ.get('YEOUN_PUBLIC_URL', '').strip().rstrip('/')
config_error = 'Set YEOUN_PUBLIC_URL to the public HTTPS origin.'
try:
    origin = urllib.parse.urlsplit(base)
    port = origin.port
except ValueError:
    raise SystemExit(config_error) from None
if (origin.scheme != 'https' or not origin.hostname or origin.username is not None
        or origin.path or origin.query or origin.fragment
        or port == 0 or origin.netloc.endswith(':')):
    raise SystemExit(config_error)
if sys.argv[1:] == ['--check-config']:
    raise SystemExit(0)
component, tag = sys.argv[1:]
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
