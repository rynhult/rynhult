"""Fixed synthetic payload. Success is NOT provider or Chrome sandbox qualification."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def require_guard_refusal(result):
    assert result.returncode == 2
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def main():
    assert os.getuid() != 0
    assert 'NoNewPrivs:\t1' in Path('/proc/self/status').read_text()
    assert len(Path('/proc/self/uid_map').read_text().splitlines()) >= 2
    os.umask(0o077)
    secret = sys.stdin.buffer.read(65537)
    assert secret == b'EXAMPLE-SESSION-TOKEN'
    token = Path('/run/credentials/token')
    token.write_bytes(secret)
    assert token.stat().st_mode & 0o777 == 0o600
    for helper in ('acc-guard', 'acc-hook'):
        try:
            Path('/qualification', helper).open('ab').close()
        except OSError:
            pass
        else:
            raise RuntimeError('mandatory helper is mutable; STOP')
    # A missing policy must fail closed. This is a helper check, not a
    # provider-invoked pre-tool block. Spool is disposable container tmpfs.
    run = Path('/tmp/run')
    run.mkdir()
    env = dict(os.environ, ACC_RUN_DIR=str(run), ACC_SESSION_ID='qualification')
    guard = subprocess.run(['/usr/bin/python3', '-I', '/qualification/acc-guard', 'codex'],
                           input=b'{"tool_name":"Bash","tool_input":{"command":"true"}}',
                           env=env, capture_output=True, timeout=10)
    require_guard_refusal(guard)
    hook = subprocess.run(['/bin/sh', '/qualification/acc-hook', 'codex', 'PostToolUse'],
                          input=b'{}', env=env, capture_output=True, timeout=10)
    assert hook.returncode == 0 and 'PostToolUse' in (run / 'events.jsonl').read_text()
    # No disabling profiles or browser sandboxes to obtain success.
    bwrap = shutil.which('bwrap')
    assert bwrap, 'bubblewrap absent'
    subprocess.run([bwrap, '--unshare-user', '--unshare-pid', '--ro-bind', '/', '/',
                    '--proc', '/proc', '--dev', '/dev', '/bin/true'], check=True, timeout=10)
    chrome = shutil.which('chromium') or shutil.which('google-chrome')
    assert chrome, 'Chrome absent'
    subprocess.run([chrome, '--headless', '--disable-gpu', '--user-data-dir=/tmp/chrome',
                    '--dump-dom', 'about:blank'], check=True, capture_output=True, timeout=20)
    token.unlink()


if __name__ == '__main__':
    main()
