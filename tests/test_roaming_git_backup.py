"""Roaming git backups: scoped grants and the machine's own public leaf."""
import hashlib
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from helpers import pause_supervisor, write_db
from test_auth import live  # noqa: F401
from test_gitbackup import served  # noqa: F401
from test_machine_identities import authority, register  # noqa: F401

from acc.node.identity import public_key_b64
from acc.server import auth, gitbackup
from acc.server.models import Device, Node, Project, Setting
from acc.server.node_tasks import NodeTasks

NODE = 'a' * 32
OTHER = 'b' * 32
NODE_KEY, OTHER_KEY = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()  # the machines' link keys
LEAF = 'c' * 64
OTHER_LEAF = 'd' * 64


@pytest.fixture(autouse=True)
def no_unexpected_supervisor_stage_errors(caplog):
    """Stage-only fakes must not leak into the running scheduler, even in a passing test."""
    yield
    errors = [record for record in caplog.get_records('call')
              if record.name == 'acc.supervisor' and record.getMessage().startswith('supervisor stage ')
              and record.getMessage().endswith(' failed')]
    assert not errors, '\n'.join(record.getMessage() for record in errors)


def grant_headers(grant, pid, *, edge=None, key=NODE_KEY, channel=None):
    """The token request a machine sends: its grant, signed with its link key for the path it goes on (SR7-2)."""
    import base64
    channel = channel or ('public' if edge else 'private')
    headers = {'Authorization': f'BackupGrant {grant}',
               gitbackup.PROOF_HEADER: base64.b64encode(key.sign(gitbackup.grant_proof_message(channel, pid, grant))).decode()}
    return {**headers, 'X-Acc-Edge-Client': edge} if edge else headers


def enable(api, pid):
    with write_db(api.app.state.db) as s:
        s.add(Node(id=NODE, name='roamer', status='active', public_git_backup=True, public_key=public_key_b64(NODE_KEY)))
        s.add(Node(id=OTHER, name='other', status='active', public_key=public_key_b64(OTHER_KEY)))

        s.get(Project, pid).host = 'roamer'
        register(s, 'node', LEAF, person='', node_id=NODE)
        register(s, 'node', OTHER_LEAF, person='', node_id=OTHER)
        s.merge(Setting(key='remote_access', value={'machine_identities': True, 'public_hostname': 'remote.example.com'}))
        s.merge(Setting(key='remote_access_runtime', value={'enabled': True, 'desired_enabled': True,
                        'pending': False, 'hostname': 'remote.example.com'}))
        s.commit()


async def test_grant_exchanges_only_for_its_project_and_is_revoked_with_job(api, project):
    pid = project['id']
    enable(api, pid)
    supervisor = api.app.state.supervisor
    grants = supervisor.backup_grants
    grant = grants.mint(pid, NODE)
    path = f'/api/git/{pid}.git/token'
    headers = grant_headers(grant, pid)
    response = await api.post(path, headers=headers)
    assert response.status_code == 200, response.text
    token = response.json()['token']
    assert supervisor.backup_tokens.check(token, pid)
    # Retry after a lost token response gets the same job token, not another one.
    assert (await api.post(path, headers=headers)).json()['token'] == token
    assert (await api.post(f'/api/git/{pid + 1}.git/token', headers=grant_headers(grant, pid + 1))).status_code == 401
    assert (await api.post(path, headers={'Authorization': f'Bearer {token}'})).status_code == 401
    grants.revoke(grant)
    assert not supervisor.backup_tokens.check(token, pid)
    assert (await api.post(path, headers=headers)).status_code == 401


@pytest.mark.parametrize('leaf,status', [(LEAF, 200), (OTHER_LEAF, 403), ('e' * 64, 403)])
async def test_public_token_request_is_bound_to_own_machine(api, project, leaf, status):
    pid = project['id']
    enable(api, pid)
    grant = api.app.state.supervisor.backup_grants.mint(pid, NODE)
    response = await api.post(f'/api/git/{pid}.git/token', headers=grant_headers(grant, pid, edge=leaf))
    assert response.status_code == status, response.text


