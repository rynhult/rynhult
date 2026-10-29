"""Security review of backup phase 2 (docs/reviews/2026-10-23-security-review-backup-phase-2.md).

Each test is a finding's proof: it failed on the reviewed branch and passes with the fix.
"""
import json
import os
import stat
import tempfile

import pytest
import test_backup_server

from acc import backup_server as backup

# Shared fixtures: real pinned restic and rest-server, run only in pytest scratch.
source, receiver = test_backup_server.source, test_backup_server.receiver
restic_binary, rest_server_binary = test_backup_server.restic_binary, test_backup_server.rest_server_binary


def _impostor(backend, backup_id):
    """What a compromised main can do with its writer credential: append a snapshot reusing an old tag."""
    with tempfile.TemporaryFile() as junk:
        junk.write(b'not the original capture')
        junk.seek(0)
        backend.command('backup', '--json', '--tag', backup_id, '--stdin', '--stdin-filename', 'acc-capture.tar',
                        input_file=junk)


# H1: append-only kept the ciphertext, but a duplicate tag made the generation unrestorable.
def test_tag_collision_cannot_make_a_historical_generation_unrestorable(source, receiver, restic_binary, tmp_path):
    target, root = receiver
    source.check_target(target['id'], binary=restic_binary)
    first = source.back_up(target['id'], binary=restic_binary)
    kit = source.kit()
    with source.backend(target, restic_binary) as backend:
        _impostor(backend, first['backup_id'])
        # Without a recorded snapshot ID the restore still refuses to guess.
        with pytest.raises(backup.Refused, match='ambiguous'):
            backup.restore(kit, target['id'], first['backup_id'], tmp_path / 'guess', binary=restic_binary, backend=backend)
        backup.restore(kit, target['id'], first['backup_id'], tmp_path / 'restored', binary=restic_binary,
                       backend=backend, snapshot_id=first['snapshot_id'])
    assert (tmp_path / 'restored/data/RESTORE-HOLD').is_file()
    verified = source.verify(target['id'], first['backup_id'], snapshot_id=first['snapshot_id'], binary=restic_binary)
    assert verified['snapshot_id'] == first['snapshot_id']
    history = source.history(target['id'], binary=restic_binary)
    rows = [row for row in history['items'] if row['backup_id'] == first['backup_id']]
    assert len(rows) == 2 and all(row['duplicate'] for row in rows)
    assert {row['snapshot_id'] for row in rows} >= {first['snapshot_id']}


def test_offline_restore_cli_selects_the_recorded_snapshot(source, restic_binary, tmp_path):
    target = source.add_local_target(tmp_path / 'encrypted', binary=restic_binary)
    first = source.back_up(target['id'], binary=restic_binary)
    with source.backend(target, restic_binary) as backend:
        _impostor(backend, first['backup_id'])
    kit = tmp_path / 'kit.json'
    kit.write_bytes(backup.canonical(source.kit()))
    args = ['restore', '--kit', str(kit), '--target-id', target['id'], '--backup-id', first['backup_id'],
            '--restic', str(restic_binary)]
    assert backup.main([*args, '--destination', str(tmp_path / 'guess')]) == 1
    assert backup.main([*args, '--snapshot-id', first['snapshot_id'], '--destination', str(tmp_path / 'restored')]) == 0
    assert (tmp_path / 'restored/data/RESTORE-HOLD').is_file()


# M1: the main-side adapter was a loopback TCP port any local account (member agents) could reach.
async def test_main_adapter_is_private_to_the_acc_account(tmp_path):
    import asyncio
    import socket
    from urllib.parse import unquote

    from acc.backup_targets import node_transport

    class Link:
        _loop = asyncio.get_running_loop()
        async def open_backup(self, target_id, installation_id):
            return await asyncio.open_connection('127.0.0.1', port)

    async def echo(reader, writer):
        writer.write((await reader.readexactly(4)).upper())
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(echo, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]

    def request():
        with node_transport(Link(), {'id': 'a' * 32}, 'b' * 32) as url:
            assert url.startswith('http+unix://'), url
            path = unquote(url[len('http+unix://'):].split(':/', 1)[0])
            assert stat.S_ISSOCK(os.lstat(path).st_mode)
            folder = os.lstat(os.path.dirname(path))
            assert folder.st_uid == os.getuid() and stat.S_IMODE(folder.st_mode) == 0o700
            with socket.socket(socket.AF_UNIX) as conn:
                conn.connect(path)
                conn.sendall(b'ping')
                assert conn.recv(4) == b'PING'
        assert not os.path.exists(path)
    try:
        await asyncio.to_thread(request)
    finally:
        server.close()
        await server.wait_closed()


# L1: a target that withholds or rolls back history went unnoticed.
def test_capture_and_history_detect_a_rolled_back_target(source, receiver, restic_binary):
    target, root = receiver
    source.check_target(target['id'], binary=restic_binary)
    source.back_up(target['id'], binary=restic_binary)
    second = source.back_up(target['id'], binary=restic_binary)
    # Target root withholds the newest generation (equivalent to serving an older copy).
    (root / target['id'] / 'snapshots' / second['snapshot_id']).unlink()
    with pytest.raises(backup.Refused, match='rolled back'):
        source.back_up(target['id'], binary=restic_binary)
    assert source.history(target['id'], binary=restic_binary)['verified_missing'] is True
    assert json.loads((source.root / f"result-{target['id']}.json").read_bytes()) == second


# L2: the receiver unit ran without capability, device, kernel or address-family/IP restrictions.
def test_receiver_unit_is_confined_to_loopback_without_capabilities(source):
    import subprocess
    target = source.add_node_target('a' * 32, 'archive-node')
    commands = source.target_setup(target['id'])
    for directive in ('CapabilityBoundingSet=\n', 'AmbientCapabilities=\n', 'PrivateDevices=true', 'IPAddressDeny=any',
                      'IPAddressAllow=localhost', 'RestrictSUIDSGID=true', 'ProtectKernelTunables=true',
                      'SystemCallArchitectures=native', 'RestrictNamespaces=true', 'LockPersonality=true'):
        assert directive in commands, directive
    assert subprocess.run(['sh', '-n'], input=commands, text=True).returncode == 0


# L3: showing the root setup commands left no audit trail.
async def test_viewing_target_setup_is_audited(api, env):
    from test_auth import cookie_client, do_setup
    from test_users import csrf

    from acc.server.models import AuditLog, Node
    await do_setup(api, env)
    owner = cookie_client(api)
    with api.app.state.db() as db:
        db.add(Node(id='c' * 16, name='archive-node', status='active', info={'os': 'linux'}))
        db.commit()
    target = (await owner.post('/api/host/backups/targets', json={'node_id': 'c' * 16}, headers=csrf(owner))).json()
    assert (await owner.get(f"/api/host/backups/{target['id']}/setup")).status_code == 200
    with api.app.state.db() as db:
        row = db.query(AuditLog).filter_by(action='backup.target_setup').one()
        assert row.target == target['id']
