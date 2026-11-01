"""Root-only target policy publication and per-request revision fences."""
import dataclasses

import pytest

from acc.broker.server import Broker


async def test_pending_and_stale_folder_revisions_refuse_before_execution(env):
    broker = Broker(dataclasses.replace(env['broker'], folder_policy_required=True))
    result = await broker.dispatch(b'{"id":1,"method":"workspace_changes","params":{"path":"/etc"}}')
    assert not result['ok']
    assert 'folder policy' in result['error'].lower()
    result = await broker.dispatch(b'{"id":2,"method":"folder_policy_apply","params":{}}')
    assert not result['ok']  # never an ordinary service/agent RPC


def test_root_policy_reload_is_monotonic_and_preserves_default_root(env):
    broker = Broker(env['broker'])
    original = broker.config.allowed_roots
    payload = {'revision': 1, 'allowed_folders': True, 'roots': [str(p) for p in original],
               'uid': __import__('os').getuid()}
    assert broker.apply_folder_policy(payload)['revision'] == 1
    assert broker.config.allowed_roots == original
    assert broker.apply_folder_policy(payload)['revision'] == 1  # idempotent root retry
    with pytest.raises(ValueError, match='revision'):
        broker.apply_folder_policy({**payload, 'roots': ['/etc']})


async def test_new_policy_requires_matching_revision_even_if_root_list_is_unchanged(env):
    import json
    import os
    broker = Broker(env['broker'])
    broker.apply_folder_policy({'revision': 1, 'allowed_folders': True,
        'roots': [str(p) for p in env['broker'].allowed_roots], 'uid': os.getuid()})
    request = {'id': 1, 'method': 'workspace_identity', 'params': {'path': str(env['workspaces']), 'folder_revision': 0}}
    refused = await broker.dispatch(json.dumps(request).encode())
    assert not refused['ok']
    request['params']['folder_revision'] = 1
    assert (await broker.dispatch(json.dumps(request).encode()))['ok']


async def test_server_fences_pending_target_state_from_resolver(api, project):
    from acc.server import security_settings as security
    from acc.server.broker_client import BrokerError
    from acc.server.models import SecurityFolderPolicy
    with api.app.state.db() as db:
        account = db.get(__import__('acc.server.models', fromlist=['Project']).Project, project['id']).account
        scope = security.account_scope(db, 'local', account)
        db.add(SecurityFolderPolicy(key=security.scope_key(scope), machine_id='local', account=account,
            roots=[], revision=1, applied_revision=0, state='pending', detail={}))
        db.commit()
    with pytest.raises(BrokerError, match='pending'):
        await api.app.state.supervisor.broker.call('workspace_changes', account=account, path=project['workspace_path'])


async def test_allowed_folders_off_without_target_ack_cannot_widen_broker(api, project):
    from acc.server import security_settings as security
    from acc.server.broker_client import BrokerError
    from acc.server.models import Project
    with api.app.state.db() as db:
        account = db.get(Project, project['id']).account
        scope = security.account_scope(db, 'local', account)
        security.save(db, scope, {'allowed_folders': False}, actor='casey', expected_revision=0)
        db.commit()
    with pytest.raises(BrokerError, match='pending'):
        await api.app.state.supervisor.broker.call('workspace_changes', account=account, path=project['workspace_path'])


def test_effective_policy_never_labels_an_unacknowledged_on_transition_applied(api, project):
    from acc.server import security_settings as security
    from acc.server.models import Project, SecurityFolderPolicy
    with api.app.state.db() as db:
        account = db.get(Project, project['id']).account
        scope = security.account_scope(db, 'local', account)
        db.add(SecurityFolderPolicy(key=security.scope_key(scope), machine_id='local', account=account,
            roots=[], revision=2, applied_revision=1, state='pending', detail={'allowed_folders': True}))
        db.flush()
        assert security.resolve(db, machine='local', account=account)['enforcement']['allowed_folders'] == 'pending'


def test_services_broker_cannot_receive_agent_folder_bypass(env):
    import os
    broker = Broker(dataclasses.replace(env['broker'], role='services'))
    with pytest.raises(ValueError, match='agent'):
        broker.apply_folder_policy({'revision': 1, 'allowed_folders': False,
            'roots': [str(p) for p in env['broker'].allowed_roots], 'uid': os.getuid()})
    assert broker.config.allowed_folders is True


