#!/usr/bin/python3 -I
"""Trusted image bootstrap; no host-home discovery or provider login writes."""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path


def prepare(root=Path('/')):
    for name in json.loads((root / 'opt/acc/state-directories.json').read_text()):
        if not name.startswith('/tmp/acc-state/') or '..' in Path(name).parts:
            raise ValueError('invalid image state directory')
        (root / name.lstrip('/')).mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in json.loads((root / 'opt/acc/state-files.json').read_text()):
        if not name.startswith('/tmp/acc-state/') or '..' in Path(name).parts:
            raise ValueError('invalid image state file')
        (root / name.lstrip('/')).touch(mode=0o600, exist_ok=True)
    env = dict(HOME='/home/acc', CLAUDE_CONFIG_DIR='/tmp/acc-state/claude',
               COPILOT_HOME='/tmp/acc-state/copilot', XDG_CONFIG_HOME='/tmp/acc-state/config')
    contract = root / 'run/acc-container/contract.json'
    if contract.is_file():
        value = json.loads(contract.read_text())
        if value.get('integrations') == 'readonly-v1':
            relative = {'claude': 'claude/.credentials.json', 'copilot': 'copilot/config.json', 'codex': None}[value['provider']]
            credentials = launch_credentials(root) if value['provider'] == 'claude' else {}
            env.update(credentials)
            if relative and not credentials:
                # This is fresh private tmpfs, before any provider/agent code starts.
                (root / 'tmp/acc-state' / relative).symlink_to('/run/acc-credentials/auth.json')
            if value.get('egress'):
                env.update(HTTPS_PROXY='http://127.0.0.1:18765', https_proxy='http://127.0.0.1:18765',
                           HTTP_PROXY='http://127.0.0.1:18765', http_proxy='http://127.0.0.1:18765',
                           NO_PROXY='', no_proxy='')
    return env


def start_bridge():
    # Bound/listening before exec: no provider-start race. Only public env values
    # go to this child, before the payload's session.env is sourced.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 18765))
        listener.listen(8)
        subprocess.Popen(['/usr/bin/python3', '-I', '-B', '/opt/acc/runtime/egress_bridge.py', str(listener.fileno())],
                         pass_fds=(listener.fileno(),), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'})


def launch_credentials(root=Path('/')):
    path = root / 'run/acc-credentials/auth.json'
    if not path.is_file():
        return {}
    value = json.loads(path.read_text())
    return value.get('launch_env', {})


def auth_mode(root=Path('/')):
    """Probe the pinned CLI with the immutable launch settings; return no auth JSON."""
    contract = json.loads((root / 'run/acc-container/contract.json').read_text())
    if contract.get('provider') != 'claude':
        return 'unknown'
    env = dict(os.environ)
    env.update(HOME='/home/acc', CLAUDE_CONFIG_DIR='/tmp/acc-state/claude',
               XDG_CONFIG_HOME='/tmp/acc-state/config')
    env.update(launch_credentials(root))
    args = contract.get('auth_args', [])
    settings = []
    for index, arg in enumerate(args[:-1]):
        if arg in ('--settings', '--setting-sources'):
            settings += [arg, args[index + 1]]
    try:
        result = subprocess.run([contract['auth_executable'], *settings, 'auth', 'status'],
                                env=env, cwd=contract['checkout'], capture_output=True, text=True, timeout=5)
        value = json.loads(result.stdout) if result.returncode == 0 else {}
        # Keep classification in sync with acc.providers.claude_auth; image runtime is stdlib-only.
        method = value.get('authMethod')
        if value.get('loggedIn') is not True:
            return 'unknown'
        reported = {'api_key': 'api_key', 'api_key_helper': 'api_key', 'third_party': 'cloud',
                    'claude.ai': 'subscription', 'oauth_token': 'subscription'}.get(method, 'unknown')
        supplied = ('api_key' if env.get('ANTHROPIC_API_KEY') else 'cloud' if any(env.get(k) == '1' for k in (
            'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')) else 'unknown')
        return reported if reported not in ('api_key', 'cloud') or reported == supplied else 'unknown'
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 'unknown'


if __name__ == '__main__':
    try:
        mode = auth_mode()
    except (OSError, ValueError, KeyError):
        mode = 'unknown'
    sys.stdout.write(json.dumps({'auth_mode': mode}) + '\n')
