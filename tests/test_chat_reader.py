"""Native account-local Chat reads: identity, integrity and bounded hostile inputs."""
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / 'docs/design/chat-spike/native-fixtures'
NATIVE = '11111111-1111-4111-8111-111111111111'


def source(home, provider='claude', records=None):
    folder = home / ('.claude/projects/-work' if provider == 'claude' else '.codex/sessions/2026/11/01')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (f'{NATIVE}.jsonl' if provider == 'claude' else f'rollout-test-{NATIVE}.jsonl')
    path.touch()
    if records is not None:
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records))
    return path


def message(text='Hello é 🌲', identifier='item-one'):
    return {'type': 'assistant', 'sessionId': NATIVE, 'version': '2.1.308', 'uuid': identifier,
            'message': {'content': [{'type': 'text', 'text': text}]}}


def read(home, **kw):
    from acc.broker.chat import read_chat
    kw.setdefault('workspace', '/work')
    return read_chat(home, 'claude', NATIVE, **kw)


def test_complete_newline_commits_and_repeated_text_is_distinct(tmp_path):
    path = source(tmp_path, records=[message()])
    first = read(tmp_path)
    assert first['state'] == 'ready' and len(first['items']) == 1
    with path.open('ab') as f:
        f.write(json.dumps(message(identifier='item-two'), ensure_ascii=False).encode())
    pending = read(tmp_path, after=first['source']['end'], checkpoint=first['source'])
    assert pending['items'] == [] and pending['state'] == 'unknown'
    with path.open('ab') as f:
        f.write(b'\n')
    second = read(tmp_path, after=first['source']['end'], checkpoint=first['source'])
    assert second['items'][0]['text'] == first['items'][0]['text']
    assert second['items'][0]['id'] != first['items'][0]['id']


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_native_fixtures_keep_four_messages_without_context_or_duplicate_events(tmp_path, provider):
    from acc.broker.chat import read_chat
    rows = [json.loads(l) for l in (FIXTURES / f'{provider}.jsonl').read_text().splitlines()]
    if provider == 'claude':
        native = rows[0]['sessionId']
    else:
        native = rows[0]['payload']['id']
    path = source(tmp_path, provider)
    path = path.rename(path.with_name(path.name.replace(NATIVE, native)))
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    result = read_chat(tmp_path, provider, native)
    assert [m['text'] for m in result['items']] == ['Fixture question é 🌲', 'Fixture reply é 🌲'] * 2
    assert len({m['id'] for m in result['items']}) == 4
    assert '/tmp/' not in json.dumps(result)


@pytest.mark.parametrize('change', ['rotate', 'rewrite', 'truncate'])
def test_source_changes_reset_instead_of_continuing_old_cursor(tmp_path, change):
    path = source(tmp_path, records=[message()])
    first = read(tmp_path)
    if change == 'rotate':
        path.unlink()
    path.write_text(json.dumps(message('Replacement', 'different')) + '\n' if change != 'truncate' else '')
    result = read(tmp_path, checkpoint=first['source'], after=first['source']['end'])
    assert result['reset'] is True
    assert result['state'] in ('gap', 'unknown')


@pytest.mark.parametrize('where', ['leaf', 'parent', 'root'])
def test_symlinks_are_refused_at_every_component(tmp_path, where):
    home = tmp_path / 'home'
    path = source(home, records=[message('Must never disclose')])
    victim = path if where == 'leaf' else path.parent if where == 'parent' else home / '.claude'
    moved = victim.rename(victim.with_name(victim.name + '-real'))
    victim.symlink_to(moved, target_is_directory=where != 'leaf')
    result = read(home)
    assert result['items'] == [] and result['state'] == 'error'


def test_oversize_malformed_and_split_utf8_are_not_silently_empty(tmp_path):
    path = source(tmp_path)
    path.write_bytes(b'not json\n' + b'x' * ((4 << 20) + 10) + b'\n' +
                     json.dumps(message(), ensure_ascii=False).encode()[:-3])
    result = read(tmp_path)
    assert result['state'] == 'gap' and result['omitted'] >= 2 and not result['items']


def test_pages_and_append_checkpoint_have_independent_positions(tmp_path):
    source(tmp_path, records=[message(str(i), str(i)) for i in range(5)])
    newest = read(tmp_path, limit=2)
    assert [m['text'] for m in newest['items']] == ['3', '4']
    earlier = read(tmp_path, before=newest['before'], limit=2, checkpoint=newest['source'])
    assert [m['text'] for m in earlier['items']] == ['1', '2']
    assert read(tmp_path, after=newest['source']['end'], checkpoint=newest['source'])['items'] == []


