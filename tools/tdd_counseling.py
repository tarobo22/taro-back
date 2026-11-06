"""Record real test executions and immutable test hashes for each rewrite slice."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--slice', required=True)
    parser.add_argument('--phase', choices=('red', 'green', 'regression'), required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    root = Path.cwd()
    folder = root / 'docs/quality/counseling-tdd-20261107/evidence'
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f'{args.slice}-{args.phase}'
    if stem.with_suffix('.json').exists():
        raise SystemExit('Evidence already exists; use a new slice name rather than overwriting it.')
    tests = {p.relative_to(root).as_posix(): digest(p)
             for area in ('api/tests', 'tests') if (root / area).exists()
             for p in (root / area).rglob('*')
             if p.is_file() and p.suffix in ('.py', '.ts', '.tsx', '.mjs', '.json')}
    red = folder / f'{args.slice}-red.json'
    if args.phase == 'green' and red.exists():
        previous = json.loads(red.read_text(encoding='utf-8'))['tests_sha256']
        if any(tests.get(name) != sha for name, sha in previous.items()):
            raise SystemExit('A test changed after RED. Stop rather than silently changing the contract.')
    started = datetime.now(timezone.utc).isoformat()
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1', 'NO_COLOR': '1'}
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    stem.with_suffix('.txt').write_bytes(completed.stdout)
    record = {'slice': args.slice, 'phase': args.phase, 'started_at': started,
              'finished_at': datetime.now(timezone.utc).isoformat(), 'parent_commit': head,
              'command': command, 'exit_code': completed.returncode,
              'output_sha256': hashlib.sha256(completed.stdout).hexdigest(),
              'tests_sha256': tests}
    stem.with_suffix('.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    sys.stdout.buffer.write(completed.stdout)
    valid = completed.returncode == (1 if args.phase == 'red' else 0)
    if not valid:
        raise SystemExit(completed.returncode or 2)


if __name__ == '__main__':
    main()
