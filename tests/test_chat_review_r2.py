"""Deterministic reproductions for CP-1..CP-9, disposable account data only."""
import asyncio
import json
import subprocess
import sys
from dataclasses import replace

import pytest
from test_chat_reader import NATIVE, message, read, source


@pytest.mark.parametrize('repeat', [8000, 18000])
def test_cp1_repeated_unclosed_wrappers_cannot_stall_history_parser(repeat):
    code = f"from acc.providers.transcripts import _prompt; _prompt('<user_query>' * {repeat}); print('finished')"
    # Bound the deliberately vulnerable old implementation to a disposable child.
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == 'finished'


def test_cp2_follow_byte_budget_never_drops_earliest_new_message(tmp_path):
    path = source(tmp_path, records=[message('Start')])
    first = read(tmp_path)
    with path.open('a') as f:
        for i in range(3):
            f.write(json.dumps(message(f'M{i} ' + 'x' * 60000, f'new{i}')) + '\n')
    page = read(tmp_path, after=first['source']['end'], checkpoint=first['source'])
    rest = read(tmp_path, after=page['source']['end'], checkpoint=page['source'])
    assert [r['text'][:2] for r in page['items'] + rest['items']] == ['M0', 'M1', 'M2']


@pytest.mark.parametrize('prefix', ['# AGENTS.md instructions for /w\nprivate',
    '<user_instructions>private</user_instructions>', '<skill>private</skill>',
    '<permissions instructions>private</permissions instructions>'])
def test_cp3_instruction_shaped_user_records_omitted_with_notice(tmp_path, prefix):
    row = {**message(), 'type': 'user', 'message': {'content': prefix}}
    source(tmp_path, records=[row])
    result = read(tmp_path)
    assert result['state'] == 'gap' and result['omitted'] > 0
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('blocks', [
    'Real prompt <system-reminder>private</system-reminder>',
    [{'type': 'text', 'text': '<system-reminder>private</system-reminder>'},
     {'type': 'text', 'text': 'Real prompt'}],
])
def test_cp3_filter_each_block_without_silently_losing_real_prompt(tmp_path, blocks):
    source(tmp_path, records=[{**message(), 'type': 'user', 'message': {'content': blocks}}])
    result = read(tmp_path)
    assert [r['text'] for r in result['items']] == ['Real prompt']
    assert result['omitted'] > 0 and 'private' not in json.dumps(result)


def test_cp4_follow_parses_only_new_records(tmp_path, monkeypatch):
    from acc.broker import chat
    path = source(tmp_path, records=[message(str(i), str(i)) for i in range(100)])
    first = read(tmp_path)
    with path.open('a') as f:
        f.write(json.dumps(message('New', 'new')) + '\n')
    original = chat._message
    visited = []
    def record(*args, **kw):
        visited.append(1)
        return original(*args, **kw)
    monkeypatch.setattr(chat, '_message', record)
    result = read(tmp_path, after=first['source']['end'], checkpoint=first['source'])
    assert [r['text'] for r in result['items']] == ['New']
    assert len(visited) == 1


def test_cp4_deadline_never_advances_to_unread_tail(tmp_path, monkeypatch):
    from acc.broker import chat
    path = source(tmp_path, records=[message(str(i), str(i)) for i in range(100)])
    clock = [0.0]
    def tick():
        clock[0] += .3
        return clock[0]
    monkeypatch.setattr(chat.time, 'monotonic', tick)
    result = read(tmp_path)
    assert result['state'] in ('unknown', 'gap') and result['more']
    assert result['source'] is None or result['source']['end'] < path.stat().st_size


def test_cp5_append_during_scan_keeps_initial_snapshot_without_reset(tmp_path, monkeypatch):
    from acc.broker import chat
    path = source(tmp_path, records=[message('Initial')])
    original = chat._message
    appended = []
    def append(*args, **kw):
        if not appended:
            with path.open('a') as f:
                f.write(json.dumps(message('Appended', 'new')) + '\n')
            appended.append(True)
        return original(*args, **kw)
    monkeypatch.setattr(chat, '_message', append)
    first = read(tmp_path)
    assert not first['reset'] and [r['text'] for r in first['items']] == ['Initial']
    second = read(tmp_path, after=first['source']['end'], checkpoint=first['source'])
    assert not second['reset'] and [r['text'] for r in second['items']] == ['Appended']


def test_cp6_unrelated_link_does_not_deny_chat_for_account(tmp_path):
    source(tmp_path, records=[message()])
    (tmp_path / '.claude/projects/unrelated').symlink_to('/not-a-chat-source')
    assert read(tmp_path)['items'][0]['text'] == 'Hello é 🌲'


