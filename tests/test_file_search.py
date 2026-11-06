"""Bounded filename search uses the real broker filesystem boundary, never content."""
import os
from pathlib import Path

import pytest
from isolated_settings import isolated_broker

from acc.broker import ops


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    return isolated_broker(tmp_path, allowed_roots=(root,)), root


def search(tree, q, limit=20):
    config, root = tree
    return ops.file_search(config, str(root), q=q, limit=limit)


def paths(result):
    return {row['path'] for row in result['results']}


def test_names_casefold_fuzzy_and_never_content(tree):
    _, root = tree
    (root / 'SessionManager.py').write_text('private contents')
    (root / 'other.txt').write_text('SessionManager')
    (root / 'nested').mkdir()
    (root / 'nested/NOTES.md').write_text('notes')
    assert paths(search(tree, 'session')) == {'SessionManager.py'}
    assert paths(search(tree, 'ssnmgr')) == {'SessionManager.py'}
    result = search(tree, 'NESTED/notes')
    assert paths(result) == {'nested/NOTES.md'}
    assert not result['truncated']
    assert all(set(row) == {'name', 'path', 'kind'} for row in result['results'])


def test_search_excludes_ignored_secrets_links_and_special_files(tree, tmp_path):
    _, root = tree
    (root / '.gitignore').write_text('generated/\n*.tmp\n!keep.tmp\n')
    for rel in ['.git/config', 'node_modules/package/target.py', 'generated/target.py', '.aws/target',
                '.env', 'drop.tmp', 'keep.tmp', 'normal/target.py', 'normal/drop.cache']:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('no contents returned')
    (root / 'normal/.gitignore').write_text('*.cache\n')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'escaped-target.py').write_text('private')
    (root / 'escape').symlink_to(outside, target_is_directory=True)
    (root / 'linked-target.py').symlink_to(outside / 'escaped-target.py')
    os.mkfifo(root / 'fifo-target')
    assert paths(search(tree, 'target')) == {'normal/target.py'}
    assert paths(search(tree, '.tmp')) == {'keep.tmp'}
    assert paths(search(tree, '.cache')) == set()
    assert paths(search(tree, '.env')) == set()
    with pytest.raises(ops.OpError):
        ops.file_search(tree[0], str(root / '../outside'), q='target')
    with pytest.raises(ops.OpError):
        ops.file_search(tree[0], str(root / 'escape'), q='target')


@pytest.mark.parametrize(('q', 'limit'), [('', 20), (' ', 20), ('x' * 201, 20), ('../x', 20),
    ('x\0y', 20), ('x', 0), ('x', 101), ('x', True)])
def test_broker_validates_query_and_limits(tree, q, limit):
    with pytest.raises(ops.OpError):
        search(tree, q, limit)


def test_result_limit_and_exact_boundary(tree):
    _, root = tree
    for n in range(3):
        (root / f'target{n}').touch()
    result = search(tree, 'target', limit=2)
    assert len(result['results']) == 2 and result['truncated']
    assert result['reason'] == 'result_limit'
    assert not search(tree, 'target', limit=3)['truncated']


def test_large_tree_entry_and_clock_budgets(tree, monkeypatch):
    from acc.broker import file_search
    _, root = tree
    for n in range(500):
        (root / f'file-{n}').touch()
    monkeypatch.setattr(file_search, 'MAX_VISITED', 50)
    result = search(tree, 'never-matches')
    assert result['visited'] == 50 and result['truncated']
    assert result['reason'] == 'entry_limit'
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(file_search.time, 'monotonic', lambda: next(ticks, 2))
    result = search(tree, 'never-matches')
    assert result['reason'] == 'time_limit' and result['truncated']
    assert result['visited'] <= 1