@pytest.mark.parametrize('allowed', [False, True])
def test_reviewed_broker_keeps_checked_directory_through_acquisition_swap(reviewed_paths, monkeypatch, allowed):
    import os
    from contextlib import contextmanager
    from pathlib import Path

    from acc.broker import ops, pinned_path
    config, safe, private = reviewed_paths
    config.allowed_folders = allowed
    (safe / 'marker').write_text('safe')
    (private / 'marker').write_text('protected')
    real_walk = pinned_path.open_directory
    @contextmanager
    def swapped(path, *args, **kwargs):
        with real_walk(path, *args, **kwargs) as opened:
            yield opened
        if Path(path) == safe:
            safe.rename(safe.parent / 'moved')
            safe.symlink_to(private, target_is_directory=True)
    monkeypatch.setattr(pinned_path, 'open_directory', swapped)
    checked = ops._under_roots(config, str(safe))
    assert (checked / 'marker').read_text() == 'safe'
    assert os.stat(checked).st_ino == (safe.parent / 'moved').stat().st_ino


@pytest.fixture
def reviewed_paths(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from acc import folder_policy
    root = tmp_path / 'host'
    safe, private = root / 'repo', root / 'private'
    safe.mkdir(parents=True)
    private.mkdir()
    config = SimpleNamespace(folder_revision=1, allowed_folders=False, allowed_roots=(root,),
        folder_homes=('/home/alex',), folder_protected=('/home/alex/private',), run_dir='/run/acc')
    real_guard = folder_policy.broker_path
    def mapped(config, path):
        relative = __import__('pathlib').Path(path).relative_to(root)
        return real_guard(config, '/home/alex/' + str(relative))
    monkeypatch.setattr(folder_policy, 'broker_path', mapped)
    return config, safe, private


@pytest.mark.parametrize('allowed', [False, True])
def test_git_uses_pinned_checkout_after_path_swap(reviewed_paths, monkeypatch, allowed):
    import subprocess

    from acc.broker import ops
    config, safe, private = reviewed_paths
    config.allowed_folders = allowed
    subprocess.run(['git', 'init', '-q', '-b', 'safe', str(safe)], check=True)
    subprocess.run(['git', '-C', str(safe), '-c', 'user.name=Alex', '-c', 'user.email=alex@example.com',
        'commit', '-q', '--allow-empty', '-m', 'Fixture'], check=True)
    original = ops._git
    swapped = False
    def swap(cwd, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            safe.rename(safe.parent / 'moved')
            safe.symlink_to(private, target_is_directory=True)
        return original(cwd, *args, **kwargs)
    monkeypatch.setattr(ops, '_git', swap)
    result = ops.workspace_changes(config, str(safe), None)
    assert result['git'] is True
    assert result['branch'] == 'safe'


@pytest.mark.parametrize('allowed', [False, True])
def test_trust_refuses_replaced_logical_name(reviewed_paths, monkeypatch, allowed):
    from acc.broker import ops
    from acc.folder_policy import FolderError
    config, safe, private = reviewed_paths
    config.allowed_folders = allowed
    original = ops._under_roots
    def swap(config, path):
        held = original(config, path)
        safe.rename(safe.parent / 'moved')
        safe.symlink_to(private, target_is_directory=True)
        return held
    monkeypatch.setattr(ops, '_under_roots', swap)
    monkeypatch.setattr(ops, 'installed_providers', lambda _: pytest.fail('Trust must fail before provider commands'))
    with pytest.raises((ops.OpError, FolderError)):
        ops.trust_workspace(config, str(safe))


def test_reviewed_worktree_removal_refuses_path_only_git_deletion(reviewed_paths, monkeypatch):
    from acc.broker import ops
    config, safe, _ = reviewed_paths
    monkeypatch.setattr(ops, '_hardened_git', lambda *args, **kwargs: pytest.fail('No path-only deletion'))
    with pytest.raises(ops.OpError, match='administrator worktree removal'):
        ops.remove_worktree(config, str(safe), str(safe))


def test_pinned_descendants_refuse_protected_symlinks(reviewed_paths):
    from acc.broker import ops
    from acc.folder_policy import FolderError
    config, safe, private = reviewed_paths
    (private / 'data').write_text('protected')
    held = ops._under_roots(config, str(safe))
    (safe / 'alias').symlink_to(private, target_is_directory=True)
    with pytest.raises(FolderError):
        (held / 'alias' / 'data').read_text()
    with pytest.raises((FolderError, OSError)):
        ops._open_dir(held, ['alias'])


@pytest.mark.parametrize('allowed', [False, True])
def test_pinned_directory_moved_into_protected_tree_is_refused(reviewed_paths, allowed):
    from acc.broker import ops
    from acc.folder_policy import FolderError
    config, safe, private = reviewed_paths
    config.allowed_folders = allowed
    (safe / 'data').write_text('safe before move')
    held = ops._under_roots(config, str(safe))
    safe.rename(private / 'moved')
    with pytest.raises(FolderError):
        (held / 'data').read_text()


def test_container_git_guard_does_not_drop_pinned_metadata(reviewed_paths):
    from acc.broker import ops
    config, safe, private = reviewed_paths
    worktree = safe / '.worktrees' / 'helper'
    (worktree / '.git').mkdir(parents=True)
    (worktree / '.git/config').write_text('[alias]\n unsafe = !false\n')
    (private / '.git').mkdir()
    (private / '.git/config').write_text('[user]\n name = Alex\n')
    held = ops._under_roots(config, str(worktree))
    worktree.rename(worktree.parent / 'moved')
    worktree.symlink_to(private, target_is_directory=True)
    with pytest.raises(ops.OpError, match='git settings include'):
        ops._git_argv(held)


def test_provider_settings_inspection_keeps_checked_path(reviewed_paths):
    from acc.broker import ops
    from acc.providers import copilot_settings
    config, safe, private = reviewed_paths
    for folder, disabled in ((safe, True), (private, False)):
        target = folder / '.github/copilot/settings.json'
        target.parent.mkdir(parents=True)
        target.write_text('{"disableAllHooks":' + str(disabled).lower() + '}')
    held = ops._under_roots(config, str(safe))
    safe.rename(safe.parent / 'moved')
    safe.symlink_to(private, target_is_directory=True)
    # A guarded reader may refuse an unsupported metadata open, but cannot
    # silently accept the replacement's clean settings for the checked inode.
    assert 'disableAllHooks' in copilot_settings.repository_problem(held)


@pytest.mark.parametrize('swap', [False, True])
@pytest.mark.parametrize('allowed', [False, True])
def test_worktree_destination_uses_held_parent(reviewed_paths, monkeypatch, swap, allowed):
    import subprocess

    from acc.broker import ops
    from acc.folder_policy import FolderError
    config, safe, private = reviewed_paths
    config.allowed_folders = allowed
    base = safe.parent / 'checkouts'
    base.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(safe)], check=True)
    subprocess.run(['git', '-C', str(safe), '-c', 'user.name=Alex', '-c', 'user.email=alex@example.com',
        'commit', '-q', '--allow-empty', '-m', 'Fixture'], check=True)
    original = ops._hardened_git
    def at_use(cwd, *args, **kwargs):
        if swap and args[:2] == ('worktree', 'add'):
            base.rename(base.parent / 'moved-checkouts')
            base.symlink_to(private, target_is_directory=True)
        return original(cwd, *args, **kwargs)
    monkeypatch.setattr(ops, '_hardened_git', at_use)
    if swap:
        # A stale display location must not be returned as a usable checkout.
        with pytest.raises((ops.OpError, FolderError)):
            ops.make_worktree(config, str(safe), str(base), 'helper', 'acc/fixture')
        assert not (private / '.worktrees').exists()
        assert (base.parent / 'moved-checkouts/.worktrees/helper/.git').is_file()
    else:
        result = ops.make_worktree(config, str(safe), str(base), 'helper', 'acc/fixture')
        assert result['path'] == str(base / '.worktrees/helper')
        assert (base / '.worktrees/helper/.git').is_file()