def test_cp7_normal_tools_are_distinct_from_corrupt_history(tmp_path):
    source(tmp_path, records=[{**message(), 'message': {'content': [
        {'type': 'tool_use', 'id': 'call', 'input': {'private': 'never-display'}}]}}])
    result = read(tmp_path)
    assert result['state'] == 'ready' and result['omitted'] == 0
    assert result['notices']['tools'] == 1 and 'never-display' not in json.dumps(result)


def test_cp8_traverse_only_ancestor(tmp_path):
    parent = tmp_path / 'traverse'
    parent.mkdir()
    home = parent / 'home'
    source(home, records=[message()])
    parent.chmod(0o111)
    try:
        assert read(home)['items'][0]['text'] == 'Hello é 🌲'
    finally:
        parent.chmod(0o700)


async def test_cp4_read_does_not_hold_broker_policy_lock_and_rechecks_policy(tmp_path, monkeypatch):
    from acc.broker.config import BrokerConfig
    from acc.broker.server import Broker
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return {'state': 'ready', 'items': []}
    broker = Broker(BrokerConfig(run_dir=tmp_path / 'run'))
    monkeypatch.setattr(broker._searches, 'run', blocked)
    work = asyncio.create_task(broker.dispatch(json.dumps({'id': 1, 'method': 'chat_read',
        'params': {'provider': 'claude', 'native_id': NATIVE}}).encode()))
    await asyncio.wait_for(entered.wait(), 2)
    with broker._folder_lock:
        broker.config = replace(broker.config, folder_revision=2)
    release.set()
    result = await work
    assert result['ok'] is False and 'revision' in result['error'].lower()


async def test_cp4_real_workers_keep_incremental_metadata_without_raw_content(tmp_path, monkeypatch):
    from acc.broker.config import BrokerConfig
    from acc.broker.search_worker import Searches
    monkeypatch.setenv('HOME', str(tmp_path))
    path = source(tmp_path, records=[message('Private initial')])
    worker = Searches()
    config = BrokerConfig(run_dir=tmp_path / 'run')
    params = {'provider': 'claude', 'native_id': NATIVE, 'workspace': '/work'}
    first = await worker.run(config, params, operation='chat_read')
    assert first['items'][0]['text'] == 'Private initial'
    assert 'Private initial' not in json.dumps(worker.chat_cache)
    with path.open('a') as f:
        f.write(json.dumps(message('Next', 'next')) + '\n')
    second = await worker.run(config, {**params, 'after': first['source']['end'],
        'checkpoint': first['source']}, operation='chat_read')
    assert not second['reset'] and [r['text'] for r in second['items']] == ['Next']


async def test_cp4_real_stalled_child_controls_deadline_and_cancel(tmp_path, monkeypatch):
    from acc.broker import search_worker
    from acc.broker.config import BrokerConfig
    from acc.broker.ops import OpError
    from acc.broker.server import Broker
    # Disposable stall process; killed by exact process handle.
    monkeypatch.setattr(search_worker, 'argv', lambda: [sys.executable, '-I', '-c',
        'import sys, time; sys.stdin.buffer.read(); time.sleep(60)'])
    broker = Broker(BrokerConfig(run_dir=tmp_path / 'run'))
    monkeypatch.setattr(search_worker, 'DEADLINE', .15)
    monkeypatch.setattr(search_worker, 'QUEUE_WAIT', .01)
    work = asyncio.create_task(broker.dispatch(json.dumps({'id': 1, 'method': 'chat_read',
        'params': {'provider': 'claude', 'native_id': NATIVE}}).encode()))
    for _ in range(100):
        if broker._searches.active:
            break
        await asyncio.sleep(0)
    with pytest.raises(OpError, match='another'):
        await broker._searches.run(broker.config, {}, operation='chat_read')
    ping = await asyncio.wait_for(broker.dispatch(b'{"id":2,"method":"ping","params":{}}'), .1)
    assert ping['ok']
    result = await work
    assert not result['ok'] and 'deadline' in result['error']
    # Cancellation also kills/reaps the exact child and releases the slot.
    for _ in range(100):
        if not broker._searches.active:
            break
        await asyncio.sleep(.001)
    monkeypatch.setattr(search_worker, 'DEADLINE', 2)
    task = asyncio.create_task(broker._searches.run(broker.config, {}, operation='chat_read'))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if not broker._searches.active:
            break
        await asyncio.sleep(.001)
    assert not broker._searches.active


