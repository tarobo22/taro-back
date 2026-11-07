"""Publish immutable CI artifacts; credentials are supplied only by GITHUB_TOKEN."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

component, filename = sys.argv[1:]
repos = {'api': 'tarobo22/taro-back', 'web': 'tarobo22/taro-raect'}
assets = {'api': 'api-image.tar.gz', 'web': 'web.tar.gz'}
repo = os.environ['GITHUB_REPOSITORY']
sha = os.environ['GITHUB_SHA']
run_id, attempt = os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT']
if (repo != repos[component] or os.environ['GITHUB_REF'] != 'refs/heads/main'
        or not re.fullmatch('[a-f0-9]{40}', sha) or not run_id.isdigit() or not attempt.isdigit()):
    raise SystemExit('Only the expected main branch can publish this component')
archive = Path(filename)
if archive.name != assets[component]:
    raise SystemExit('Unexpected artifact filename')
digest = hashlib.sha256()
with archive.open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
tag = f'{component}-{sha}-{run_id}-{attempt}'
manifest = archive.parent / 'deploy-manifest.json'
manifest.write_text(json.dumps(dict(schema=1, component=component, repository=repo,
    sha=sha, tag=tag, asset=archive.name, sha256=digest.hexdigest(), size=archive.stat().st_size), indent=2) + '\n')
notes = archive.parent / 'release-notes.md'
notes.write_text(f'Validated {component} release.\n\nSource: {sha}\n\n'
                 'Deployment is complete only when the workflow public health/status check passes.\n')
subprocess.run(['gh', 'release', 'create', tag, str(archive), str(manifest), '--repo', repo,
    '--target', sha, '--title', f'Yeoun {component} {sha[:7]}', '--notes-file', str(notes), '--draft'], check=True)
subprocess.run(['gh', 'release', 'edit', tag, '--repo', repo, '--draft=false', '--latest'], check=True)
with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
    output.write(f'tag={tag}\n')