@pytest.mark.parametrize('allowed', [False, True])
def test_worktree_final_leaf_swap_never_populates_protected_destination(reviewed_paths, monkeypatch, allowed):
    import subprocess

    from acc.broker import ops
    from acc.folder_policy import FolderError
    config, safe, private = reviewed_paths
    config.allowed_folders = allowed
    base = safe.parent / 'checkouts'
    base.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(safe)], check=True)
    (safe / 'tracked.txt').write_text('review fixture')
    subprocess.run(['git', '-C', str(safe), 'add', 'tracked.txt'], check=True)
    subprocess.run(['git', '-C', str(safe), '-c', 'user.name=Alex', '-c', 'user.email=alex@example.com',
        'commit', '-q', '-m', 'Fixture'], check=True)
    original = ops._hardened_git
    def swap(cwd, *args, **kwargs):
        if args[:2] == ('worktree', 'add'):
            leaf = base / '.worktrees/helper'
            if leaf.exists():
                leaf.rename(leaf.parent / 'moved-helper')
            leaf.symlink_to(private, target_is_directory=True)
        return original(cwd, *args, **kwargs)
    monkeypatch.setattr(ops, '_hardened_git', swap)
    with pytest.raises((ops.OpError, FolderError)):
        ops.make_worktree(config, str(safe), str(base), 'helper', 'acc/review')
    assert list(private.iterdir()) == [], 'Refusal must precede every write into the protected destination'


