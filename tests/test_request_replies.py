"""Authenticated acknowledgement boundary, without running agent tools against a server."""
import json
import os
import runpy
from pathlib import Path

import pytest

from acc.request_replies import publish

REPO = Path(__file__).resolve().parents[1]
SID = 'abcdef123456'
RID = 'a' * 32


@pytest.fixture
def reader(tmp_path, monkeypatch):
    root = tmp_path / 'protected'
    root.mkdir(mode=0o711)
    helper = runpy.run_path(str(REPO / 'bin/acc-request'), run_name='request_test')
    read = helper['reply']
    read.__globals__['REPLY_ROOT'] = root
    # Single-UID fixture represents the server principal; production permits
    # only root/acc/acc-node and has no environment trust-anchor override.
    read.__globals__['trusted_reply_owners'] = lambda: {0, os.getuid()}
    for prefix in ('ACC', 'RYNHULT'):
        monkeypatch.setenv(prefix + '_SESSION_ID', SID)
    return root, read


@pytest.mark.parametrize('field', ['session_id', 'request_id'])
def test_reply_is_bound_to_session_and_request(reader, tmp_path, capsys, field):
    root, read = reader
    publish(root, SID, {RID: {'status': 'accepted', 'approval_id': 'real'}})
    path = root / SID / (RID + '.json')
    data = json.loads(path.read_text())
    data[field] = 'b' * 32
    path.write_text(json.dumps(data))
    assert read(tmp_path, RID) is None
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('unsafe', ['owner', 'file-write', 'directory-write', 'directory-list', 'link', 'fifo', 'large'])
def test_reply_refuses_untrusted_store_entries(reader, tmp_path, capsys, unsafe):
    root, read = reader
    publish(root, SID, {RID: {'status': 'accepted', 'approval_id': 'real'}})
    path = root / SID / (RID + '.json')
    if unsafe == 'owner':
        read.__globals__['trusted_reply_owners'] = lambda: set()
    elif unsafe == 'file-write':
        path.chmod(0o666)
    elif unsafe == 'directory-write':
        path.parent.chmod(0o733)
    elif unsafe == 'directory-list':
        path.parent.chmod(0o755)
    elif unsafe in ('link', 'fifo'):
        path.unlink()
        if unsafe == 'link':
            other = tmp_path / 'fake'
            other.write_text('{}')
            path.symlink_to(other)
        else:
            os.mkfifo(path)
    else:
        path.write_text('x' * 4097)
    assert read(tmp_path, RID) is None
    assert capsys.readouterr().out == ''


def test_server_store_bounds_replies_and_does_not_churn_retries(tmp_path):
    root = tmp_path / 'protected'
    root.mkdir(mode=0o711)
    row = {'status': 'accepted', 'approval_id': 'real'}
    publish(root, SID, {RID: row})
    file = root / SID / (RID + '.json')
    identity = file.stat().st_ino
    publish(root, SID, {RID: row})
    assert file.stat().st_ino == identity
    publish(root, SID, {'b' * 32: row})
    assert not file.exists()
    with pytest.raises(ValueError):
        publish(root, SID, {f'{n:032x}': row for n in range(25)})