async def test_api_project_and_session_locations_and_validation(api, env, project):
    from helpers import wait_for, write_db
    from test_changes import repo
    from test_supervisor import state_is

    root = env['workspaces'] / 'demo'
    repo(root)
    (root / 'primary-note.md').touch()
    url = f"/api/projects/{project['id']}/files/search"
    response = await api.get(url, params={'q': 'primary-note'})
    assert response.status_code == 200, response.text
    assert paths(response.json()) == {'primary-note.md'}
    assert (await api.get(f"/api/files/project/{project['id']}/search", params={'q': 'primary-note'})).json() == response.json()
    for params in [{}, {'q': ''}, {'q': '../x'}, {'q': 'x', 'limit': 'bad'},
                   {'q': 'x', 'limit': 101}, {'q': 'x', 'limit': 0}]:
        assert (await api.get(url, params=params)).status_code == 422
    await api.post(f"/api/projects/{project['id']}/trust")
    made = (await api.post('/api/sessions', json={'project_id': project['id'], 'name': 'Search tree',
        'provider': 'claude', 'worktree': True})).json()
    await wait_for(state_is(api, made['id'], 'idle'))
    (Path(made['worktree']) / 'session-only.md').touch()
    session_url = f"/api/sessions/{made['id']}/files/search"
    assert paths((await api.get(session_url, params={'q': 'session-only'})).json()) == {'session-only.md'}
    assert not paths((await api.get(url, params={'q': 'session-only'})).json())
    # Offline is a refusal, never a successful empty search. No local path fallback.
    from acc.server.models import Node
    with write_db(api.app.state.db) as db:
        db.add(Node(id='offline-search', name='offline-search', public_key='unused', status='active'))
        from acc.server.models import Project
        db.get(Project, project['id']).host = 'offline-search'
        db.commit()
    offline = await api.get(url, params={'q': 'primary-note'})
    assert offline.status_code == 503, offline.text
    assert offline.json()['state'] == 'unavailable'
    assert 'results' not in offline.json()


async def test_search_uses_files_authorization_for_owner_member_and_private_project(api, env, project, member_accounts):
    from test_auth import do_setup
    from test_users import browser, csrf, join

    await do_setup(api, env)
    (env['workspaces'] / 'demo/owner-only.md').touch()
    owner_url = f"/api/projects/{project['id']}/files/search"
    async with browser(api) as member:
        link = (await api.post('/api/users/invites', json={'role': 'member', 'account': 'dana-agent'})).json()['link']
        await join(member, link, 'dana')
        for url in [owner_url, f"/api/files/project/{project['id']}/search"]:
            for params in [{}, {'q': ''}, {'q': 'x' * 201}, {'q': 'x', 'limit': 'bad'}]:
                assert (await member.get(url, params=params)).status_code == 404
        assert (await member.get(owner_url, params={'q': 'owner-only'})).status_code == 404
        for url in ['/api/sessions/not-found/files/search', '/api/files/session/not-found/search']:
            for params in [{}, {'q': '', 'limit': 'bad'}]:
                assert (await member.get(url, params=params)).status_code == 404
        mine = (await member.post('/api/projects', json={'name': 'Mine', 'slug': 'search-mine',
            'workspace_path': str(env['workspaces'] / 'demo')}, headers=csrf(member))).json()
        mine_url = f"/api/projects/{mine['id']}/files/search"
        assert (await member.get(mine_url, params={'q': 'owner-only'})).status_code == 200
        assert (await api.get(mine_url, params={'q': 'owner-only'})).status_code == 200
        async with browser(api) as other:
            link = (await api.post('/api/users/invites', json={'role': 'member', 'account': 'alice-agent'})).json()['link']
            await join(other, link, 'blair')
            assert (await other.get(mine_url, params={'q': 'owner-only'})).status_code == 404


def test_directory_swap_to_external_link_is_not_followed(tree, tmp_path, monkeypatch):
    from acc.broker import file_search
    _, root = tree
    (root / 'nested').mkdir()
    (root / 'nested/target-good').touch()
    outside = tmp_path / 'private'
    outside.mkdir()
    (outside / 'target-private').touch()
    real_open = file_search._open_dir
    def swap(fd, components):
        if components == ['nested']:
            (root / 'nested').rename(root / 'moved')
            (root / 'nested').symlink_to(outside, target_is_directory=True)
        return real_open(fd, components)
    monkeypatch.setattr(file_search, '_open_dir', swap)
    result = search(tree, 'target')
    assert not paths(result)
    assert result['truncated'] and result['reason'] == 'unreadable'


def test_depth_ignore_size_and_rule_budgets(tree, monkeypatch):
    from acc.broker import file_search
    _, root = tree
    (root / 'a/b/c').mkdir(parents=True)
    (root / 'a/b/c/target').touch()
    monkeypatch.setattr(file_search, 'MAX_DEPTH', 1)
    result = search(tree, 'target')
    assert not paths(result) and result['reason'] == 'depth_limit'
    (root / '.gitignore').write_text('x' * (file_search.MAX_IGNORE_BYTES + 1))
    assert search(tree, 'target')['reason'] == 'ignore_limit'
    monkeypatch.setattr(file_search, 'MAX_IGNORE_PATTERNS', 1)
    (root / '.gitignore').write_text('a\nb\n')
    assert search(tree, 'target')['reason'] == 'ignore_limit'


