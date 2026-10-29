"""Receiver binding is opt-in and cannot become an arbitrary socket proxy."""
import json
import os

import pytest


def test_receiver_binding_rejects_unapproved_target_and_writable_config(tmp_path):
    from acc.backup_targets import receiver_binding
    path = tmp_path / 'binding.json'
    from acc.backup_receiver_check import identity
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    path.write_text(json.dumps({'target_id': 'a' * 32, 'installation_id': 'b' * 32, 'port': 18762, **identity(data, os.getuid())}))
    path.chmod(0o600)
    assert receiver_binding(path, 'a' * 32, 'b' * 32, owner=os.getuid(), data=data, receiver_uid=os.getuid()) == 18762
    with pytest.raises(ValueError):
        receiver_binding(path, 'c' * 32, 'b' * 32, owner=os.getuid(), data=data, receiver_uid=os.getuid())
    path.chmod(0o666)
    with pytest.raises(ValueError):
        receiver_binding(path, 'a' * 32, 'b' * 32, owner=os.getuid(), data=data, receiver_uid=os.getuid())
    path.unlink()
    path.symlink_to('/etc/passwd')
    with pytest.raises(ValueError):
        receiver_binding(path, 'a' * 32, 'b' * 32, owner=os.getuid(), data=data, receiver_uid=os.getuid())


async def test_backup_channel_refuses_missing_owner_binding(tmp_path):
    from acc.node.client import Relay, frame
    class Socket:
        def __init__(self):
            self.sent = []
        async def send(self, data):
            self.sent.append(data)
    ws = Socket()
    relay = Relay(ws, tmp_path)
    await relay.open(1, json.dumps({'type': 'backup-v1', 'target_id': 'a' * 32,
                                  'installation_id': 'b' * 32, 'port': 22}).encode())
    assert ws.sent[0][:5] == frame(b'e', 1)
    assert not relay.channels