@pytest.mark.parametrize('change', ['revoked', 'expired', 'moved', 'identities-off', 'remote-off', 'pending', 'backups-off'])
async def test_token_request_refuses_revocation_and_disabled_policy(api, project, change):
    pid = project['id']
    enable(api, pid)
    grant = api.app.state.supervisor.backup_grants.mint(pid, NODE)
    with write_db(api.app.state.db) as s:
        if change == 'revoked':
            s.query(Device).filter_by(fingerprint=LEAF).one().status = 'revoked'
        elif change == 'expired':
            s.query(Device).filter_by(fingerprint=LEAF).one().expires_at = auth.now() - timedelta(seconds=1)
        elif change == 'moved':
            s.get(Project, pid).host = 'other'
        elif change == 'identities-off':
            s.get(Setting, 'remote_access').value = {'machine_identities': False}
        elif change in ('remote-off', 'pending'):
            row = s.get(Setting, 'remote_access_runtime')
            row.value = {**row.value, **({'enabled': False} if change == 'remote-off' else {'pending': True})}
        else:
            s.get(Project, pid).git_backup = False
        s.commit()
    response = await api.post(f'/api/git/{pid}.git/token', headers=grant_headers(grant, pid, edge=LEAF))
    assert response.status_code == 403, response.text


def test_expired_grant_never_mints_a_push_token():
    tokens = gitbackup.BackupTokens()
    grants = gitbackup.BackupGrants(tokens, ttl=-1)
    grant = grants.mint(7, NODE)
    assert grants.lookup(grant, 7) is None
    assert not tokens._tokens


async def test_host_reports_last_successful_public_backup_even_offline(api, project):
    from acc.server.models import Observation
    pid = project['id']
    enable(api, pid)
    with write_db(api.app.state.db) as s:
        s.add(Observation(key=f'backup.public.{NODE}', value={'at': '2026-10-25T10:00:00+00:00',
                          'project_id': pid}, source='git backup', observed_at=1))
        s.commit()
    view = (await api.get('/api/host?machine=roamer')).json()
    assert view['last_backup_via_public_address'] == '2026-10-25T10:00:00+00:00'


