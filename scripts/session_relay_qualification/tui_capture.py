"""Isolated native TUI wire capture; run only inside probe.sandbox, never directly."""
import base64
import fcntl
import hashlib
import json
import os
import pty
import select
import signal
import socket
import subprocess
import termios
import threading
import time
from pathlib import Path


def main():
    assert not Path('/srv/acc').exists()
    assert set(name for _, name in socket.if_nameindex()) == {'lo'}
    home = Path(os.environ['HOME'])
    assert str(home) == '/home/probe'
    codex_home = Path(os.environ['CODEX_HOME'])
    assert not any(codex_home.iterdir())
    path = '/evidence/native-tui.sock'
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    os.chmod(path, 0o600)
    server.listen(1)
    server.settimeout(10)
    captured = []
    def receive():
        try:
            client, _ = server.accept()
            with client:
                client.settimeout(10)
                header = b''
                while b'\r\n\r\n' not in header and len(header) < 8192:
                    header += client.recv(1024)
                captured.append({'upgrade': header.decode(errors='replace')})
                key = next(line.split(b':', 1)[1].strip() for line in header.split(b'\r\n')
                           if line.lower().startswith(b'sec-websocket-key:'))
                accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                client.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                def exact(count):
                    data = b''
                    while len(data) < count:
                        chunk = client.recv(count - len(data))
                        if not chunk:
                            raise EOFError()
                        data += chunk
                    return data
                def send(value):
                    data = json.dumps(value).encode()
                    size = len(data)
                    prefix = bytes([0x81, size]) if size < 126 else bytes([0x81, 126]) + size.to_bytes(2, 'big')
                    client.sendall(prefix + data)
                for _ in range(32):
                    flags, size = exact(2)
                    mask = bool(size & 128)
                    size &= 127
                    if size in (126, 127):
                        size = int.from_bytes(exact(2 if size == 126 else 8), 'big')
                    if size > 1 << 20 or not mask:
                        break
                    masking = exact(4)
                    data = bytes(value ^ masking[index % 4] for index, value in enumerate(exact(size)))
                    if flags & 15 == 8:
                        break
                    value = json.loads(data)
                    captured.append(value)
                    if 'id' not in value:
                        continue
                    if value.get('method') == 'initialize':
                        send({'id': value['id'], 'result': {'userAgent': 'fixture', 'codexHome': '/home/probe/codex',
                            'platformOs': 'linux', 'platformFamily': 'unix'}})
                    elif value.get('method') == 'account/read':
                        send({'id': value['id'], 'result': {'account': None, 'requiresOpenaiAuth': False}})
                    elif value.get('method') == 'config/read':
                        send({'id': value['id'], 'result': {'config': {'model': 'fixture-model', 'model_provider': 'fixture',
                            'approval_policy': 'on-request', 'sandbox_mode': 'workspace-write'}, 'origins': {}}})
                    elif value.get('method') == 'configRequirements/read':
                        send({'id': value['id'], 'result': {'requirements': {'allowedApprovalPolicies': ['on-request'],
                            'allowedSandboxModes': ['workspace-write'], 'allowRemoteControl': False}}})
                    elif value.get('method') == 'model/list':
                        send({'id': value['id'], 'result': {'data': [{'id': 'fixture-model', 'model': 'fixture-model', 'displayName': 'Fixture', 'description': 'Offline synthetic model', 'hidden': False, 'isDefault': True, 'defaultReasoningEffort': 'medium', 'supportedReasoningEfforts': [{'reasoningEffort': 'medium', 'description': 'Fixture'}]}], 'nextCursor': None}})
                    elif value.get('method') == 'thread/read':
                        native = '11111111-1111-4111-8111-111111111111'
                        send({'id': value['id'], 'result': {'thread': {'id': native, 'sessionId': native,
                            'projectId': None, 'cliVersion': '0.167.1', 'createdAt': 1, 'updatedAt': 1,
                            'cwd': '/home/probe', 'ephemeral': True, 'modelProvider': 'fixture', 'preview': '',
                            'source': 'appServer', 'status': {'type': 'idle'}, 'turns': []}}})
                    elif value.get('method') == 'thread/resume':
                        native = '11111111-1111-4111-8111-111111111111'
                        send({'id': value['id'], 'result': {'approvalPolicy': 'on-request', 'approvalsReviewer': 'user',
                            'cwd': '/home/probe', 'model': 'fixture-model', 'modelProvider': 'fixture',
                            'sandbox': {'type': 'workspaceWrite'}, 'thread': {'id': native, 'sessionId': native,
                            'projectId': None, 'cliVersion': '0.167.1', 'createdAt': 1, 'updatedAt': 1,
                            'cwd': '/home/probe', 'ephemeral': True, 'modelProvider': 'fixture', 'preview': '',
                            'source': 'appServer', 'status': {'type': 'idle'}, 'turns': []}}})
                    else:
                        send({'id': value['id'], 'error': {'code': -32001, 'message': 'Unqualified native request refused'}})
        except (TimeoutError, EOFError, OSError):
            pass
    thread = threading.Thread(target=receive)
    thread.start()
    master, slave = pty.openpty()
    import struct
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 120, 0, 0))
    def terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    process = subprocess.Popen(['/usr/bin/codex', '--remote', 'unix://' + path,
        '--no-alt-screen', 'resume', '11111111-1111-4111-8111-111111111111'], stdin=slave, stdout=slave,
        stderr=slave, env={**os.environ, 'TERM': 'xterm-256color'}, preexec_fn=terminal, close_fds=True)
    os.close(slave)
    output = b''
    try:
        started = time.monotonic()
        deadline = started + 12
        sent = False
        submitted = False
        while process.poll() is None and time.monotonic() < deadline and len(output) < 1 << 20:
            if not sent and time.monotonic() - started > 2:
                os.write(master, b'native-tui-fixture')
                sent = True
            if sent and not submitted and time.monotonic() - started > 3:
                os.write(master, b'\r')
                submitted = True
            if select.select([master], [], [], .1)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                output += data
                if b'\x1b[6n' in data:
                    os.write(master, b'\x1b[1;1R')
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        os.close(master)
        server.close()
        thread.join(timeout=12)
        Path(path).unlink(missing_ok=True)
    print(json.dumps({'first_frames': captured, 'terminal_output_bytes': len(output), 'exit': process.returncode, 'terminal_startup': output.decode(errors='replace')[:4000]}))


if __name__ == '__main__':
    main()