@pytest.mark.parametrize('allowed', [False, True])
def test_reviewed_private_creator_refuses_before_path_only_external_creation(reviewed_paths, monkeypatch, allowed):
    import subprocess

    from acc.broker import ops
    config, safe, private = reviewed_paths
    config.allowed_folders = allowed
    base = safe.parent / 'checkouts'
    base.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(safe)], check=True)
    subprocess.run(['git', '-C', str(safe), '-c', 'user.name=Alex', '-c', 'user.email=alex@example.com',
        'commit', '-q', '--allow-empty', '-m', 'Fixture'], check=True)
    monkeypatch.setattr(ops.containers, 'make_checkout', lambda *args, **kwargs: pytest.fail('No path-only private creator'))
    with pytest.raises(ops.OpError, match='private checkout'):
        ops.make_private_checkout(config, str(safe), str(base), 'helper', 'acc/review')
    assert not (base / '.worktrees').exists()
    assert list(private.iterdir()) == []


@pytest.fixture
def reviewed_worktree_source(reviewed_paths):
    import subprocess
    config, safe, private = reviewed_paths
    base = safe.parent / 'checkouts'
    base.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(safe)], check=True)
    (safe / 'tracked.txt').write_text('review fixture')
    subprocess.run(['git', '-C', str(safe), 'add', 'tracked.txt'], check=True)
    subprocess.run(['git', '-C', str(safe), '-c', 'user.name=Alex', '-c', 'user.email=alex@example.com',
        'commit', '-q', '-m', 'Fixture'], check=True)
    return config, safe, private, base


@pytest.mark.parametrize('allowed', [False, True])
def test_worktree_metadata_survives_closed_request_handles(reviewed_worktree_source, allowed):
    import gc
    import subprocess
    from pathlib import Path

    from acc.broker import ops
    config, safe, private, base = reviewed_worktree_source
    config.allowed_folders = allowed
    result = ops.make_worktree(config, str(safe), str(base), 'helper', 'acc/review')
    gc.collect()
    target = Path(result['path'])
    assert (target / 'tracked.txt').read_text() == 'review fixture'
    status = subprocess.run(['git', '-C', str(target), 'status', '--porcelain'], capture_output=True, text=True)
    assert status.returncode == 0, status.stderr
    assert not status.stdout
    listed = subprocess.run(['git', '-C', str(safe), 'worktree', 'list', '--porcelain'], capture_output=True, text=True, check=True)
    assert f'worktree {target}\n' in listed.stdout
    assert '/proc/' not in listed.stdout
    assert '/proc/' not in (target / '.git').read_text()
    assert not list(private.iterdir())


