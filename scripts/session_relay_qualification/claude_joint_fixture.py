"""Offline native PTY endpoint; authority and receipts stay in production parent.

The private test control socket is administrative fixture orchestration, never
an input authority. Only framed bytes from the real Host enter the test prompt.
"""
import errno
import fcntl
import json
import os
import pty
import shutil
import socket
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from acc.broker.relay_enclosure import Enclosure


def control(method):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(25)
        connection.connect('/evidence/joint.sock')
        connection.sendall(json.dumps({'method': method}).encode() + b'\n')
        data = b''
        while b'\n' not in data:
            chunk = connection.recv(8192)
            if not chunk or len(data) > 65536:
                raise ValueError('Incomplete fixture control reply')
            data += chunk
        result = json.loads(data)
        if 'error' in result:
            raise RuntimeError(result['error'])
        return result


def native_tui(settings, env, observer_file, requests, native):
    projects = Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects'
    shared = Path('/evidence/native-projects')
    shutil.move(projects, shared)
    projects.symlink_to(shared, target_is_directory=True)
    mode = Path('/evidence/joint-mode').read_text()
    before_requests = len([r for r in requests if 'count_tokens' not in r['path']])
    original = settings.read_text()
    value = json.loads(original)
    wrapper = Path('/evidence/joint_hook.py')
    wrapper.write_text("import runpy,sys; from pathlib import Path; "
        "event=sys.argv[2]; g=runpy.run_path('/evidence/acc/session_relay/claude_hook.py'); "
        "code=g['main'](); Path('/evidence/hook-done-'+event).write_text(str(code)); raise SystemExit(code)\n")
    for event in ('SessionStart', 'UserPromptSubmit', 'Stop', 'SessionEnd'):
        channel = '/evidence/missing/owner-channel.json' if mode == 'lost_stop' and event == 'Stop' else '/evidence/hook/owner-channel.json'
        command = '/usr/bin/python3 /evidence/joint_hook.py ' + channel + ' ' + event
        value['hooks'][event] = [{'hooks': [{'type': 'command', 'command': command}]}]
    settings.write_text(json.dumps(value))
    enclosed = Enclosure(Path('/evidence/owners'), SimpleNamespace(run_id='joint-native'), '/usr/bin/claude')
    master, slave = pty.openpty()
    os.set_blocking(master, False)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 120, 0, 0))
    listener = socket.socket(socket.AF_UNIX)
    listener.bind('/evidence/input.sock')
    listener.listen(1)
    listener.settimeout(25)
    failures, frames = [], []
    def input_bridge():
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(25)
                def exact(size):
                    data = b''
                    while len(data) < size:
                        chunk = connection.recv(size - len(data))
                        if not chunk:
                            return None
                        data += chunk
                    return data
                while True:
                    header = exact(5)
                    if header is None:
                        break
                    kind, size = struct.unpack('>cI', header)
                    assert kind == b'd' and 0 < size <= 4096
                    data = exact(size)
                    assert data is not None
                    frames.append(data.decode())
                    assert os.write(master, data) == len(data)
        except Exception as error:
            failures.append(repr(error))
    bridge = threading.Thread(target=input_bridge, daemon=True)
    bridge.start()
    child = subprocess.Popen(['/usr/bin/python3', '-I', '/evidence/pty_exec.py', str(enclosed.cli),
        '--resume', native, '--model', 'claude-sonnet-4-5', '--settings', str(settings)],
        stdin=slave, stdout=slave, stderr=slave, env={**env, 'TERM': 'xterm-256color'}, start_new_session=True)
    os.close(slave)
    output, completed = bytearray(), False
    started = time.monotonic()
    prepared = False
    failed_hook_at = None
    try:
        while child.poll() is None and time.monotonic() - started < 30 and len(output) < 256 << 10:
            try:
                data = os.read(master, 65536)
                output.extend(data)
                if b'\x1b[6n' in data:
                    os.write(master, b'\x1b[1;1R')
            except BlockingIOError:
                pass
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
            if not prepared and time.monotonic() - started > 4:
                control('ready')
                prepared = True
            if prepared:
                status = control('status')
                if mode == 'accepted' and status['processed'] == 2:
                    completed = True
                    break
                marker = Path('/evidence/hook-done-' + ('UserPromptSubmit' if mode == 'deny_prompt' else 'Stop'))
                if mode != 'accepted' and marker.exists():
                    assert marker.read_text() == ('2' if mode == 'deny_prompt' else '0')
                    failed_hook_at = failed_hook_at or time.monotonic()
                    if time.monotonic() - failed_hook_at > 2:
                        completed = True
                        break
            time.sleep(.03)
        assert completed, (status if prepared else {}, output.decode(errors='replace')[-5000:])
    finally:
        if completed and mode != 'deny_prompt' and child.poll() is None:
            os.write(master, b'/exit')  # administrative exit, outside the input profile
            time.sleep(.7)
            os.write(master, b'\r')
            deadline = time.monotonic() + 4
            while child.poll() is None and time.monotonic() < deadline:
                try:
                    output.extend(os.read(master, 65536))
                except (BlockingIOError, OSError):
                    pass
                time.sleep(.03)
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=8)
        control('detach')
        listener.close()
        bridge.join(timeout=2)
        os.close(master)
        settings.write_text(original)
    started_receipt = json.loads((enclosed.path / 'started.json').read_text())
    empty = enclosed.proof('exit.json', f"{started_receipt['pid']}:{started_receipt['started']}")
    enclosed.close()
    assert not failures and not bridge.is_alive() and empty, (failures, bridge.is_alive(), empty, child.returncode)
    if mode != 'deny_prompt':
        assert child.returncode == 0
    native_requests = len([r for r in requests if 'count_tokens' not in r['path']]) - before_requests
    assert native_requests == (0 if mode == 'deny_prompt' else 1)
    return {'mode': mode, 'native_model_requests': native_requests, 'completed': completed, 'frames': frames, 'empty_tree': empty, 'exit': child.returncode,
            'terminalMediated': False, 'output_bytes': len(output), 'scope': 'production main/broker/grants/Host/hooks/journal with real native PTY; fixture launcher, not browser or owned opener'}
