"""Trusted image bootstrap; no host-home discovery or provider login writes."""
import json
import socket
import subprocess
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
            if relative:
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