async def test_node_adapter_roundtrips_and_cleans_channels(tmp_path):
    import asyncio

    from acc.backup_targets import node_transport
    class Link:
        _loop = asyncio.get_running_loop()
        async def open_backup(self, target_id, installation_id):
            assert target_id == 'a' * 32
            assert installation_id == 'b' * 32
            return await asyncio.open_connection('127.0.0.1', port)
    async def echo(reader, writer):
        try:
            try:
                data = await reader.readexactly(4)
            except asyncio.IncompleteReadError:
                return  # the disk-check preflight closes without sending repository data
            writer.write(data.upper())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(echo, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    def request():
        import socket
        with node_transport(Link(), {'id': 'a' * 32}, 'b' * 32) as url:
            with socket.socket(socket.AF_UNIX) as conn:
                conn.settimeout(5)
                conn.connect(url[len('http+unix://'):].split(':', 1)[0])
                conn.sendall(b'ping')
                assert conn.recv(4) == b'PING'
    try:
        await asyncio.to_thread(request)
    finally:
        server.close()
        await server.wait_closed()


def test_backup_target_blocks_main_supplied_root_update(tmp_path, monkeypatch):
    from acc import backup_targets, update_helper
    from acc.node.updates import Updates
    binding = tmp_path / 'binding.json'
    binding.write_text('{}')
    monkeypatch.setattr(backup_targets, 'BINDING', binding)
    root = tmp_path / 'updates'
    (root / 'requests').mkdir(parents=True)
    request = root / 'requests' / ('a' * 32 + '.json')
    request.write_text(json.dumps({'action':'update','version':'99.0.0','build':'b'*64,'sha256':'c'*64}))
    monkeypatch.setenv('ACC_UPDATE_ROOT', str(root))
    monkeypatch.setenv('ACC_UPDATE_MODE', 'node')
    monkeypatch.setenv('ACC_UPDATE_GROUP', '')
    # No network or root install may be reached, even with a pre-queued valid request.
    monkeypatch.setattr(update_helper.NodeTransaction, 'apply', lambda self: pytest.fail('root update ran on backup target'))
    assert update_helper.main() == 0
    assert request.exists()
    assert json.loads((root / 'state.json').read_text())['ok'] is False
    request.unlink()
    updates = Updates(root)
    updates.handle({'t':'update','version':'99.0.0','build':'b'*64,'sha256':'c'*64})
    assert not list((root / 'requests').glob('*.json'))
    assert 'owner' in updates.refused['detail'].lower()


async def test_backup_target_blocks_root_package_requests(tmp_path, monkeypatch):
    from acc import backup_targets
    from acc.node.packages import HelperPackages, Refused
    binding = tmp_path / 'binding.json'
    binding.write_text('{}')
    monkeypatch.setattr(backup_targets, 'BINDING', binding)
    root = tmp_path / 'packages'
    (root / 'requests').mkdir(parents=True)
    packages = HelperPackages({'restic': {}}, root)
    with pytest.raises(Refused, match='owner'):
        packages.request('pkg-' + 'a' * 12, 'install', 'restic')
    with pytest.raises(ValueError, match='owner'):
        await packages.tool({'t':'tool','id':'tool-'+'a'*12,'manager':'apt','name':'curl','version':None,'operation':'install'}, timeout=0)
    assert not list((root / 'requests').glob('*'))


async def test_node_adapter_bounds_accepted_connections():
    import asyncio
    import threading

    from acc.backup_targets import node_transport
    started = threading.Event()
    count = 0
    class Link:
        _loop = asyncio.get_running_loop()
        async def open_backup(self, *args):
            nonlocal count
            count += 1
            started.set()
            await asyncio.Future()
    def connect():
        import socket
        with node_transport(Link(), {'id':'a'*32}, 'b'*32) as url:
            path = url[len('http+unix://'):].split(':', 1)[0]
            sockets = [socket.socket(socket.AF_UNIX) for _ in range(3)]
            for conn in sockets:
                conn.settimeout(2)
                conn.connect(path)
            try:
                assert started.wait(2)
                assert sockets[2].recv(1) == b''
            finally:
                for conn in sockets:
                    conn.close()
    await asyncio.to_thread(connect)
    assert count == 1


async def test_old_node_requires_update_before_backup_open(monkeypatch):
    import asyncio

    from acc.server import nodes
    monkeypatch.setattr(nodes, 'OPEN_TIMEOUT', .01)
    sent = []
    async def send(data):
        sent.append(data)
    link = nodes.NodeLink('a'*32, 'archive-node', send, {}, [])
    try:
        with pytest.raises(ConnectionError, match='update'):
            await link.open_backup('b'*32, 'c'*32)
        assert not sent
    finally:
        link.close()
        await asyncio.gather(link._sender, link._watchdog, return_exceptions=True)


@pytest.mark.parametrize('free, expected', [(None, 20), (100 * 2**30, 50), (0, 1), (2**60, 10240)])
def test_receiver_ceiling_defaults_are_bounded(free, expected):
    from acc.backup_targets import default_max_size
    assert default_max_size(free) == expected * 2**30


def test_receiver_commands_use_saved_ceiling_and_preserve_legacy_targets():
    from acc.backup_targets import setup_commands
    assert '--max-size 32212254720' in setup_commands({'id': 'a'*32, 'max_size_bytes': 30 * 2**30}, 'b'*32, '{SHA}EXAMPLE')
    assert '--max-size 107374182400' in setup_commands({'id': 'a'*32}, 'b'*32, '{SHA}EXAMPLE')


async def test_prepare_target_saves_validated_ceiling(api, env):
    from test_auth import cookie_client, do_setup
    from test_users import csrf

    from acc.server.models import Node
    await do_setup(api, env)
    owner = cookie_client(api)
    with api.app.state.db() as db:
        db.add(Node(id='c'*16, name='archive-node', status='active', info={'os': 'linux'}))
        db.commit()
    for value in [0, -1, 10241 * 2**30, True, '30']:
        response = await owner.post('/api/host/backups/targets', json={'node_id': 'c'*16, 'max_size_bytes': value}, headers=csrf(owner))
        assert response.status_code == 422
    response = await owner.post('/api/host/backups/targets', json={'node_id': 'c'*16, 'max_size_bytes': 30*2**30}, headers=csrf(owner))
    assert response.status_code == 201
    target = response.json()
    assert target['max_size_bytes'] == 30*2**30
    setup = (await owner.get(f"/api/host/backups/{target['id']}/setup")).json()
    assert '--max-size 32212254720' in setup['commands']
    state = (await owner.get('/api/host/backups')).json()
    assert state['targets'][0]['max_size_bytes'] == 30*2**30


def test_capacity_uses_filesystem_containing_receiver():
    from types import SimpleNamespace

    from acc.backup_targets import node_max_size
    link = SimpleNamespace(host_snapshot={'disks': [
        {'mount': '/', 'free': 100 * 2**30},
        {'mount': '/var', 'free': 80 * 2**30},
        {'mount': '/mnt/archive', 'free': 800 * 2**30},
    ]}, host_light={'disk': {'mount': '/', 'free': 10 * 2**30}})
    assert node_max_size(link) == 40 * 2**30
    link.host_snapshot = None
    assert node_max_size(link) == 5 * 2**30
    link.host_light['disk']['mount'] = '/mnt/archive'
    assert node_max_size(link) == 20 * 2**30
    assert node_max_size(None) == 20 * 2**30


def test_setup_is_paste_safe_and_explains_failed_guards():
    import subprocess

    from acc.backup_targets import setup_commands
    commands = setup_commands({'id': 'a'*32}, 'b'*32, '{SHA}EXAMPLE')
    assert "bash -euo pipefail <<'ACC_SETUP'" in commands
    assert 'trap ' in commands and 'Setup stopped' in commands
    assert 'already configured' in commands and 'mount' in commands.lower()
    # Execute only the enclosing child-shell shape with an injected failure: never touch host setup.
    shell = commands[:commands.index('\numask')] + '\nfalse\nACC_SETUP\nprintf "LOGIN_SURVIVED\\n"\n'
    result = subprocess.run(['bash'], input=shell, capture_output=True, text=True)
    assert 'LOGIN_SURVIVED' in result.stdout


@pytest.mark.parametrize('change', ['device', 'fsid', 'mode', 'owner', 'filesystem-root', 'unreadable'])
def test_receiver_disk_identity_fails_closed(tmp_path, monkeypatch, change):
    from acc import backup_receiver_check as check
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    record = check.identity(data, os.geteuid(), inspect_entries=True)
    monkeypatch.setattr(check, 'filesystem_root', lambda p: change == 'filesystem-root')
    if change in ('device', 'fsid'):
        record[change] += 1
    if change == 'mode': data.chmod(0o755)
    uid = os.geteuid() + 1 if change == 'owner' else os.geteuid()
    if change == 'unreadable': monkeypatch.setattr(check.os, 'access', lambda *a, **kw: False)
    with pytest.raises(ValueError, match='receiver'):
        check.verify(data, record, uid, inspect_entries=True)


def test_setup_embedded_disk_writer_is_valid_python():
    from acc.backup_targets import setup_commands
    script = setup_commands({'id': 'a'*32}, 'b'*32, '{SHA}EXAMPLE')
    code = script.split("<<'ACC_DISK'\n", 1)[1].split('\nACC_DISK', 1)[0]
    compile(code, 'generated disk binding writer', 'exec')
    assert 'ExecStartPre=/usr/bin/python3 -I /usr/local/libexec/acc-backup-receiver-check' in script


async def test_receiver_preflight_failure_reports_disk_check(tmp_path):
    import asyncio

    from acc.backup_server import Refused
    from acc.backup_targets import node_transport
    class Link:
        _loop = asyncio.get_running_loop()
        async def open_backup(self, *args):
            raise ConnectionError('receiver refused')
    def check():
        with pytest.raises(Refused, match='Backup receiver unavailable'):
            with node_transport(Link(), {'id': 'a'*32}, 'b'*32, preflight=True):
                pytest.fail('an unverified receiver was used')
    await asyncio.to_thread(check)


def test_filesystem_root_detection_allows_bind_mounted_subfolder(tmp_path):
    from acc.backup_receiver_check import filesystem_root
    data = tmp_path / 'data'
    data.mkdir()
    mounts = tmp_path / 'mountinfo'
    mounts.write_text(f'80 1 8:1 / {data} rw - ext4 /dev/example rw\n')
    assert filesystem_root(data, mountinfo=mounts)
    mounts.write_text(f'80 1 8:1 /acc-backup {data} rw - ext4 /dev/example rw\n')
    assert not filesystem_root(data, mountinfo=mounts)


def test_receiver_binding_names_why_it_refused(tmp_path):
    from acc.backup_receiver_check import identity
    from acc.backup_targets import ReceiverRefused, receiver_binding
    path, data = tmp_path / 'binding.json', tmp_path / 'data'
    data.mkdir(mode=0o700)
    kw = dict(owner=os.getuid(), data=data, receiver_uid=os.getuid())
    with pytest.raises(ReceiverRefused) as caught:
        receiver_binding(path, 'a' * 32, 'b' * 32, **kw)
    assert caught.value.reason == 'missing'
    path.write_text(json.dumps({'target_id': 'a' * 32, 'installation_id': 'b' * 32, 'port': 18762, **identity(data, os.getuid())}))
    path.chmod(0o600)
    with pytest.raises(ReceiverRefused) as caught:
        receiver_binding(path, 'c' * 32, 'b' * 32, **kw)
    assert caught.value.reason == 'mismatch'
    data.chmod(0o755)
    with pytest.raises(ReceiverRefused) as caught:
        receiver_binding(path, 'a' * 32, 'b' * 32, **kw)
    assert caught.value.reason == 'disk'
    path.chmod(0o666)
    with pytest.raises(ReceiverRefused) as caught:
        receiver_binding(path, 'a' * 32, 'b' * 32, **kw)
    assert caught.value.reason == 'unsafe'


@pytest.mark.parametrize('reason', ['missing', 'mismatch', 'unsafe', 'disk', 'down'])
async def test_node_sends_the_fixed_text_for_each_receiver_refusal(tmp_path, monkeypatch, reason):
    import asyncio

    from acc import backup_targets
    from acc.node.client import Relay
    class Socket:
        def __init__(self):
            self.sent = []
        async def send(self, data):
            self.sent.append(data)
    def binding(*a):
        if reason == 'down':
            return 18762
        raise backup_targets.ReceiverRefused('internal detail', reason)
    async def refuse(*a, **kw):
        raise ConnectionRefusedError('internal detail')
    monkeypatch.setattr(backup_targets, 'receiver_binding', binding)
    monkeypatch.setattr(asyncio, 'open_connection', refuse)
    ws = Socket()
    await Relay(ws, tmp_path).open(1, json.dumps({'type': 'backup-v1', 'target_id': 'a' * 32, 'installation_id': 'b' * 32}).encode())
    assert ws.sent[0].endswith(backup_targets.RECEIVER_REFUSALS[reason].encode())
    assert len(backup_targets.RECEIVER_REFUSALS[reason]) <= 300  # the main keeps 300 characters of a refusal
    assert b'internal detail' not in ws.sent[0]


@pytest.mark.parametrize('sent, shown', [
    ('missing', 'missing'), ('disk', 'disk'), ('down', 'down'),
    ('Node update required for backup targets', 'Node update required for backup targets'),
    ('<a href=x>anything a node makes up</a>', 'generic'),
    ('Backup receiver disk check failed or receiver unavailable; verify acc-backup ownership', 'generic'),  # older node
])
async def test_check_setup_shows_only_known_receiver_refusals(sent, shown):
    import asyncio

    from acc.backup_server import Refused
    from acc.backup_targets import RECEIVER_GENERIC, RECEIVER_REFUSALS, node_transport
    text = RECEIVER_REFUSALS.get(sent, sent)
    class Link:
        _loop = asyncio.get_running_loop()
        async def open_backup(self, *args):
            raise ConnectionError(text)
    def check():
        with pytest.raises(Refused) as caught:
            with node_transport(Link(), {'id': 'a'*32}, 'b'*32, preflight=True):
                pytest.fail('an unverified receiver was used')
        return str(caught.value)
    message = await asyncio.to_thread(check)
    assert message == (RECEIVER_GENERIC if shown == 'generic' else RECEIVER_REFUSALS.get(shown, shown))


async def test_check_setup_waits_out_a_busy_receiver(tmp_path):
    import asyncio

    from acc.backup_targets import node_transport
    attempts = []
    class Writer:
        def close(self): pass
        async def wait_closed(self): pass
    class Link:
        _loop = asyncio.get_running_loop()
        async def open_backup(self, *args):
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError('backup receiver busy')
            return None, Writer()
    def check():
        with node_transport(Link(), {'id': 'a'*32}, 'b'*32, preflight=True):
            pass
    await asyncio.to_thread(check)
    assert len(attempts) == 3