def test_version_mismatch_and_duplicate_discovery_are_not_empty(tmp_path):
    path = source(tmp_path, records=[{**message(), 'version': 'unknown'}])
    assert read(tmp_path)['state'] == 'unsupported'
    other = tmp_path / '.claude/projects/other'
    other.mkdir()
    (other / path.name).write_bytes(path.read_bytes())
    assert read(tmp_path, workspace=None)['state'] == 'unknown'


def test_wrong_native_record_and_private_reasoning_never_leave_broker(tmp_path):
    source(tmp_path, records=[{**message('Wrong session'), 'sessionId': 'wrong'},
                              {**message('safe'), 'message': {'content': [
                                  {'type': 'thinking', 'thinking': 'Private reasoning'},
                                  {'type': 'text', 'text': 'safe'}]}}])
    result = read(tmp_path)
    assert result['state'] == 'gap'
    assert [m['text'] for m in result['items']] == ['safe']
    assert 'Private reasoning' not in json.dumps(result)


def test_missing_source_after_cursor_is_an_explicit_gap(tmp_path):
    path = source(tmp_path, records=[message()])
    first = read(tmp_path)
    path.unlink()
    result = read(tmp_path, checkpoint=first['source'], after=first['source']['end'])
    assert result['reset'] is True and result['state'] == 'gap'


def test_duplicate_native_item_is_an_upsert_not_a_duplicate_bubble(tmp_path):
    source(tmp_path, records=[message('Updating'), message('Final')])
    result = read(tmp_path)
    assert [m['text'] for m in result['items']] == ['Final']
    assert result['items'][0]['revision']


def test_fifo_never_blocks_or_discloses(tmp_path):
    import os
    path = source(tmp_path)
    path.unlink()
    os.mkfifo(path)
    assert read(tmp_path)['state'] == 'error'


def test_tool_only_history_is_a_gap_not_confirmed_empty(tmp_path):
    source(tmp_path, records=[{**message(), 'message': {'content': [
        {'type': 'tool_use', 'id': 'call', 'input': {'secret': 'not-for-chat'}}]}}])
    result = read(tmp_path)
    assert result['state'] == 'ready' and result['notices']['tools'] == 1
    assert 'not-for-chat' not in json.dumps(result)


def test_source_scan_budget_reports_gap_without_reading_remainder(tmp_path, monkeypatch):
    from acc.broker import chat
    source(tmp_path, records=[message('x' * 1000)])
    monkeypatch.setattr(chat, 'MAX_BYTES', 128)
    result = read(tmp_path)
    assert result['state'] == 'gap' and result['source']['end'] == 0


async def test_account_broker_rpc_uses_its_own_home(tmp_path, monkeypatch):
    from acc.broker.config import BrokerConfig
    from acc.broker.server import Broker
    source(tmp_path, records=[message('Broker-owned account')])
    monkeypatch.setenv('HOME', str(tmp_path))
    broker = Broker(BrokerConfig(run_dir=tmp_path / 'run'))
    reply = await broker.dispatch(json.dumps({'id': 1, 'method': 'chat_read',
                                             'params': {'provider': 'claude', 'native_id': NATIVE}}).encode())
    assert reply['ok'] and reply['result']['items'][0]['text'] == 'Broker-owned account'


def test_unknown_record_schema_is_counted_as_a_gap(tmp_path):
    source(tmp_path, records=[message(), {'type': 'future-schema', 'private': 'do-not-render'}])
    result = read(tmp_path)
    assert result['state'] == 'gap' and result['omitted'] == 1
    assert 'do-not-render' not in json.dumps(result)


def test_compacted_rewrite_has_reset_and_omission_instead_of_invented_archive(tmp_path):
    path = source(tmp_path, records=[message('Before compaction')])
    first = read(tmp_path)
    path.write_text(json.dumps({'type': 'compacted', 'summary': 'private context'}) + '\n' +
                    json.dumps(message('After compaction', 'new')) + '\n')
    changed = read(tmp_path, after=first['source']['end'], checkpoint=first['source'])
    assert changed['reset'] is True and changed['state'] == 'gap'
    fresh = read(tmp_path)
    assert fresh['state'] == 'gap' and fresh['omitted'] == 1
    assert [m['text'] for m in fresh['items']] == ['After compaction']
    assert 'private context' not in json.dumps(fresh)


def test_codex_tool_record_is_a_gap_and_does_not_expose_arguments(tmp_path):
    from acc.broker.chat import read_chat
    source(tmp_path, 'codex', records=[
        {'type': 'session_meta', 'payload': {'id': NATIVE, 'cli_version': '0.163.0'}},
        {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'shell',
                                             'arguments': 'private command'}}])
    result = read_chat(tmp_path, 'codex', NATIVE)
    assert result['state'] == 'ready' and result['notices']['tools'] == 1
    assert 'private command' not in json.dumps(result)