def test_cp9_follows_reclaim_excess_share_without_leaking_slots():
    from fastapi import HTTPException

    from acc.server.api.chat import FollowSlots
    slots = FollowSlots()
    old = [slots.acquire(str(person)) for person in range(8) for _ in range(4)]
    ninth = slots.acquire('ninth')
    assert len(slots.active) == 32 and sum(stop.is_set() for _, stop in old) == 1
    for ticket, _ in old + [ninth]:
        slots.release(ticket)
        slots.release(ticket)
    assert not slots.active
    for _ in range(4):
        slots.acquire('one')
    with pytest.raises(HTTPException) as refused:
        slots.acquire('one')
    assert refused.value.status_code == 429


def test_cp3_unknown_codex_content_kind_is_gap_not_private_prompt(tmp_path):
    from acc.broker.chat import read_chat
    native = 'native-codex'
    root = tmp_path / '.codex/sessions/2026'
    root.mkdir(parents=True)
    (root / f'rollout-date-{native}.jsonl').write_text('\n'.join(json.dumps(row) for row in [
        {'type': 'session_meta', 'payload': {'id': native, 'cli_version': '0.163.0'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': 'private instructions'}],
            'internal_chat_message_metadata_passthrough': {'content_item_kinds': ['unqualified']}}},
    ]) + '\n')
    result = read_chat(tmp_path, 'codex', native)
    assert result['state'] == 'gap' and result['omitted'] == 1
    assert 'private instructions' not in json.dumps(result)


def test_cp4_periodic_prefix_verification_detects_middle_rewrite(tmp_path, monkeypatch):
    from acc.broker import chat
    path = source(tmp_path, records=[message(str(i) + 'x' * 200, str(i)) for i in range(100)])
    first = read(tmp_path)
    data = path.read_bytes()
    changed = data.replace(b'50xxx', b'50yyy', 1)
    assert changed != data and len(changed) == len(data)
    path.write_bytes(changed)
    monkeypatch.setattr(chat, 'VERIFY_SECONDS', 0)
    result = read(tmp_path, after=first['source']['end'], checkpoint=first['source'])
    assert result['reset'] and result['state'] == 'gap'


async def test_cp4_chat_transport_is_not_replayed_and_does_not_fence_writes(tmp_path):
    from contextlib import nullcontext

    from acc.server.broker_client import BrokerClient, BrokerUnavailable
    delivered, fenced = [], []
    class Authority:
        activity = staticmethod(nullcontext)
        mark_uncertain = staticmethod(fenced.append)
    async def handle(reader, writer):
        delivered.append(json.loads(await reader.readline()))
        writer.close()
        await writer.wait_closed()
    path = tmp_path / 'chat.sock'
    server = await asyncio.start_unix_server(handle, str(path))
    async with server:
        client = BrokerClient(path, authority=Authority())
        try:
            with pytest.raises(BrokerUnavailable):
                await client.call('chat_read')
            assert len(delivered) == 1 and not fenced
        finally:
            await client.close()


async def test_cp4_unreaped_worker_keeps_admission_slot(tmp_path, monkeypatch):
    from acc.broker import search_worker
    from acc.broker.config import BrokerConfig
    from acc.broker.ops import OpError
    entered, reaped = asyncio.Event(), asyncio.Event()
    class Process:
        returncode = None
        def kill(self):
            pass  # Simulated uninterruptible IO, never a real process.
        async def communicate(self, payload=None):
            entered.set()
            await reaped.wait()
            self.returncode = -9
            return b'', b''
    async def spawn(*args, **kwargs):
        return Process()
    monkeypatch.setattr(search_worker.asyncio, 'create_subprocess_exec', spawn)
    worker = search_worker.Searches()
    config = BrokerConfig(run_dir=tmp_path / 'run')
    task = asyncio.create_task(worker.run(config, {}, operation='chat_read'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.active
    with pytest.raises(OpError, match='another'):
        await worker.run(config, {}, operation='chat_read')
    reaped.set()
    for _ in range(100):
        if not worker.active:
            break
        await asyncio.sleep(0)
    assert not worker.active


@pytest.mark.parametrize('checkpoint', [[], {'end': -1},
    {'identity': 'a' * 64, 'digest': 'b' * 64, 'end': -1},
    {'identity': 'a' * 64, 'digest': 'b' * 64, 'end': 1 << 64}])
def test_cp4_malformed_checkpoint_cannot_expand_read_bounds(tmp_path, checkpoint):
    source(tmp_path, records=[message()])
    with pytest.raises(ValueError, match='invalid_checkpoint'):
        read(tmp_path, checkpoint=checkpoint)