def test_ignore_symlink_is_not_read(tree, tmp_path):
    _, root = tree
    outside = tmp_path / 'private-ignore'
    outside.write_text('target\n')
    (root / '.gitignore').symlink_to(outside)
    (root / 'target').touch()
    result = search(tree, 'target')
    assert not paths(result) and result['truncated']


def test_mount_alias_is_not_walked(tree, monkeypatch):
    from acc.broker import file_search
    _, root = tree
    (root / 'alias').mkdir()
    (root / 'alias/target').touch()
    root_inode = root.stat().st_ino
    monkeypatch.setattr(file_search, 'mount_identity', lambda fd: 1 if os.fstat(fd).st_ino == root_inode else 2)
    result = search(tree, 'target')
    assert not paths(result) and result['reason'] == 'mount_boundary'


@pytest.mark.parametrize('allowed', [True, False])
def test_reviewed_root_moved_into_protected_location_refuses(tree, tmp_path, monkeypatch, allowed):
    from dataclasses import replace

    from acc import folder_policy
    from acc.broker import file_search
    config, root = tree
    config = replace(config, folder_revision=1, allowed_folders=allowed)
    (root / 'target-private').touch()
    moved = tmp_path / 'protected'
    def policy(_config, path):
        if str(path) != str(root):
            raise folder_policy.FolderError('Protected target.')
    monkeypatch.setattr(folder_policy, 'broker_path', policy)
    real_mount = file_search.mount_identity
    def swap(fd):
        identity = real_mount(fd)
        if root.exists():
            root.rename(moved)
        return identity
    monkeypatch.setattr(file_search, 'mount_identity', swap)
    with pytest.raises(ops.OpError, match='Protected target'):
        ops.file_search(config, str(root), q='target')


async def test_remote_search_uses_authenticated_machine_and_account_without_local_fallback(api, env, live):  # noqa: F811
    import asyncio

    from helpers import wait_for
    from test_multihost import StandInNode, enroll

    key, node_id = await enroll(api)
    node = StandInNode(env['settings'].broker_socket, key, node_id)
    task = asyncio.create_task(node.run(live))
    async def connected():
        return any(row['connected'] for row in (await api.get('/api/nodes')).json())
    try:
        await wait_for(connected)
        root = env['workspaces'] / 'remote-search'
        root.mkdir()
        (root / 'remote-marker.md').touch()
        project = (await api.post('/api/projects', json={'slug': 'remote-search', 'name': 'Remote search',
            'host': 'pc', 'workspace_path': str(root)})).json()
        result = await api.get(f"/api/projects/{project['id']}/files/search", params={
            'q': 'remote-marker', 'host': 'local', 'account': 'root', 'path': '/etc'})
        assert result.status_code == 200, result.text
        assert paths(result.json()) == {'remote-marker.md'}
        assert node.relayed >= 1 and node.opened and set(node.opened) == {'agent'}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


from test_auth import live  # noqa: E402,F401


async def test_unavailable_search_never_returns_transport_paths(api, project, monkeypatch):
    from acc.server.broker_client import BrokerUnavailable
    broker = api.app.state.ctx.broker
    def unavailable(*args, **params):
        raise BrokerUnavailable('broker socket /private/service/credential.sock: private-marker')
    monkeypatch.setattr(broker, 'fresh', unavailable)
    result = await api.get(f"/api/projects/{project['id']}/files/search", params={'q': 'hello'})
    assert result.status_code == 503
    assert result.json()['state'] == 'unavailable'
    assert '/private/' not in result.text and 'private-marker' not in result.text


def test_hardlinked_ignore_metadata_is_not_read(tree, tmp_path):
    _, root = tree
    original = tmp_path / 'private-ignore'
    original.write_text('target\n')
    os.link(original, root / '.gitignore')
    (root / 'target').touch()
    result = search(tree, 'target')
    assert not paths(result) and result['truncated']
    assert result['reason'] == 'ignore_limit'