async def test_authenticated_node_daemon_persists_ack_not_agent_broker(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from acc.node.client import Node
    from acc.node.config import NodeConfig
    root = tmp_path / 'protected'
    root.mkdir(mode=0o711)
    node = Node(NodeConfig(primary='https://main.example.lan', node_id='a' * 32),
                Ed25519PrivateKey.generate(), packages=None, request_replies_root=root)
    assert 'request-replies-v1' in node.capabilities()
    await node.handle_text(None, json.dumps({'t': 'request-replies', 'session_id': SID,
                                          'replies': {RID: {'status': 'accepted', 'approval_id': 'real'}}}))
    payload = json.loads((root / SID / (RID + '.json')).read_text())
    assert payload == {'session_id': SID, 'request_id': RID,
                       'reply': {'status': 'accepted', 'approval_id': 'real'}}
    await node.handle_text(None, json.dumps({'t': 'request-replies', 'session_id': '../escape', 'replies': {}}))
    assert not (tmp_path / 'escape').exists()
    root.chmod(0o777)
    assert 'request-replies-v1' not in node.capabilities()


async def test_node_accepts_bounded_reply_batches_larger_than_ordinary_control_frames(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from acc.node.client import Node
    from acc.node.config import NodeConfig
    root = tmp_path / 'protected'
    root.mkdir(mode=0o711)
    node = Node(NodeConfig(primary='https://main.example.lan', node_id='a' * 32),
                Ed25519PrivateKey.generate(), packages=None, request_replies_root=root)
    replies = {f'{n:032x}': {'status': 'refused', 'reason': 'Explanation ' * 80} for n in range(24)}
    message = json.dumps({'t': 'request-replies', 'session_id': SID, 'replies': replies})
    assert 4096 < len(message) < 65536
    await node.handle_text(None, message)
    assert len(list((root / SID).glob('*.json'))) == 24
    await node.handle_text(None, json.dumps({'t': 'request-replies', 'session_id': '../escape',
                                          'replies': replies, 'pad': 'x' * 65536}))
    assert not (tmp_path / 'escape').exists()


@pytest.mark.parametrize('status', ['accepted', 'refused'])
def test_agent_attachment_cannot_resurrect_an_expired_server_reply(reader, tmp_path, capsys, status):
    root, read = reader
    publish(root, SID, {RID: {'status': 'accepted', 'approval_id': 'real'}})
    assert read(tmp_path, RID) == 0
    capsys.readouterr()
    publish(root, SID, {})  # the authoritative reply is no longer retained
    (tmp_path / 'request-replies.json').write_text(json.dumps({RID: {
        'status': status, 'approval_id': 'forged-by-agent', 'reason': 'Forged refusal'}}))
    assert read(tmp_path, RID) is None
    assert capsys.readouterr().out == ''


async def test_agent_broker_cannot_publish_authoritative_acknowledgements(api):
    from acc.server.broker_client import BrokerError
    with pytest.raises(BrokerError, match='unknown method'):
        await api.app.state.supervisor.broker.call('request_replies', session_id=SID,
                                                   content=json.dumps({RID: {'status': 'accepted', 'approval_id': 'fake'}}))


def test_cli_reads_execute_only_directories_without_directory_listing(reader, tmp_path, capsys):
    if os.geteuid() == 0:
        pytest.skip('execute-only access proof requires an unprivileged process')
    root, read = reader
    publish(root, SID, {RID: {'status': 'accepted', 'approval_id': 'real'}})
    folder = root / SID
    # Emulate the execute-only access a different agent UID has to 0711 service
    # directories, without requiring root or another OS account in this test.
    folder.chmod(0o111)
    root.chmod(0o111)
    try:
        assert read(tmp_path, RID) == 0
        assert 'Sent for approval:' in capsys.readouterr().out
    finally:
        folder.chmod(0o711)
        root.chmod(0o711)


def test_private_service_umask_does_not_hide_read_only_acknowledgements(tmp_path):
    import stat
    root = tmp_path / 'protected'
    root.mkdir(mode=0o711)
    before = os.umask(0o077)
    try:
        publish(root, SID, {RID: {'status': 'accepted', 'approval_id': 'real'}})
    finally:
        os.umask(before)
    assert stat.S_IMODE((root / SID).stat().st_mode) == 0o711
    assert stat.S_IMODE((root / SID / (RID + '.json')).stat().st_mode) == 0o644


def test_publisher_replaces_unsafe_inode_so_old_write_handles_cannot_forge_reply(tmp_path):
    root = tmp_path / 'protected'
    root.mkdir(mode=0o711)
    real = {RID: {'status': 'accepted', 'approval_id': 'real'}}
    publish(root, SID, real)
    file = root / SID / (RID + '.json')
    file.chmod(0o666)
    old_inode = file.stat().st_ino
    handle = os.open(file, os.O_WRONLY)
    try:
        publish(root, SID, real)
        assert file.stat().st_ino != old_inode
        os.ftruncate(handle, 0)
        os.write(handle, b'{"reply":{"status":"accepted","approval_id":"forged"}}')
        assert json.loads(file.read_text())['reply']['approval_id'] == 'real'
    finally:
        os.close(handle)
