"""Pull public, CI-verified Yeoun releases. No GitHub or AWS credentials required."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

ROOT = Path('/opt/yeoun')
STATE = Path('/var/lib/yeoun-deploy')
REPOS = {'api': 'tarobo22/taro-back', 'web': 'tarobo22/taro-raect'}
ASSETS = {'api': 'api-image.tar.gz', 'web': 'web.tar.gz'}
COMPOSE = ['docker', 'compose', '--env-file', '/etc/yeoun/runtime.env',
           '-f', str(ROOT / 'deploy/lightsail/compose.yaml'), '--project-directory', str(ROOT)]


def validate_manifest(item, component):
    if (item.get('schema') != 1 or item.get('component') != component
            or item.get('repository') != REPOS[component] or item.get('asset') != ASSETS[component]):
        raise ValueError('Unexpected release repository, component or artifact')
    sha = item.get('sha', '')
    if not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise ValueError('Invalid source commit')
    if not re.fullmatch(re.escape(component + '-' + sha) + r'-[0-9]+-[0-9]+', item.get('tag', '')):
        raise ValueError('Release does not match its source commit')
    if not re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', '')):
        raise ValueError('Invalid checksum')
    if type(item.get('size')) is not int or not 0 < item['size'] < 2_000_000_000:
        raise ValueError('Invalid archive size')
    return item


def verify_archive(path, expected_hash, expected_size):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    if path.stat().st_size != expected_size or digest.hexdigest() != expected_hash:
        raise ValueError('Release checksum or length mismatch')


def extract_web_archive(path, target):
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        if len(members) > 20_000 or sum(m.size for m in members) > 512_000_000:
            raise ValueError('Web archive exceeds extraction limits')
        for member in members:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or '..' in name.parts or '\\' in member.name
                    or not (member.isfile() or member.isdir())):
                raise ValueError('Unsafe web archive entry')
        # Write only file bytes, never tar owner IDs, permission bits or links.
        target.mkdir(parents=True, exist_ok=True)
        for member in members:
            destination = target / member.name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open('wb') as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(0o644)
    if not (target / 'index.html').is_file():
        raise ValueError('Missing web entry page')


def request(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        'User-Agent': 'yeoun-release-pull/1', 'Cache-Control': 'no-cache'}), timeout=30)


def download(item, directory):
    destination = directory / item['asset']
    if shutil.disk_usage(directory).free < item['size'] * 5 + 2_000_000_000:
        raise RuntimeError('Insufficient free disk space; active release unchanged')
    url = f"https://github.com/{item['repository']}/releases/download/{item['tag']}/{item['asset']}"
    with request(url) as response, destination.open('wb') as output:
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > item['size']:
                raise ValueError('Download exceeds manifest size')
            output.write(chunk)
    verify_archive(destination, item['sha256'], item['size'])
    return destination


def run(args, timeout=180):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    if result.returncode:
        # Never copy container environment, provider values or SQL errors into public status/logs.
        raise RuntimeError(f'Command {args[0]} failed with exit {result.returncode}')
    return result.stdout.decode('utf-8', 'replace').strip()


def wait_url(url, timeout=60):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            with request(url) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(2)
    raise RuntimeError('Deployment health check did not pass')


def write_json(path, data, mode=0o644):
    temporary = path.with_suffix('.next')
    temporary.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    temporary.chmod(mode)
    temporary.replace(path)


def deploy_web(item, archive, public_base):
    www = ROOT / 'www'
    target = www / 'releases' / item['tag']
    extract_web_archive(archive, target)
    # Hashed assets remain available to browsers that opened the previous release.
    if (target / 'assets').is_dir():
        shutil.copytree(target / 'assets', www / 'assets', dirs_exist_ok=True)
    next_link = www / 'next'
    next_link.unlink(missing_ok=True)
    next_link.symlink_to(Path('releases') / item['tag'])
    next_link.replace(www / 'current')
    wait_url(public_base + '/')


def deploy_api(item, archive, public_base):
    image = 'yeoun-api:' + item['sha']
    run(['docker', 'load', '-i', str(archive)], timeout=300)
    run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'])
    backup = STATE / (item['tag'] + '.dump')
    # A local pre-migration backup is retained; no automatic DB downgrade is performed.
    with backup.open('wb') as output:
        backup.chmod(0o600)
        result = subprocess.run(COMPOSE + ['exec', '-T', 'db', 'pg_dump', '-U', 'yeoun', '-Fc', 'yeoun'],
                                stdout=output, stderr=subprocess.PIPE, timeout=120)
    if result.returncode:
        raise RuntimeError('Pre-migration backup failed')
    common = ['--network', 'yeoun-demo_default', '--env-file', '/etc/yeoun/api.env', '--memory', '192m']
    run(['docker', 'run', '--rm', *common, image, 'python', '-m', 'api.migrate'])
    candidate = 'yeoun-api-candidate'
    # A previous interrupted check may have left this named, disposable candidate.
    subprocess.run(['docker', 'rm', '-f', candidate], capture_output=True, check=False)
    try:
        run(['docker', 'run', '-d', '--name', candidate, *common,
             '-p', '127.0.0.1:18000:8000', image, 'python', '-c',
             'import uvicorn; from api.main import create_app; '
             'uvicorn.run(create_app(workers_enabled=False), host="0.0.0.0", port=8000, access_log=False)'])
        wait_url('http://127.0.0.1:18000/health')
    finally:
        subprocess.run(['docker', 'rm', '-f', candidate], capture_output=True, check=False)
    # Only a healthy candidate can replace the running API.
    runtime = Path('/etc/yeoun/runtime.env')
    next_runtime = runtime.with_suffix('.next')
    next_runtime.write_text('YEOUN_API_IMAGE=' + image + '\n', encoding='utf-8')
    next_runtime.chmod(0o600)
    next_runtime.replace(runtime)
    run(COMPOSE + ['up', '-d', '--no-deps', '--no-build', 'api'])
    wait_url(public_base + '/api/health', timeout=90)
    active_image = run(['docker', 'inspect', 'yeoun-demo-api-1', '--format', '{{.Config.Image}}'])
    if active_image != image:
        raise RuntimeError('Running API does not match the released image')


def main():
    import fcntl  # Linux service only; pure archive validation is portable.
    host = os.environ.get('YEOUN_HOST', '').strip()
    if not re.fullmatch(r'[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+', host):
        raise SystemExit('Set YEOUN_HOST to the public hostname in /etc/yeoun/site.env.')
    public_base = 'https://' + host
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        status_file = ROOT / 'www/deployment-status.json'
        status = json.loads(status_file.read_text()) if status_file.exists() else {}
        failed = False
        for component, repository in REPOS.items():
            item = None
            try:
                try:
                    with request(f'https://github.com/{repository}/releases/latest/download/deploy-manifest.json') as response:
                        item = validate_manifest(json.loads(response.read(16_384)), component)
                except urllib.error.HTTPError as error:
                    if error.code == 404:  # No pipeline release published yet.
                        continue
                    raise
                if status.get(component, {}).get('tag') == item['tag']:
                    continue
                staging = STATE / item['tag']
                staging.mkdir(exist_ok=True)
                archive = download(item, staging)
                (deploy_api if component == 'api' else deploy_web)(item, archive, public_base)
                status[component] = {'sha': item['sha'], 'tag': item['tag'], 'status': 'deployed',
                                     'deployed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
                status.pop(component + '_error', None)
                write_json(status_file, status)
                print(f"Deployed {component} {item['sha']}", flush=True)
            except Exception as error:
                failed = True
                status[component + '_error'] = {'type': type(error).__name__,
                                               'tag': item.get('tag') if isinstance(item, dict) else None}
                write_json(status_file, status)
                print(f'{component} deployment failed: {type(error).__name__}: {error}', file=sys.stderr, flush=True)
        if failed:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
