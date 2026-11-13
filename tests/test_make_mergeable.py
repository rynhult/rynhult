"""Focused mergeability checks: real Git conflicts and authenticated workflow decisions."""
# ruff: noqa: F811
import subprocess

import httpx
import pytest
from helpers import write_db
from test_auth import do_setup
from test_pulls import fake, world  # noqa: F401
from test_session_git import checkout  # noqa: F401
from test_users import csrf

from acc.broker import ops, session_git
from acc.server import claude_auth, guardrails
from acc.server.models import Session


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, text=True, capture_output=True).stdout.strip()


@pytest.mark.parametrize('conflict', [False, True])
@pytest.mark.parametrize('mode', ['rebase', 'merge'])
def test_update_base_clean_or_conflicted(checkout, monkeypatch, conflict, mode):
    config, root = checkout
    (root / 'shared').write_text('original\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'shared ancestor')
    ancestor = git(root, 'rev-parse', 'HEAD')
    (root / ('shared' if conflict else 'topic')).write_text('topic\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'topic')
    topic = git(root, 'rev-parse', 'HEAD')
    git(root, 'checkout', '-qb', 'base', ancestor)
    (root / 'shared').write_text('base\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'base')
    base = git(root, 'rev-parse', 'HEAD')
    git(root, 'checkout', 'feature')
    git(root, 'config', 'pull.rebase', 'false' if mode == 'merge' else 'true')
    hook = root / '.git/hooks/post-checkout'
    hook.write_text('#!/bin/sh\ntouch ' + str(root / 'hook-ran') + '\n')
    hook.chmod(0o755)
    monkeypatch.setattr(session_git, 'transport', lambda *a, **kw: subprocess.CompletedProcess([], 0, '', ''))
    result = session_git.update_base(config, str(root), 'feature', 'example/shop', 'base', base, head=topic, mode=mode, force_push_blocked=False, person='casey')
    assert result['mode'] == mode and result['ok'] is not conflict
    assert result['conflicts'] == (['shared'] if conflict else [])
    assert not (root / 'hook-ran').exists()
    if conflict:
        assert git(root, 'diff', '--name-only', '--diff-filter=U') == 'shared'
        assert session_git.operate(config, str(root), 'status')['conflicts'] == ['shared']
        with pytest.raises(ops.OpError, match='in progress'):
            session_git.update_base(config, str(root), 'feature', 'example/shop', 'base', base, head=topic, mode=mode, force_push_blocked=False, person='casey')
        git(root, mode, '--abort')
        assert git(root, 'rev-parse', 'HEAD') == topic
    else:
        git(root, 'merge-base', '--is-ancestor', base, 'HEAD')
        assert (root / 'topic').read_text() == 'topic\n'


def test_base_update_refuses_dirty_default_and_custom_rules(checkout):
    config, root = checkout
    sha = git(root, 'rev-parse', 'HEAD')
    (root / 'dirty').write_text('unsaved')
    with pytest.raises(ops.OpError, match='Commit or discard'):
        session_git.update_base(config, str(root), 'feature', 'example/shop', 'main', sha, head=sha, mode='rebase', force_push_blocked=False, person='casey')
    (root / 'dirty').unlink()
    with pytest.raises(ops.OpError, match='default branch'):
        session_git.update_base(config, str(root), 'feature', 'example/shop', 'feature', sha, head=sha, mode='rebase', force_push_blocked=False, person='casey')
    with pytest.raises(ops.OpError, match='project guardrail'):
        session_git.update_base(config, str(root), 'feature', 'example/shop', 'main', sha, head=sha, mode='rebase', force_push_blocked=False, person='casey', custom=[{'pattern': 'git rebase'}])


async def human(api, env):
    await do_setup(api, env)
    api.headers.pop('Authorization', None)
    api.headers.update(csrf(api))


async def test_installation_token_cannot_merge_or_run_guided_mutations(api, world, fake):
    fake.open_pull()
    for suffix, body in [('merge', {'method': 'squash', 'sha': 'a' * 40, 'confirm': True}),
                         ('mergeable/base', {'mode': 'merge'}), ('mergeable/rerun', {}), ('mergeable/feedback', {}),
                         ('mergeable/push/unknown', {}), ('mergeable/completed-rebase', {'sha': 'a' * 40, 'confirm': True})]:
        response = await api.post(world['url'] + '/' + suffix, json=body)
        assert response.status_code == 403, response.text
    assert not fake.merged


@pytest.mark.parametrize('provider,automatic', [('claude', False), ('claude-api', True), ('codex', True), ('opencode', True)])
async def test_guided_feedback_subscription_is_preview_only_and_actor_audited(api, env, world, fake, monkeypatch, provider, automatic):
    fake.open_pull()
    fake.check('test', 'failure')
    with write_db(api.app.state.db) as db:
        session = db.get(Session, world['session']['id'])
        session.provider = 'claude' if provider == 'claude-api' else provider
        if provider == 'claude-api':
            session.claude_auth = 'api_key'
            claude_auth.store(db, session, 'api_key')
        db.commit()
    sent = []
    async def send(acc_id, actor, message):
        sent.append((acc_id, actor, message))
    monkeypatch.setattr(api.app.state.supervisor, 'send_text', send)
    await human(api, env)
    result = await api.post(world['url'] + '/mergeable/feedback')
    assert result.status_code == 200, result.text
    preview = result.json()
    assert preview['sent'] is automatic and bool(sent) is automatic
    assert 'untrusted-ci-output' in preview['text'] and len(preview['text']) <= 4000
    action = 'pr.send_to_agent' if automatic else 'pr.feedback_preview'
    rows = (await api.get('/api/audit', params={'action': action})).json()
    assert any(row['actor'] == 'casey' and row['action'] == action for row in rows)


async def test_missing_person_consent_refuses_merge(api, env, world, fake):
    fake.open_pull()
    await human(api, env)
    response = await api.post(world['url'] + '/merge', json={'method': 'squash', 'sha': 'a' * 40})
    assert response.status_code == 409 and not fake.merged


async def test_person_rebase_lease_security_protection_and_audit(api, env, world, fake, monkeypatch):
    fake.open_pull()
    await human(api, env)
    sid, path = world['session']['id'], world['path']
    with write_db(api.app.state.db) as db:
        db.get(Session, sid).worktree = str(path)
        db.commit()
    calls = []
    broker = api.app.state.supervisor.broker
    original = broker.call
    async def call(method, **kwargs):
        if method == 'session_update_base':
            calls.append((method, kwargs))
            return {'ok': True, 'mode': 'rebase', 'before': 'a' * 40, 'commit': 'c' * 40, 'conflicts': [], 'output': 'done\u202e\u200b\x1b[31m'}
        if method == 'session_git' and kwargs.get('lease'):
            calls.append((method, kwargs))
            return {'ok': True, 'output': 'pushed\u202e\u200b'}
        return await original(method, **kwargs)
    monkeypatch.setattr(broker, 'call', call)
    policy = guardrails.policy
    blocked = [False]
    def allow(*args, **kwargs):
        result = policy(*args, **kwargs)
        result['rules']['force_push'] = blocked[0]
        return result
    monkeypatch.setattr(guardrails, 'policy', allow)
    base = world['url'] + '/mergeable'
    fake.protection['feature/login'] = {'enabled': True}
    denied = await api.post(base + '/base', json={'mode': 'rebase'})
    assert denied.status_code == 403 and not calls
    fake.protection.pop('feature/login')
    response = await api.post(base + '/base', json={'mode': 'rebase'})
    assert response.status_code == 200, response.text
    assert response.json()['output'] == 'done[U+202E][U+200B]'
    ticket = response.json()['push_ticket']
    assert calls[0][1]['person'] == 'casey' and calls[0][1]['path'] == str(path)
    assert calls[0][1]['head'] == 'a' * 40 and calls[0][1]['mode'] == 'rebase'
    blocked[0] = True
    denied = await api.post(base + '/push/' + ticket)
    assert denied.status_code == 403 and 'Security' in denied.text
    blocked[0] = False
    pushed = await api.post(base + '/push/' + ticket)
    assert pushed.status_code == 200 and pushed.json()['ok']
    assert pushed.json()['output'] == 'pushed[U+202E][U+200B]'
    assert calls[-1][1]['lease'] == 'a' * 40 and calls[-1][1]['expected_head'] == 'c' * 40
    assert (await api.post(base + '/push/' + ticket)).status_code == 409
    rows = (await api.get('/api/audit', params={'action': 'pr.base'})).json()
    assert any(r['actor'] == 'casey' and r['detail'].get('ok') for r in rows)


async def test_ci_rerun_deduplicates_jobs_on_current_head(api, env, world, fake, monkeypatch):
    fake.open_pull()
    fake.check('test', 'failure', job=21)
    fake.check('lint', 'failure', job=22)
    fake.jobs.update({21: {'run_id': 7}, 22: {'run_id': 7}})
    original = fake.respond
    def respond(method, path, query, body):
        if method == 'POST' and path.endswith('/actions/runs/7/rerun-failed-jobs'):
            return httpx.Response(201, json=None)
        return original(method, path, query, body)
    monkeypatch.setattr(fake, 'respond', respond)
    await human(api, env)
    response = await api.post(world['url'] + '/mergeable/rerun')
    assert response.status_code == 200 and response.json() == {'runs': [7]}
    assert len([c for c in fake.calls if c[0] == 'POST' and c[1].endswith('rerun-failed-jobs')]) == 1
    rows = (await api.get('/api/audit', params={'action': 'pr.rerun'})).json()
    assert any(r['actor'] == 'casey' and r['detail'].get('runs') == [7] for r in rows)


def test_lease_push_pins_reviewed_commit_even_if_checkout_moves(checkout, monkeypatch):
    config, root = checkout
    git(root, 'remote', 'add', 'origin', 'https://github.com/example/shop.git')
    reviewed = git(root, 'rev-parse', 'HEAD')
    with pytest.raises(ops.OpError, match='head changed'):
        session_git.operate(config, str(root), 'push', branch='feature', default_branch='main', repo='example/shop',
                            lease='a' * 40, expected_head='b' * 40)
    original_transport = session_git.transport
    def transport(cwd, args, **kwargs):
        (root / 'later').write_text('an agent committed after the person reviewed')
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'later')
        return original_transport(cwd, args, **kwargs)
    monkeypatch.setattr(session_git, 'transport', transport)
    original_run = subprocess.run
    recorded = []
    def run(argv, **kwargs):
        if 'push' in argv:
            from pathlib import Path
            bare = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--git-dir=')))
            recorded.append((argv, (bare / 'HEAD').read_text().strip()))
            return subprocess.CompletedProcess(argv, 0, 'pushed', '')
        return original_run(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    result = session_git.operate(config, str(root), 'push', branch='feature', default_branch='main', repo='example/shop',
                                 lease='a' * 40, expected_head=reviewed)
    assert result['ok'] and result['commit'] == reviewed
    assert recorded[0][1] == reviewed != git(root, 'rev-parse', 'HEAD')
    assert '--force-with-lease=refs/heads/feature:' + 'a' * 40 in recorded[0][0]


async def test_readiness_uses_existing_pull_and_project_git_preference(api, world, fake, monkeypatch):
    fake.open_pull()
    git(world['path'], 'config', 'pull.rebase', 'false')
    original = fake.respond
    def respond(method, path, query, body):
        if '/compare/main...' in path:
            return httpx.Response(200, json={'behind_by': 3})
        return original(method, path, query, body)
    monkeypatch.setattr(fake, 'respond', respond)
    response = await api.get(world['url'] + '/mergeable')
    assert response.status_code == 200, response.text
    readiness = response.json()
    assert readiness.pop('summary') == {'before_count': None, 'after_count': None, 'subjects': [], 'omitted': 0}
    assert readiness == {'behind': 3, 'base': 'main', 'automatic': False, 'mode': 'merge', 'force_push_blocked': True, 'head': git(world['path'], 'rev-parse', 'HEAD'), 'conflicts': []}


@pytest.mark.parametrize('object_present', [False, True])
def test_remote_only_update_branch_commit_is_never_lost(checkout, tmp_path, monkeypatch, object_present):
    config, root = checkout
    before = git(root, 'rev-parse', 'HEAD')
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    git(root, 'remote', 'add', 'origin', str(remote))
    git(root, 'push', 'origin', 'HEAD:refs/heads/feature')
    other = tmp_path / 'other'
    subprocess.run(['git', 'clone', '-b', 'feature', str(remote), str(other)], check=True, capture_output=True)
    git(other, 'config', 'user.name', 'Reviewer')
    git(other, 'config', 'user.email', 'reviewer@example.com')
    (other / 'github-update').write_text('Remote-only GitHub update must survive')
    git(other, 'add', '.')
    git(other, 'commit', '-qm', 'GitHub Update branch')
    head = git(other, 'rev-parse', 'HEAD')
    git(other, 'push', 'origin', 'feature')
    if object_present:
        git(root, 'fetch', 'origin', 'feature')
    def no_transport(*args, **kwargs):
        pytest.fail('Remote-only history must refuse before fetch, rebase or push')
    monkeypatch.setattr(session_git, 'transport', no_transport)
    with pytest.raises(ops.OpError, match='Pull the remote changes first'):
        session_git.update_base(config, str(root), 'feature', 'example/shop', 'main', before,
                                head=head, mode='rebase', force_push_blocked=False, person='casey')
    assert git(root, 'rev-parse', 'HEAD') == before
    assert git(remote, 'rev-parse', 'refs/heads/feature') == head
    assert git(other, 'show', 'feature:github-update') == 'Remote-only GitHub update must survive'


def test_local_ahead_of_remote_is_allowed_but_mode_changes_refuse(checkout, monkeypatch):
    config, root = checkout
    head = git(root, 'rev-parse', 'HEAD')
    (root / 'local').write_text('local commit')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'ahead')
    before = git(root, 'rev-parse', 'HEAD')
    calls = []
    monkeypatch.setattr(session_git, 'transport', lambda *a, **kw: calls.append(a) or subprocess.CompletedProcess([], 0, '', ''))
    git(root, 'config', 'pull.rebase', 'false')
    with pytest.raises(ops.OpError, match='mode changed'):
        session_git.update_base(config, str(root), 'feature', 'example/shop', 'main', head,
                                head=head, mode='rebase', force_push_blocked=False, person='casey')
    assert not calls and git(root, 'rev-parse', 'HEAD') == before
    result = session_git.update_base(config, str(root), 'feature', 'example/shop', 'main', head,
                                     head=head, mode='merge', force_push_blocked=True, person='casey')
    assert result['ok'] and result['commit'] == before


async def test_security_switch_offers_only_merge_before_rewriting(api, env, world, fake, monkeypatch):
    fake.open_pull()
    await human(api, env)
    with write_db(api.app.state.db) as db:
        db.get(Session, world['session']['id']).worktree = str(world['path'])
        db.commit()
    broker = api.app.state.supervisor.broker
    original = broker.call
    calls = []
    async def call(method, **kwargs):
        if method == 'session_update_base':
            calls.append(kwargs)
            return {'ok': True, 'mode': kwargs['mode'], 'before': 'a' * 40, 'commit': 'b' * 40, 'conflicts': [], 'output': 'merged'}
        return await original(method, **kwargs)
    monkeypatch.setattr(broker, 'call', call)
    before = git(world['path'], 'rev-parse', 'HEAD')
    response = await api.post(world['url'] + '/mergeable/base', json={'mode': 'rebase'})
    assert response.status_code == 409 and 'Block force pushes' in response.text
    assert not calls and git(world['path'], 'rev-parse', 'HEAD') == before
    response = await api.post(world['url'] + '/mergeable/base', json={'mode': 'merge'})
    assert response.status_code == 200 and 'push_ticket' not in response.json()
    assert calls[0]['force_push_blocked'] is True


async def test_changes_push_detached_head_refuses_before_github(api, world, monkeypatch):
    with write_db(api.app.state.db) as db:
        db.get(Session, world['session']['id']).worktree = str(world['path'])
        db.commit()
    git(world['path'], 'checkout', '--detach')
    async def no_github(*args, **kwargs):
        pytest.fail('Detached push should refuse before a GitHub request')
    from acc.server import pulls
    monkeypatch.setattr(pulls.BrokerGitHub, 'request', no_github)
    result = await api.post(f"/api/sessions/{world['session']['id']}/git/push", json={})
    assert result.status_code == 409 and 'Finish or abort the rebase/merge first' in result.text


def test_conflicts_have_their_own_untrusted_intro_and_visible_git_evidence():
    from acc.server import pulls
    message, omitted = pulls.conflicts_message(['src/file.py'], {'number': 42}, 'example/shop', 'feature', 'casey')
    assert '<untrusted-conflicted-paths>' in message and 'Git conflicts in the session worktree' in message
    assert 'unresolved review comments' not in message and omitted == 0
    evidence = '\x1b[31mfile\u202e\u200b.py\x1b[0m\nnext\x00'
    assert pulls.display_text(evidence) == 'file[U+202E][U+200B].py\nnext[U+0000]'
    assert pulls.display_text(evidence, multiline=False) == 'file[U+202E][U+200B].py[U+000A]next[U+0000]'


async def test_failed_automatic_delivery_keeps_preview_and_audit(api, env, world, fake, monkeypatch):
    from acc.server.supervisor import LaunchRefused
    fake.open_pull()
    fake.check('test', 'failure')
    with write_db(api.app.state.db) as db:
        db.get(Session, world['session']['id']).provider = 'codex'
        db.commit()
    async def refuse(*args, **kwargs):
        raise LaunchRefused('Sign-in changed; delivery refused')
    monkeypatch.setattr(api.app.state.supervisor, 'send_text', refuse)
    await human(api, env)
    result = await api.post(world['url'] + '/mergeable/feedback')
    assert result.status_code == 200, result.text
    preview = result.json()
    assert not preview['sent'] and preview['id'] and 'delivery refused' in preview['refused']
    assert 'untrusted-ci-output' in preview['text']
    rows = (await api.get('/api/audit', params={'action': 'pr.feedback_preview'})).json()
    assert any(r['actor'] == 'casey' and 'delivery refused' in r['detail'].get('refused', '') for r in rows)
    # Retry uses the same retained preview; the refusal did not consume it.
    retry = await api.post(world['url'] + '/feedback/' + preview['id'] + '/send')
    assert retry.status_code != 404


async def test_api_remote_only_commit_refuses_without_a_push_ticket(api, env, world, fake, monkeypatch):
    pull = fake.open_pull()
    await human(api, env)
    root = world['path']
    before = git(root, 'rev-parse', 'HEAD')
    git(root, 'checkout', '-qb', 'remote-update')
    (root / 'remote-only').write_text('reviewer change')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Reviewer', '-c', 'user.email=reviewer@example.com', 'commit', '-qm', 'remote update')
    remote_head = git(root, 'rev-parse', 'HEAD')
    git(root, 'checkout', 'feature/login')
    pull['head']['sha'] = remote_head
    with write_db(api.app.state.db) as db:
        db.get(Session, world['session']['id']).worktree = str(root)
        db.commit()
    original = guardrails.policy
    def allow(*args, **kwargs):
        policy = original(*args, **kwargs)
        policy['rules']['force_push'] = False
        return policy
    monkeypatch.setattr(guardrails, 'policy', allow)
    result = await api.post(world['url'] + '/mergeable/base', json={'mode': 'rebase'})
    assert result.status_code >= 400 and 'Pull the remote changes first' in result.text
    assert 'push_ticket' not in result.json() and git(root, 'rev-parse', 'HEAD') == before
    assert git(root, 'show', remote_head + ':remote-only') == 'reviewer change'


async def test_conflicted_rebase_returns_visible_names_and_exact_finish_instructions(api, env, world, fake, monkeypatch):
    fake.open_pull()
    await human(api, env)
    with write_db(api.app.state.db) as db:
        db.get(Session, world['session']['id']).worktree = str(world['path'])
        db.commit()
    original = guardrails.policy
    def allow(*args, **kwargs):
        policy = original(*args, **kwargs)
        policy['rules']['force_push'] = False
        return policy
    monkeypatch.setattr(guardrails, 'policy', allow)
    broker = api.app.state.supervisor.broker
    original_call = broker.call
    async def call(method, **kwargs):
        if method == 'session_update_base':
            return {'ok': False, 'mode': 'rebase', 'before': 'a' * 40, 'commit': 'b' * 40,
                    'conflicts': ['src/evil\u202e\u200b.py'], 'output': 'conflict\u202e\u200b'}
        return await original_call(method, **kwargs)
    monkeypatch.setattr(broker, 'call', call)
    result = await api.post(world['url'] + '/mergeable/base', json={'mode': 'rebase'})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body['conflicts'] == ['src/evil[U+202E][U+200B].py']
    assert body['output'] == 'conflict[U+202E][U+200B]' and 'push_ticket' not in body
    assert 'git rebase --continue' in body['finish_instructions']
    assert '--force-with-lease=refs/heads/feature/login:' + 'a' * 40 in body['finish_instructions']
    assert 'Block force pushes OFF' in body['finish_instructions']
    assert "this session's own terminal in Rynhult" in body['finish_instructions']
    assert 'not in your own shell' in body['finish_instructions']
    assert 'git -c core.hooksPath=/dev/null push' in body['finish_instructions']
    assert 'Push completed rebase' in body['finish_instructions']
    assert 'git rebase --abort' in body['finish_instructions']


@pytest.mark.parametrize('case', ['ok', 'security', 'remote-only', 'base-missing', 'moved-head', 'no-consent'])
async def test_person_push_completed_rebase_rechecks_current_history(api, env, world, fake, monkeypatch, case):
    pull = fake.open_pull()
    await human(api, env)
    root = world['path']
    initial = git(root, 'rev-parse', 'HEAD')
    pull['head']['sha'] = initial
    fake.branches['main'] = initial
    with write_db(api.app.state.db) as db:
        db.get(Session, world['session']['id']).worktree = str(root)
        db.commit()
    if case in ('remote-only', 'base-missing'):
        git(root, 'checkout', '-qb', 'remote-new')
        (root / 'remote-new').write_text('never lose this change')
        git(root, 'add', '.')
        git(root, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-qm', 'remote-only')
        remote = git(root, 'rev-parse', 'HEAD')
        git(root, 'checkout', 'feature/login')
        if case == 'remote-only':
            pull['head']['sha'] = remote
        else:
            fake.branches['main'] = remote
    (root / 'completed').write_text('reviewed completion')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-qm', 'completed')
    local = git(root, 'rev-parse', 'HEAD')
    original_policy = guardrails.policy
    def policy(*args, **kwargs):
        result = original_policy(*args, **kwargs)
        result['rules']['force_push'] = case == 'security'
        return result
    monkeypatch.setattr(guardrails, 'policy', policy)
    broker = api.app.state.supervisor.broker
    original_call = broker.call
    pushes = []
    async def call(method, **kwargs):
        if method == 'session_git' and kwargs.get('lease'):
            pushes.append(kwargs)
            return {'ok': True, 'output': 'pushed'}
        return await original_call(method, **kwargs)
    monkeypatch.setattr(broker, 'call', call)
    result = await api.post(world['url'] + '/mergeable/completed-rebase',
                            json={'sha': initial if case == 'moved-head' else local, 'confirm': case != 'no-consent'})
    if case == 'ok':
        assert result.status_code == 200 and result.json()['ok'], result.text
        assert len(pushes) == 1 and pushes[0]['expected_head'] == local and pushes[0]['lease'] == initial
        assert pushes[0]['branch'] == 'feature/login' and pushes[0]['person'] == 'casey'
        rows = (await api.get('/api/audit', params={'action': 'pr.completed_rebase'})).json()
        assert any(r['actor'] == 'casey' and r['detail'].get('outcome') == 'prepared' for r in rows)
    else:
        assert result.status_code >= 400 and not pushes, result.text
        expected = {'security': 'Security switch', 'remote-only': 'branch changed on GitHub since the rebase started',
                    'base-missing': 'current base', 'moved-head': 'head changed', 'no-consent': 'Confirm pushing'}[case]
        assert expected in result.text
        assert git(root, 'rev-parse', 'HEAD') == local


@pytest.mark.parametrize('state', ['rebase-merge', 'MERGE_HEAD', 'index.lock'])
def test_completed_rebase_refuses_operations_in_progress(checkout, state):
    config, root = checkout
    sha = git(root, 'rev-parse', 'HEAD')
    target = root / '.git' / state
    if state == 'rebase-merge':
        target.mkdir()
    else:
        target.write_text(sha)
    with pytest.raises(ops.OpError, match='Finish or abort'):
        session_git.completed_rebase(config, str(root), 'feature', sha, sha, sha)


@pytest.mark.parametrize('case', ['ok', 'remote-moved', 'expired', 'other-person', 'markers', 'markdown', 'crlf'])
async def test_real_conflicted_rebase_completion_uses_server_record(api, env, world, fake, monkeypatch, case):
    from acc.server.api import pulls as pull_routes
    from acc.server.api.context import Principal

    pull = fake.open_pull()
    await human(api, env)
    root = world['path']
    git(root, 'config', 'user.name', 'Agent')
    git(root, 'config', 'user.email', 'agent@example.com')
    (root / 'app.py').write_text('topic version\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'topic conflicts with base')
    lease = git(root, 'rev-parse', 'HEAD')
    remote = env['workspaces'] / 'completion-remote.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    git(root, 'push', str(remote), 'HEAD:refs/heads/feature/login')
    git(root, 'remote', 'add', 'origin', f'https://github.com/{fake.repo}.git')
    git(root, 'checkout', 'main')
    (root / 'app.py').write_text('new base version\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'base conflicts with topic')
    base = git(root, 'rev-parse', 'HEAD')
    git(root, 'checkout', 'feature/login')
    pull['head']['sha'] = lease
    fake.branches['main'] = base
    with write_db(api.app.state.db) as db:
        session = db.get(Session, world['session']['id'])
        session.worktree = str(root)
        session.worktree_branch = 'feature/login'
        db.commit()
    original_policy = guardrails.policy
    def policy(*args, **kwargs):
        value = original_policy(*args, **kwargs)
        value['rules']['force_push'] = False
        return value
    monkeypatch.setattr(guardrails, 'policy', policy)
    original_transport = session_git.transport
    def transport(cwd, args, **kwargs):
        if args[0] == 'fetch':
            # The real base commit already exists locally; only network discovery is faked.
            return subprocess.CompletedProcess(args, 0, '', '')
        return original_transport(cwd, args, **kwargs)
    monkeypatch.setattr(session_git, 'transport', transport)
    original_run = subprocess.run
    pushes = []
    def run(argv, **kwargs):
        if 'push' in argv and any(arg.startswith('--git-dir=') for arg in argv):
            # Exercise the real isolated bare transport and lease against our disposable bare remote.
            pushes.append(argv)
            argv = [str(remote) if arg == f'https://github.com/{fake.repo}.git' else arg for arg in argv]
            kwargs['env'] = {**kwargs['env'], 'GIT_ALLOW_PROTOCOL': 'file'}
        return original_run(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    if case == 'expired':
        monkeypatch.setattr(pull_routes, 'COMPLETION_SECONDS', -1)
    started = await api.post(world['url'] + '/mergeable/base', json={'mode': 'rebase'})
    assert started.status_code == 200 and not started.json()['ok'], started.text
    assert started.json()['conflicts'] == ['app.py']
    assert 'push_ticket' not in started.json()
    resolved = ('<<<<<<< ours\nleftover\n=======\nother\n>>>>>>> theirs\n' if case == 'markers'
                else 'resolved base and topic versions\n')
    (root / 'app.py').write_text(resolved)
    if case == 'markdown':
        (root / 'README.md').write_bytes(b'line one  \nline two\n')
        git(root, 'add', '--', 'README.md')
    if case == 'crlf':
        git(root, 'config', 'core.autocrlf', 'false')
        (root / 'win.txt').write_bytes(b'line one\r\nline two\r\n')
        git(root, 'add', '--', 'win.txt')
    git(root, 'add', '--', 'app.py')
    git(root, '-c', 'core.editor=true', 'rebase', '--continue')
    final = git(root, 'rev-parse', 'HEAD')
    assert final != lease
    assert subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', lease, final]).returncode != 0
    git(root, 'merge-base', '--is-ancestor', base, final)
    expected_remote = lease
    if case == 'remote-moved':
        git(root, 'checkout', '-qb', 'remote-moved', lease)
        (root / 'remote-only-new').write_text('preserve this remote-only commit')
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'another clone advanced the remote')
        expected_remote = git(root, 'rev-parse', 'HEAD')
        git(root, 'push', str(remote), 'HEAD:refs/heads/feature/login')
        git(root, 'checkout', 'feature/login')
        pull['head']['sha'] = expected_remote
    if case == 'other-person':
        monkeypatch.setitem(api.app.dependency_overrides, api.app.state.ctx.principal,
                            lambda: Principal('another-owner', 'owner', 'agent', 9876))
    if case in ('ok', 'markdown', 'crlf'):
        original_respond = fake.respond
        def respond(method, path, query, body):
            if '/compare/' in path:
                return httpx.Response(200, json={'behind_by': 1})
            return original_respond(method, path, query, body)
        monkeypatch.setattr(fake, 'respond', respond)
        readiness = await api.get(world['url'] + '/mergeable')
        assert readiness.status_code == 200, readiness.text
        assert readiness.json()['head'] == final
        assert readiness.json()['summary'] == {'before_count': 1, 'after_count': 1,
                                               'subjects': ['topic conflicts with base'], 'omitted': 0}
    result = await api.post(world['url'] + '/mergeable/completed-rebase', json={'sha': final, 'confirm': True})
    if case in ('ok', 'markdown', 'crlf'):
        assert result.status_code == 200 and result.json()['ok'], result.text
        assert len(pushes) == 1 and '--force-with-lease=refs/heads/feature/login:' + lease in pushes[0]
        assert git(remote, 'rev-parse', 'refs/heads/feature/login') == final
        assert git(remote, 'show', 'refs/heads/feature/login:app.py') == resolved.strip()
        if case in ('markdown', 'crlf'):
            filename, expected_bytes = (('README.md', b'line one  \nline two\n') if case == 'markdown'
                                        else ('win.txt', b'line one\r\nline two\r\n'))
            content = subprocess.run(['git', '-C', str(remote), 'show', 'refs/heads/feature/login:' + filename],
                                     check=True, capture_output=True).stdout
            assert content == expected_bytes

        # Successful completion consumes the server record; rewritten ancestry alone gives no new authority.
        retry = await api.post(world['url'] + '/mergeable/completed-rebase', json={'sha': final, 'confirm': True})
        assert retry.status_code >= 400 and 'start again' in retry.text
    else:
        assert result.status_code >= 400 and not pushes, result.text
        expected = {'remote-moved': 'branch changed on GitHub since the rebase started',
                    'expired': 'record expired', 'other-person': 'another person', 'markers': 'leftover conflict markers'}[case]
        assert expected in result.text and 'Pull the remote' not in result.text
        assert git(remote, 'rev-parse', 'refs/heads/feature/login') == expected_remote
        assert git(root, 'rev-parse', 'HEAD') == final


def test_rebase_summary_reports_changed_counts_and_bounds_subjects(checkout):
    config, root = checkout
    base = git(root, 'rev-parse', 'HEAD')
    for index in range(22):
        (root / 'topic').write_text(str(index))
        git(root, 'add', '.')
        git(root, 'commit', '-qm', f'Topic {index}')
    before = git(root, 'rev-parse', 'HEAD')
    git(root, 'checkout', '-qb', 'rebased', base)
    (root / 'resolved').write_text('resolved')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Resolved topic')
    after = git(root, 'rev-parse', 'HEAD')
    summary = session_git.rebase_summary(config, str(root), base, before, after)
    assert summary == {'before_count': 22, 'after_count': 1, 'subjects': ['Resolved topic'], 'omitted': 0}
    bounded = session_git.rebase_summary(config, str(root), base, after, before)
    assert bounded['after_count'] == 22 and len(bounded['subjects']) == 20 and bounded['omitted'] == 2