@pytest.mark.parametrize('expires_during_push', [False, True])
@pytest.mark.parametrize('concurrent_writer', [False, True])
async def test_supervisor_uses_scoped_node_job_and_records_public_success(api, env, project, monkeypatch, expires_during_push, concurrent_writer):
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy import text
    from test_gitbackup import make_repo

    from acc.server.db import _transaction_gate

    await pause_supervisor(api)
    pid = project['id']
    enable(api, pid)
    source = make_repo(env['workspaces'] / 'roaming-source')
    account = env['settings'].default_account
    with write_db(api.app.state.db) as s:
        row = s.get(Project, pid)
        row.workspace_path, row.account = str(source), account
        s.commit()
    supervisor = api.app.state.supervisor
    object.__setattr__(env['settings'], 'backup_url', 'https://acc.example.lan')

    async def broker_call(method, **params):
        assert method == 'git_refs'
        return {'git': True, 'digest': 'new'}

    held = {}
    async def job(payload, timeout):
        assert payload['public_origin'] == 'https://remote.example.com'
        assert payload['private_origin'] == 'https://acc.example.lan'
        assert payload['project_id'] == pid and payload['account'] == account
        assert 'token' not in payload and 'certificate' not in payload
        assert supervisor.backup_grants.lookup(payload['grant'], pid)['node'] == NODE
        response = await api.post(f'/api/git/{pid}.git/token', headers=grant_headers(payload['grant'], pid, edge=LEAF))
        assert response.status_code == 200, response.text
        # The node checks the token against this before pushing (an impostor can't produce it).
        assert payload['token_sha256'] == hashlib.sha256(response.json()['token'].encode()).hexdigest()
        if expires_during_push:
            supervisor.backup_grants.lookup(payload['grant'], pid)['until'] = 0
        if concurrent_writer:
            # The public status transaction must wait off-loop, so the coroutine
            # holding this independent SQLite writer can release it.
            writer = api.app.state.db()
            writer.execute(text('UPDATE attention_item SET title = title'))
            started = asyncio.Event()
            loop = asyncio.get_running_loop()
            engine = api.app.state.db.kw['bind']
            gate = _transaction_gate(engine)
            enter = gate.enter
            def starting(worker, timeout):
                loop.call_soon_threadsafe(started.set)
                return enter(worker, timeout)
            monkeypatch.setattr(gate, 'enter', starting)
            async def release():
                await started.wait()
                await asyncio.sleep(.05)
                writer.rollback()
                writer.close()
            held.update(writer=writer, started=started, gate=gate, enter=enter,
                        release=asyncio.create_task(release()))
        return {'via': 'public', 'digest': 'new', 'updated': ['refs/heads/main'],
                'kept': [{'ref': 'refs/heads/main', 'as': 'refs/heads/backup/example/main', 'reason': 'rewritten'}] if concurrent_writer else []}

    link = SimpleNamespace(node_id=NODE, closed=False, host_capable=False, tasks=NodeTasks("roamer"), info={'capabilities': ['git-backup-v1']}, git_backup=job)
    # These fakes implement the backup stage, not the scheduler's other broker methods.
    assert api.supervisor_task.done(), 'stop and join the scheduler before installing a stage-only fake'
    monkeypatch.setattr(supervisor.broker, 'call', broker_call)
    supervisor.broker.hub.links['roamer'] = link
    try:
        result = await supervisor.backup_project(pid, force=True)
        assert result.get('via') == 'public', result
        assert not supervisor.backup_grants._grants
        view = (await api.get('/api/host?machine=roamer')).json()
        assert view['last_backup_via_public_address'] == result['last_at']
    finally:
        supervisor.broker.hub.links.pop('roamer', None)
        if held:
            held['started'].set()
            await held['release']
            monkeypatch.setattr(held['gate'], 'enter', held['enter'])


async def test_loopback_forwarder_carries_a_real_streamed_git_push(api, env, project, served):  # noqa: F811
    import asyncio
    import os

    import httpx
    from test_gitbackup import bare_refs, commit, git, make_repo

    from acc.broker import ops
    from acc.node.git_backup import forward_app, local_forwarder

    pid = project['id']
    source = make_repo(env['workspaces'] / 'forwarded')
    (source / 'pack.bin').write_bytes(os.urandom(2 << 20))
    head = commit(source, 'pack.bin', None)
    with api.app.state.db() as s:
        owner = gitbackup.identity(s.get(Project, pid))
    await asyncio.to_thread(gitbackup.ensure_repo, env['settings'].git_dir, pid, owner)
    token = api.app.state.supervisor.backup_tokens.mint(pid)
    async with httpx.AsyncClient(trust_env=False) as upstream:
        async with local_forwarder(forward_app(upstream, served, pid, token)) as local:
            result = await asyncio.to_thread(ops.git_backup, env['broker'], str(source),
                                             f'{local}/api/git/{pid}.git', token)
    assert result['updated'] == ['refs/heads/main']
    assert bare_refs(env, pid)['refs/heads/main'] == head
    assert git(source, 'rev-parse', 'HEAD').stdout.strip() == head