def test_bad_nested_ignore_does_not_hide_siblings(tree, monkeypatch):
    from contextlib import contextmanager
    original = os.scandir
    @contextmanager
    def ordered(fd):
        with original(fd) as entries:
            yield iter(sorted(entries, key=lambda e: e.name))
    monkeypatch.setattr(os, 'scandir', ordered)
    _, root = tree
    (root / 'bad').mkdir()
    (root / 'bad/.gitignore').write_text('x' * 9000)
    (root / 'bad/target-hidden').touch()
    (root / 'target-visible').touch()
    result = search(tree, 'target')
    assert paths(result) == {'target-visible'}
    assert result['truncated'] and result['reason'] == 'ignore_limit'


async def test_stalled_search_deadline_does_not_block_stop(tree, monkeypatch):
    import asyncio
    import sys

    from acc.broker import search_worker
    from acc.broker.server import Broker
    from acc.server.broker_client import RETRYABLE_READS
    assert 'files_search' not in RETRYABLE_READS
    monkeypatch.setattr(search_worker, 'DEADLINE', 0.15)
    monkeypatch.setattr(search_worker, 'argv', lambda: [sys.executable, '-c', 'import time; time.sleep(60)'])
    broker = Broker(tree[0])
    broker.methods['interrupt'] = lambda p: {'stopped': True}
    pending = asyncio.create_task(broker._dispatch({'id': 1, 'method': 'files_search',
        'params': {'path': str(tree[1]), 'q': 'target'}}))
    await asyncio.sleep(0.05)
    stopped = await asyncio.wait_for(broker._dispatch({'id': 2, 'method': 'interrupt', 'params': {}}), 0.1)
    assert stopped['result'] == {'stopped': True}
    busy = await broker._dispatch({'id': 3, 'method': 'files_search', 'params': {'path': str(tree[1]), 'q': 'x'}})
    assert not busy['ok'] and busy['code'] == 'read_busy'
    result = await asyncio.wait_for(pending, 0.5)
    assert not result['ok'] and 'unavailable' in result['error'].lower()
    await asyncio.sleep(0.05)  # reap the killed child


@pytest.mark.parametrize('failure', ['unknown', 'fence', 'policy'])
async def test_all_execution_unavailability_has_same_shape(api, project, monkeypatch, failure):
    from acc.server.broker_client import BrokerError
    from acc.server.ha import Fenced
    def fresh(*args, **kwargs):
        if failure == 'fence':
            raise Fenced('private epoch detail')
        raise BrokerError('unknown method files_search' if failure == 'unknown' else 'Folder policy is pending')
    monkeypatch.setattr(api.app.state.ctx.broker, 'fresh', fresh)
    for url in [f"/api/projects/{project['id']}/files/search", f"/api/files/project/{project['id']}/search"]:
        response = await api.get(url, params={'q': 'hello'})
        assert response.status_code == 503
        assert set(response.json()) == {'state', 'detail'}
        assert response.json()['state'] == 'unavailable'
        assert 'private' not in response.text and 'unknown method' not in response.text


async def test_search_single_flight_aliases_and_dedicated_client(api, project, monkeypatch):
    import asyncio
    started, release = asyncio.Event(), asyncio.Event()
    closed = []
    class Client:
        async def call(self, method, **params):
            assert method == 'files_search'
            started.set()
            await release.wait()
            return {'results': [], 'visited': 0, 'truncated': False, 'reason': None}
        async def close(self):
            closed.append(True)
    broker = api.app.state.ctx.broker
    def fresh(*args, **kwargs):
        assert kwargs['timeout'] == 3
        return Client()
    monkeypatch.setattr(broker, 'fresh', fresh)
    original = broker.call
    async def shared(method, **params):
        assert method != 'files_search', 'search must not enter control client FIFO'
        return await original(method, **params)
    monkeypatch.setattr(broker, 'call', shared)
    url = f"/api/projects/{project['id']}/files/search"
    pending = asyncio.create_task(api.get(url, params={'q': 'first'}))
    await asyncio.wait_for(started.wait(), 2)
    try:
        duplicate = await api.get(f"/api/files/project/{project['id']}/search", params={'q': 'second'})
        assert duplicate.status_code == 429
    finally:
        release.set()
        assert (await pending).status_code == 200
    assert closed == [True]
    assert (await api.get(url, params={'q': 'third'})).status_code == 200


