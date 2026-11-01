"""Only the authenticated node control channel can carry folder helper requests."""
import asyncio
import json

import pytest

from acc.server.nodes import NodeLink


async def test_folder_control_is_bound_to_machine_and_fails_on_disconnect():
    sent = asyncio.Queue()
    link = NodeLink('node1', 'demo', sent.put, {'capabilities': ['folder-policy-v1']}, ['alex-agent'])
    try:
        with pytest.raises(ConnectionError):
            await link.folder_policy({'machine_id': 'other'})
        task = asyncio.create_task(link.folder_policy({'machine_id': 'node1'}))
        frame = json.loads(await asyncio.wait_for(sent.get(), 1))
        assert frame['t'] == 'folder-policy'
        link.folder_policy_result({'id': 'unrelated', 'ok': True, 'result': {}})
        assert not task.done()
        link.folder_policy_result({'id': frame['id'], 'ok': True, 'result': {'revision': 2}})
        assert await task == {'revision': 2}
        pending = asyncio.create_task(link.folder_policy({'machine_id': 'node1'}))
        await asyncio.wait_for(sent.get(), 1)
        link.close()
        with pytest.raises(ConnectionError):
            await pending
    finally:
        link.close()
        await link.wait_closed()


async def test_node_rejects_cross_machine_folder_requests_without_root_call():
    from acc.node.folder_policy import handle
    class Socket:
        async def send(self, value):
            pytest.fail('Wrong machine request must be discarded before contacting the root helper.')
    await handle(Socket(), {'id': 'request', 'payload': {'machine_id': 'other'}}, 'node1')


async def test_node_allows_bounded_folder_frames_above_old_control_frame_limit(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from acc import folder_channel
    from acc.node.client import Node
    messages = asyncio.Queue()
    async def root(message, *, socket):
        assert socket == fake_socket
        return {'revision': 1}
    monkeypatch.setattr(folder_channel, 'request', root)  # fixed local root socket boundary only
    class Socket:
        async def send(self, message):
            await messages.put(json.loads(message))
    fake_socket = tmp_path / 'folder.sock'
    node = SimpleNamespace(config=SimpleNamespace(node_id='node1', folder_policy_socket=fake_socket))
    frame = {'t': 'folder-policy', 'id': 'review', 'payload': {'machine_id': 'node1', 'padding': 'x' * 5000}}
    await Node.handle_text(node, Socket(), json.dumps(frame))
    result = await asyncio.wait_for(messages.get(), 1)
    assert result == {'t': 'folder-policy-result', 'id': 'review', 'ok': True, 'result': {'revision': 1}}
    await asyncio.gather(*node._tool_tasks)


def test_node_folder_helper_is_unconfigured_by_default(tmp_path):
    from types import SimpleNamespace

    from acc.node.client import Node
    from acc.node.config import NodeConfig

    config = NodeConfig('https://main.example.lan', 'node1')
    assert config.folder_policy_socket is None
    node = SimpleNamespace(config=config, request_replies_root=tmp_path / 'replies')
    assert 'folder-policy-v1' not in Node.capabilities(node)


def test_node_folder_helper_configuration_round_trips(tmp_path):
    from acc.node.config import NodeConfig

    config = NodeConfig('https://main.example.lan', 'node1', folder_policy_socket=tmp_path / 'fake.sock')
    config.write(tmp_path / 'node.conf')
    assert NodeConfig.load(tmp_path / 'node.conf') == config


async def test_unconfigured_node_refuses_folder_request_without_connecting(monkeypatch):
    from acc.node.folder_policy import handle

    async def connect(*args, **kwargs):
        pytest.fail('Unconfigured nodes must not contact any helper.')
    monkeypatch.setattr(asyncio, 'open_unix_connection', connect)
    replies = []
    class Socket:
        async def send(self, value):
            replies.append(json.loads(value))
    await handle(Socket(), {'id': 'review', 'payload': {'machine_id': 'node1'}}, 'node1')
    assert replies == [{'t': 'folder-policy-result', 'id': 'review', 'ok': False}]