@pytest.mark.parametrize('token_busy', [False, True])
@pytest.mark.parametrize('private_unreachable', [False, True])
async def test_node_job_pushes_real_git_via_selected_path(api, env, project, authority, tmp_path, monkeypatch, private_unreachable, token_busy):  # noqa: F811
    import asyncio
    import getpass
    import ssl

    import httpx
    from test_gitbackup import bare_refs, git, make_repo
    from test_machine_identities import fingerprint

    from acc.node import machine
    from acc.node.config import NodeConfig
    from acc.node.git_backup import run_job

    pid = project['id']
    enable(api, pid)
    state = tmp_path / 'identity'
    state.mkdir(mode=0o700)
    ca, directory = authority
    request = machine.new_request(state, NODE)
    certificate = ca.issue(directory, {'operation': 'issue_machine', 'node_id': NODE, 'csr': request, 'days': 30})['certificate']
    assert machine.accept(state, certificate)
    leaf = fingerprint(certificate)
    assert machine.switch(state, leaf)
    source = make_repo(env['workspaces'] / 'node-job')
    with write_db(api.app.state.db) as s:
        row = s.query(Device).filter_by(fingerprint=LEAF).one()
        row.fingerprint, row.certificate_pem = leaf, certificate
        owner = gitbackup.identity(s.get(Project, pid))
        s.commit()
    await asyncio.to_thread(gitbackup.ensure_repo, env['settings'].git_dir, pid, owner)
    grants = api.app.state.supervisor.backup_grants
    grant = grants.mint(pid, NODE)
    account = getpass.getuser()
    (env['broker'].socket_path.parent / f'{account}.sock').symlink_to(env['broker'].socket_path)
    config = NodeConfig(primary='https://acc.example.lan', node_id=NODE, socket_dir=env['broker'].socket_path.parent)
    seen = []
    asgi = httpx.ASGITransport(api.app)
    async def routed(request):
        seen.append((request.url.host, request.url.path))
        assert request.url.path.startswith(f'/api/git/{pid}.git/')
        if request.url.host == 'acc.example.lan' and private_unreachable:
            raise httpx.ConnectError('private address unreachable')
        if token_busy and request.url.path.endswith('/token'):
            return httpx.Response(503, headers={'Retry-After': '1'})
        if request.url.host == 'remote.example.com':
            request.headers['X-Acc-Edge-Client'] = leaf  # the real edge's header is tested in the Caddy suite
        return await asgi.handle_async_request(request)
    real_client = httpx.AsyncClient
    contexts = []
    def client(*args, **kwargs):
        assert kwargs['verify'].verify_mode == ssl.CERT_REQUIRED and kwargs['verify'].check_hostname
        contexts.append(kwargs['verify'])
        kwargs['transport'] = httpx.MockTransport(routed)
        return real_client(*args, **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    from acc.node import git_backup as transport
    edge_leaf = x509.load_pem_x509_certificate(certificate.encode()).public_bytes(Encoding.DER)  # any certificate
    probed = []
    async def server_leaf(origin, context):
        probed.append(origin)
        return edge_leaf
    monkeypatch.setattr(transport, 'server_leaf', server_leaf)
    real_trusting_only, pins = transport.trusting_only, []
    def trusting_only(leaf):
        pins.append((leaf, real_trusting_only(leaf)))
        return pins[-1][1]
    monkeypatch.setattr(transport, 'trusting_only', trusting_only)
    try:
        result = await run_job(config, state, {'project_id': pid, 'account': account, 'path': str(source),
                               'private_origin': config.primary, 'public_origin': 'https://remote.example.com', 'grant': grant,
                               'token_sha256': grants.commitment(grant, pid)},
                               link_url='wss://acc.example.lan/api/nodes/connect', key=NODE_KEY)
        if token_busy:
            assert {host for host, _ in seen} == ({'acc.example.lan', 'remote.example.com'} if private_unreachable else {'acc.example.lan'})
            assert result == {'retry': True}
            assert bare_refs(env, pid) == {}
            return
        assert result['via'] == ('public' if private_unreachable else 'private')
        assert bare_refs(env, pid)['refs/heads/main'] == git(source, 'rev-parse', 'HEAD').stdout.strip()
        assert {host for host, _ in seen} == ({'acc.example.lan', 'remote.example.com'} if private_unreachable else {'acc.example.lan'})
        # SR7-1: the public client (token request and push alike) trusts only the leaf one verified connection showed.
        assert probed == (['https://remote.example.com'] if private_unreachable else [])
        if private_unreachable:
            assert [leaf for leaf, _ in pins] == [edge_leaf] and contexts[-1] is pins[0][1]
    finally:
        grants.revoke(grant)


async def test_push_advertisement_requires_own_active_leaf_and_public_policy(api, env, project):
    import asyncio
    pid = project['id']
    enable(api, pid)
    with api.app.state.db() as s:
        owner = gitbackup.identity(s.get(Project, pid))
    await asyncio.to_thread(gitbackup.ensure_repo, env['settings'].git_dir, pid, owner)
    token = api.app.state.supervisor.backup_tokens.mint(pid)
    path = f'/api/git/{pid}.git/info/refs?service=git-receive-pack'
    headers = {'Authorization': 'Bearer ' + token, 'X-Acc-Edge-Client': LEAF}
    assert (await api.get(path, headers=headers)).status_code == 200
    assert (await api.get(path, headers={**headers, 'X-Acc-Edge-Client': OTHER_LEAF})).status_code == 403
    assert (await api.get(path, headers={**headers, 'X-Acc-Edge-Client': ''})).status_code == 403
    with write_db(api.app.state.db) as s:
        runtime = s.get(Setting, 'remote_access_runtime')
        runtime.value = {**runtime.value, 'enabled': False}
        s.commit()
    assert (await api.get(path, headers=headers)).status_code == 403
    # Private backups keep their existing token authorization with remote access off.
    assert (await api.get(path, headers={'Authorization': 'Bearer ' + token})).status_code == 200


def test_push_token_cannot_outlive_its_job_grant(monkeypatch):
    from types import SimpleNamespace
    clock = [100.0]
    monkeypatch.setattr(gitbackup, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    tokens = gitbackup.BackupTokens(ttl=600)
    grants = gitbackup.BackupGrants(tokens, ttl=10)
    grant = grants.mint(7, NODE)
    clock[0] = 109.0
    token = grants.exchange(grant, 7)
    assert tokens.check(token, 7)
    clock[0] = 110.0
    assert not tokens.check(token, 7)


async def test_token_exchange_waits_for_writer_without_blocking_node_loop(api, project, monkeypatch):
    import asyncio

    from sqlalchemy import event, text

    from acc.server.db import _transaction_gate

    pid = project['id']
    enable(api, pid)
    api.supervisor_task.cancel()
    await asyncio.gather(api.supervisor_task, return_exceptions=True)
    await asyncio.gather(*api.supervisor_threads, return_exceptions=True)
    grant = api.app.state.supervisor.backup_grants.mint(pid, NODE)
    db = api.app.state.db
    engine = db.kw['bind']
    def short_wait(raw, *_):
        raw.execute('PRAGMA busy_timeout=200')
    event.listen(engine, 'checkout', short_wait)
    writer = db()
    writer.execute(text('UPDATE attention_item SET title = title'))
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    gate = _transaction_gate(engine)
    enter = gate.enter
    def starting(worker, timeout):
        loop.call_soon_threadsafe(started.set)
        return enter(worker, timeout)
    monkeypatch.setattr(gate, 'enter', starting)
    async def release():
        await started.wait()
        await asyncio.sleep(.05)
        writer.rollback()
        writer.close()
    release_task = asyncio.create_task(release())
    try:
        response = await api.post(f'/api/git/{pid}.git/token', headers=grant_headers(grant, pid))
        assert response.status_code == 200, response.text
    finally:
        started.set()
        await release_task
        event.remove(engine, 'checkout', short_wait)


def roaming_source(api, env, pid, name):
    from test_gitbackup import make_repo
    source = make_repo(env['workspaces'] / name)
    with write_db(api.app.state.db) as s:
        row = s.get(Project, pid)
        row.workspace_path, row.account, row.host = str(source), env['settings'].default_account, 'roamer'
        s.commit()


@pytest.mark.parametrize('base', ['https://acc.example.lan/acc', 'http://acc.example.lan'])
async def test_a_backup_url_the_node_job_cannot_use_keeps_the_broker_push(api, env, project, monkeypatch, base):
    from types import SimpleNamespace

    await pause_supervisor(api)
    pid = project['id']
    enable(api, pid)
    roaming_source(api, env, pid, 'path-base')
    supervisor = api.app.state.supervisor
    object.__setattr__(env['settings'], 'backup_url', base)
    pushed = []

    class Broker:
        async def call(self, method, **params):
            pushed.append((method, params['url']))
            return {'digest': 'new', 'updated': []}

        async def close(self):
            pass

    async def broker_call(method, **params):
        return {'git': True, 'digest': 'new'}

    async def job(payload, timeout):
        raise AssertionError('the node job only takes a bare HTTPS backup origin')

    # These fakes implement the backup stage, not the scheduler's other broker methods.
    assert api.supervisor_task.done(), 'stop and join the scheduler before installing a stage-only fake'
    monkeypatch.setattr(supervisor.broker, 'call', broker_call)
    monkeypatch.setattr(supervisor.broker, 'fresh', lambda account, host, timeout: Broker())
    supervisor.broker.hub.links['roamer'] = SimpleNamespace(node_id=NODE, closed=False, host_capable=False, tasks=NodeTasks("roamer"),
                                                            info={'capabilities': ['git-backup-v1']}, git_backup=job)
    try:
        result = await supervisor.backup_project(pid, force=True)
    finally:
        supervisor.broker.hub.links.pop('roamer', None)
    assert 'error' not in result, result
    assert pushed == [('git_backup', f'{base}/api/git/{pid}.git')]
    assert result['via'] == 'private'


async def test_backups_of_two_projects_on_one_machine_wait_for_each_other(api, env, project, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    await pause_supervisor(api)
    first = project['id']
    response = await api.post('/api/projects', json={'slug': 'second', 'name': 'Second',
                                                     'workspace_path': str(env['workspaces'] / 'second')})
    assert response.status_code == 201, response.text
    second = response.json()['id']
    enable(api, first)
    roaming_source(api, env, first, 'first-source')
    roaming_source(api, env, second, 'second-source')
    supervisor = api.app.state.supervisor
    object.__setattr__(env['settings'], 'backup_url', 'https://acc.example.lan')
    running = []

    async def broker_call(method, **params):
        return {'git': True, 'digest': 'new'}

    async def job(payload, timeout):
        if running:  # what the node link and the node both answer to a second job
            raise RuntimeError('A git backup is already running on this machine')
        running.append(payload['project_id'])
        try:
            response = await api.post(f"/api/git/{payload['project_id']}.git/token",
                                      headers=grant_headers(payload['grant'], payload['project_id']))
            assert response.status_code == 200, response.text
            await asyncio.sleep(.05)
        finally:
            running.clear()
        return {'via': 'private', 'digest': 'new', 'updated': []}

    # These fakes implement the backup stage, not the scheduler's other broker methods.
    assert api.supervisor_task.done(), 'stop and join the scheduler before installing a stage-only fake'
    monkeypatch.setattr(supervisor.broker, 'call', broker_call)
    supervisor.broker.hub.links['roamer'] = SimpleNamespace(node_id=NODE, closed=False, host_capable=False, tasks=NodeTasks("roamer"),
                                                            info={'capabilities': ['git-backup-v1']}, git_backup=job)
    try:
        results = await asyncio.gather(supervisor.backup_project(first, force=True),
                                       supervisor.backup_project(second, force=True))
    finally:
        supervisor.broker.hub.links.pop('roamer', None)
    assert [r.get('error') for r in results] == [None, None], results


async def test_the_first_public_token_exchange_is_audited_once_without_secrets(api, project):
    import json

    from acc.server.models import AuditLog
    pid = project['id']
    enable(api, pid)
    grant = api.app.state.supervisor.backup_grants.mint(pid, NODE)
    path = f'/api/git/{pid}.git/token'

    def audited():
        with api.app.state.db() as s:
            return [(r.actor, r.target, r.detail) for r in s.query(AuditLog).filter_by(action='git_backup.public_token')]

    assert (await api.post(path, headers=grant_headers(grant, pid))).status_code == 200
    assert audited() == []  # the private path is the normal one
    for _ in range(2):  # a retry after a lost response is the same exchange
        response = await api.post(path, headers=grant_headers(grant, pid, edge=LEAF))
        assert response.status_code == 200, response.text
    token = response.json()['token']
    rows = audited()
    assert rows == [('node:roamer', 'demo', {'project_id': pid, 'node_id': NODE})]
    assert token not in json.dumps(rows) and grant not in json.dumps(rows)


async def test_supervisor_defers_node_token_503_without_accepting_success(api, env, project, monkeypatch):
    from types import SimpleNamespace

    from test_gitbackup import make_repo
    await pause_supervisor(api)
    pid = project['id']
    enable(api, pid)
    make_repo(env['workspaces'] / 'demo')
    supervisor = api.app.state.supervisor
    object.__setattr__(env['settings'], 'backup_url', 'https://acc.example.lan')
    async def refs(*a, **kw):
        return {'git': True, 'digest': 'new'}
    async def job(*a, **kw):
        return {'retry': True}  # token endpoint returned 503 before authenticating a push path
    monkeypatch.setattr(supervisor.broker, 'call', refs)
    supervisor.broker.hub.links['roamer'] = SimpleNamespace(node_id=NODE, closed=False, host_capable=False,
                                        info={'capabilities': ['git-backup-v1']}, git_backup=job)
    try:
        result = await supervisor.backup_project(pid, force=True)
        assert result == {}
        assert supervisor.backup_status(pid) == {}
        assert pid not in supervisor._backup_due and pid in supervisor._backup_deferred
        assert not supervisor.backup_grants._grants
    finally:
        supervisor.broker.hub.links.pop('roamer', None)


@pytest.mark.parametrize('result,accepted', [({'retry': True}, True), ({'retry': 1}, False),
                                             ({'retry': False}, False), ({'retry': True, 'digest': 'fake'}, False)])
async def test_node_link_accepts_deferral_without_claimed_push_path(result, accepted):
    import asyncio

    from acc.server.nodes import NodeLink
    link = object.__new__(NodeLink)
    future = asyncio.get_running_loop().create_future()
    link.git_backup_waiters = {'job': future}
    link.git_backup_result({'id': 'job', 'ok': True, 'result': result})
    if accepted:
        assert await future == {'retry': True}
    else:
        with pytest.raises(RuntimeError, match="project backup result Rynhult can't use"):
            await future
@pytest.mark.parametrize('edge', [None, LEAF], ids=['private', 'public'])
async def test_a_grant_works_only_signed_by_its_machine_for_its_path(api, project, edge):
    """Security review 7, SR7-2: a grant shown to a fake public server must be useless at the private address (and the
    other way round), and useless to anyone without the machine's link key."""
    pid = project['id']
    enable(api, pid)
    grant = api.app.state.supervisor.backup_grants.mint(pid, NODE)
    path = f'/api/git/{pid}.git/token'
    other_path = 'private' if edge else 'public'
    refused = [
        {'Authorization': f'BackupGrant {grant}', **({'X-Acc-Edge-Client': edge} if edge else {})},  # no proof
        grant_headers(grant, pid, edge=edge, key=OTHER_KEY),  # another machine's key
        grant_headers(grant, pid, edge=edge, channel=other_path),  # signed for the other path
        {**grant_headers(grant, pid, edge=edge), gitbackup.PROOF_HEADER: 'not base64!'},
        {**grant_headers(grant, pid, edge=edge),
         gitbackup.PROOF_HEADER: grant_headers('x' * 43, pid, edge=edge)[gitbackup.PROOF_HEADER]},  # another grant's proof
    ]
    for headers in refused:
        response = await api.post(path, headers=headers)
        assert response.status_code == 403 and 'signed by the machine' in response.text, (headers, response.text)
    response = await api.post(path, headers=grant_headers(grant, pid, edge=edge))
    assert response.status_code == 200, response.text


async def test_a_failed_audit_write_leaves_the_next_public_exchange_audited(api, project, monkeypatch):
    """Security review 7, SR7-4: the "audited" mark used to be set before the write, so a failed write left the grant's
    public exchanges unaudited."""
    import json

    from acc.server.api import backups
    from acc.server.models import AuditLog

    pid = project['id']
    enable(api, pid)
    grant = api.app.state.supervisor.backup_grants.mint(pid, NODE)
    path = f'/api/git/{pid}.git/token'
    real, calls = backups.AuditLog, []
    def failing(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError('database unavailable')
        return real(**kwargs)
    monkeypatch.setattr(backups, 'AuditLog', failing)
    with pytest.raises(RuntimeError):
        await api.post(path, headers=grant_headers(grant, pid, edge=LEAF))
    response = await api.post(path, headers=grant_headers(grant, pid, edge=LEAF))
    assert response.status_code == 200, response.text
    with api.app.state.db() as s:
        rows = [r.detail for r in s.query(AuditLog).filter_by(action='git_backup.public_token')]
    assert rows == [{'project_id': pid, 'node_id': NODE}] and grant not in json.dumps(rows)




@pytest.mark.parametrize('withdraw_while_waiting', [False, True])
async def test_without_consent_backup_uses_private_broker_even_after_queue_wait(
        api, env, project, monkeypatch, withdraw_while_waiting):
    import asyncio
    from types import SimpleNamespace

    await pause_supervisor(api)  # Broker fakes below describe backup traffic, not background auth.

    pid = project['id']
    enable(api, pid)
    roaming_source(api, env, pid, 'consent-source')
    supervisor = api.app.state.supervisor
    base = 'https://acc.example.lan'
    object.__setattr__(env['settings'], 'backup_url', base)
    entered = asyncio.Event()
    pushed = []

    class QueueLock(asyncio.Lock):
        async def __aenter__(self):
            entered.set()
            return await super().__aenter__()

    lock = QueueLock()
    supervisor._node_backup_locks[NODE] = lock
    if withdraw_while_waiting:
        await lock.acquire()
    else:
        with write_db(api.app.state.db) as db:
            db.get(Node, NODE).public_git_backup = False
            db.commit()

    class Broker:
        async def call(self, method, **params):
            pushed.append((method, params['url']))
            return {'digest': 'new', 'updated': []}

        async def close(self):
            pass

    async def broker_call(method, **params):
        return {'git': True, 'digest': 'new'}

    async def job(payload, timeout):
        raise AssertionError('public backup job issued without owner consent')

    monkeypatch.setattr(supervisor.broker, 'call', broker_call)
    monkeypatch.setattr(supervisor.broker, 'fresh', lambda account, host, timeout: Broker())
    supervisor.broker.hub.links['roamer'] = SimpleNamespace(node_id=NODE, closed=False, host_capable=False,
        info={'capabilities': ['git-backup-v1']}, git_backup=job)
    task = asyncio.create_task(supervisor.backup_project(pid, force=True))
    try:
        if withdraw_while_waiting:
            await asyncio.wait_for(entered.wait(), 10)
            with write_db(api.app.state.db) as db:
                db.get(Node, NODE).public_git_backup = False
                db.commit()
            lock.release()
        result = await asyncio.wait_for(task, 15)
        assert 'error' not in result, result
        assert pushed == [('git_backup', f'{base}/api/git/{pid}.git')]
        assert result['via'] == 'private'
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        supervisor.broker.hub.links.pop('roamer', None)
        supervisor._node_backup_locks.pop(NODE, None)

