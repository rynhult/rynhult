#!/usr/bin/python3
"""Offline qualification-only providers; never execute on the development host."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, '/opt/qualification')
from linux_phase1_probe import require_disposable

require_disposable()
provider = Path(sys.argv[0]).name
assert provider in ('claude', 'codex')
args = sys.argv[1:]
home = Path.home()
store = home / '.qualification-auth'
store.mkdir(mode=0o700, exist_ok=True)
auth = store / provider
if args[:1] in (['auth'], ['login']):
    with (store / 'invocations.jsonl').open('a') as trace:
        trace.write(json.dumps({'provider': provider, 'uid': os.getuid(), 'home': str(home), 'action': args[0]}) + '\n')
if args == ['--version']:
    print('codex-cli 0.163.0' if provider == 'codex' else '2.1.120 (Claude Code qualification fake)')
    raise SystemExit(0)
if args[:2] in (['auth', 'status'], ['login', 'status']):
    if provider == 'claude':
        print(json.dumps({'loggedIn': auth.exists(), 'authMethod': 'qualification-fake'}))
    else:
        print('Logged in using qualification fake' if auth.exists() else 'Not logged in')
    raise SystemExit(0 if auth.exists() else 1)
if args[:2] in (['auth', 'login'], ['login', '--device-auth']):
    print('https://login.example.com/' + provider, flush=True)
    if provider == 'claude':
        if sys.stdin.readline().strip() != 'qualification-code':
            print('Invalid qualification code', flush=True)
            raise SystemExit(1)
    else:
        print('Enter this one-time code ABCD-12345', flush=True)
        time.sleep(1)
    auth.write_text('qualification fake; no real provider credential\n')
    auth.chmod(0o600)
    print('Successfully logged in', flush=True)
    raise SystemExit(0)
run = os.environ.get('ACC_RUN_DIR')
if run:
    try:
        Path('/home/desktop-alex/public-fixture').read_text()
        readable = True
    except OSError:
        readable = False
    Path(run, 'qualification-process.json').write_text(json.dumps({'uid': os.getuid(), 'pid': os.getpid(),
        'home': str(home), 'provider': provider, 'owner_home_readable': readable}))
root = Path('/opt/qualification/resources')
os.environ['FAKE_HOOK'] = str(root.resolve() / 'bin/acc-hook')
os.execv('/usr/bin/python3', ['python3', '/opt/qualification/fake-' + provider, *args])