@pytest.mark.parametrize('allowed', [False, True])
def test_worktree_acquisition_refuses_symlink_inserted_after_mkdir(reviewed_worktree_source, monkeypatch, allowed):
    import os

    from acc.broker import ops
    config, safe, private, base = reviewed_worktree_source
    config.allowed_folders = allowed
    mkdir = os.mkdir
    def swap(path, mode=0o777, *, dir_fd=None):
        mkdir(path, mode, dir_fd=dir_fd)
        if path == 'helper' and dir_fd is not None:
            os.rename('helper', 'moved-helper', src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.symlink(str(private), 'helper', dir_fd=dir_fd)
    monkeypatch.setattr(ops.os, 'mkdir', swap)
    monkeypatch.setattr(ops, '_hardened_git', lambda *args, **kwargs: pytest.fail('Refuse before Git'))
    with pytest.raises(ops.OpError, match='created safely'):
        ops.make_worktree(config, str(safe), str(base), 'helper', 'acc/review')
    assert not list(private.iterdir())
    assert (base / '.worktrees/helper').is_symlink()
    assert (base / '.worktrees/moved-helper').is_dir()


@pytest.mark.parametrize('allowed', [False, True])
def test_failed_git_creation_retains_directory_and_can_retry_with_fresh_name(reviewed_worktree_source, allowed):
    import gc
    import subprocess

    from acc.broker import ops
    config, safe, private, base = reviewed_worktree_source
    config.allowed_folders = allowed
    with pytest.raises(ops.OpError, match='directory is retained'):
        ops.make_worktree(config, str(safe), str(base), 'failed', 'acc/failed', base_commit='0' * 40)
    retained = base / '.worktrees/failed'
    assert retained.is_dir() and not list(retained.iterdir())
    result = ops.make_worktree(config, str(safe), str(base), 'retry', 'acc/retry')
    gc.collect()
    status = subprocess.run(['git', '-C', result['path'], 'status', '--porcelain'], capture_output=True, text=True)
    assert status.returncode == 0 and not status.stdout, status.stderr
    assert retained.is_dir()
    assert not list(private.iterdir())


def test_exclusive_worktree_mkdir_never_adopts_a_concurrently_created_directory(reviewed_worktree_source, monkeypatch):
    import os

    from acc.broker import ops
    config, safe, private, base = reviewed_worktree_source
    mkdir = os.mkdir
    def preempt(path, mode=0o777, *, dir_fd=None):
        if path == 'helper' and dir_fd is not None:
            mkdir(path, mode, dir_fd=dir_fd)
            (base / '.worktrees/helper/existing.txt').write_text('someone else')
        return mkdir(path, mode, dir_fd=dir_fd)
    monkeypatch.setattr(ops.os, 'mkdir', preempt)
    monkeypatch.setattr(ops, '_hardened_git', lambda *args, **kwargs: pytest.fail('No destination adoption'))
    with pytest.raises(ops.OpError, match='created safely'):
        ops.make_worktree(config, str(safe), str(base), 'helper', 'acc/review')
    assert (base / '.worktrees/helper/existing.txt').read_text() == 'someone else'
    assert not list(private.iterdir())


async def test_t3_reload_changes_later_broker_calls_without_killing_or_rewriting_native_work(env, reviewed_paths):
    import json
    import os
    from pathlib import Path

    from acc.broker import ops
    broker = Broker(env['broker'])
    original = str(broker.config.allowed_roots[0])
    mapped, extra, private = reviewed_paths
    (extra / 'native-data').write_text('unchanged')
    payload = {'revision': 1, 'allowed_folders': True, 'roots': [original, str(extra)], 'uid': os.getuid(),
               'homes': list(mapped.folder_homes), 'protected': list(mapped.folder_protected)}
    broker.apply_folder_policy(payload)
    assert ops._under_roots(broker.config, str(extra))
    # Removing a root is a serialized config reload, not a process/service restart.
    broker.apply_folder_policy({**payload, 'revision': 2, 'roots': [original]})
    with pytest.raises(ops.OpError):
        ops._under_roots(broker.config, str(extra))
    broker.apply_folder_policy({**payload, 'revision': 3, 'roots': [original], 'allowed_folders': False})
    assert ops._under_roots(broker.config, str(extra))  # owner-reviewed Off widens service policy only
    with pytest.raises(ops.OpError):
        ops._under_roots(broker.config, str(private))  # mandatory protected paths still refuse
    broker.apply_folder_policy({**payload, 'revision': 4, 'roots': [original], 'allowed_folders': True})
    with pytest.raises(ops.OpError):
        ops._under_roots(broker.config, str(extra))
    assert (extra / 'native-data').read_text() == 'unchanged'  # OS access is not revoked
    assert Path(original).exists()
    ping = await broker.dispatch(json.dumps({'id': 1, 'method': 'ping', 'params': {}}).encode())
    assert ping['ok']  # terminal observation/kill likewise stay exempt