async def test_policy_change_discards_inflight_search(tree, monkeypatch):
    import asyncio
    from dataclasses import replace

    from acc.broker.server import Broker
    broker = Broker(tree[0])
    entered, release = asyncio.Event(), asyncio.Event()
    async def run(config, params):
        entered.set()
        await release.wait()
        return {'results': [{'path': 'must-not-return'}]}
    monkeypatch.setattr(broker._searches, 'run', run)
    pending = asyncio.create_task(broker._dispatch({'id': 1, 'method': 'files_search',
        'params': {'path': str(tree[1]), 'q': 'x'}}))
    await asyncio.wait_for(entered.wait(), 2)
    with broker._folder_lock:
        broker.config = replace(broker.config, folder_revision=2)
    release.set()
    result = await pending
    assert not result['ok'] and 'revision changed' in result['error']
    assert 'must-not-return' not in str(result)


async def test_cancelled_search_kills_child_and_releases_slot(tree, monkeypatch):
    import asyncio
    import sys

    from acc.broker import search_worker
    workers = search_worker.Searches()
    monkeypatch.setattr(search_worker, 'argv', lambda: [sys.executable, '-c', 'import time; time.sleep(60)'])
    pending = asyncio.create_task(workers.run(tree[0], {'path': str(tree[1]), 'q': 'x'}))
    await asyncio.sleep(0.1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    for _ in range(100):
        if not workers.active:
            break
        await asyncio.sleep(0.01)
    assert not workers.active


async def test_unreaped_search_worker_keeps_slot_without_waiting(tree, monkeypatch):
    import asyncio

    from acc.broker import search_worker
    release = asyncio.Event()
    class Process:
        returncode = None
        killed = False
        async def communicate(self, payload):
            await release.wait()
            self.returncode = -9
            return b'', b''
        def kill(self):
            self.killed = True
    process = Process()
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr(search_worker.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(search_worker, 'DEADLINE', 0.02)
    workers = search_worker.Searches()
    try:
        with pytest.raises(ops.OpError, match='deadline'):
            await asyncio.wait_for(workers.run(tree[0], {'path': str(tree[1]), 'q': 'x'}), 0.5)
        assert process.killed and workers.active
        with pytest.raises(search_worker.ReadBusy, match='busy'):
            await workers.run(tree[0], {'path': str(tree[1]), 'q': 'x'})
    finally:
        release.set()
        for _ in range(100):
            if not workers.active:
                break
            await asyncio.sleep(0.01)
    assert not workers.active


async def test_search_ignores_busy_control_lock_without_executor_queue(tree):
    import asyncio
    import threading

    from acc.broker.server import Broker
    broker = Broker(tree[0])
    held, release = threading.Event(), threading.Event()
    def hold():
        with broker._folder_lock:
            held.set()
            release.wait(3)
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert await asyncio.to_thread(held.wait, 1)
        result = await asyncio.wait_for(broker._dispatch({'id': 1, 'method': 'files_search',
            'params': {'path': str(tree[1]), 'q': 'x'}}), 2.5)
        assert result['ok'] and result['result']['results'] == []
    finally:
        release.set()
        thread.join(1)


async def test_busy_account_search_is_retryable_not_an_update_failure(api, project, monkeypatch):
    from acc.server.broker_client import BrokerBusy

    broker = api.app.state.ctx.broker
    original = broker.fresh

    def busy(*args, **kwargs):
        raise BrokerBusy('private admission details')

    monkeypatch.setattr(broker, 'fresh', busy)
    for url in [f"/api/projects/{project['id']}/files/search",
                f"/api/files/project/{project['id']}/search"]:
        response = await api.get(url, params={'q': 'hello'})
        assert response.status_code == 429
        assert response.json()['state'] == 'busy'
        assert response.headers['Retry-After'] == '1'
        assert 'update' not in response.text.lower()
        assert 'private admission' not in response.text
    monkeypatch.setattr(broker, 'fresh', original)
    assert (await api.get(url, params={'q': 'hello'})).status_code == 200


async def test_filename_search_admission_stays_immediate_while_a_search_runs(tree):
    import asyncio
    import time

    from acc.broker.search_worker import ReadBusy, Searches
    workers = Searches()
    # A held search (including an unreaped worker) cannot enqueue another walk.
    workers.active = True
    workers.operation = 'files_search'
    started = time.monotonic()
    with pytest.raises(ReadBusy):
        await asyncio.wait_for(workers.run(tree[0], {'path': str(tree[1]), 'q': 'x'}), .1)
    assert time.monotonic() - started < .1
    assert workers.active and not workers.waiters
