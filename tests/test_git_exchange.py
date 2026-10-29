"""Real Git boundary tests: config execution, unrelated objects and dirty bytes must not cross."""
import subprocess

import pytest
from isolated_settings import isolated_broker
from test_gitbackup import commit, git, make_repo


def test_exchange_transfers_only_selected_history_and_reads_regular_published_blobs(tmp_path):
    from acc import git_exchange as exchange
    source = make_repo(tmp_path / 'source')
    first = git(source, 'rev-parse', 'HEAD').stdout.strip()
    git(source, 'checkout', '--orphan', 'private')
    commit(source, 'secret.txt', 'private-history-canary')
    private = git(source, 'rev-parse', 'HEAD').stdout.strip()
    git(source, 'checkout', 'main')
    (source / 'README.md').write_text('dirty-canary')
    (source / '.git/hooks/pre-upload-pack').write_text('#!/bin/sh\nexit 99\n')
    git(source, 'config', 'uploadpack.packObjectsHook', 'touch ' + str(tmp_path / 'executed'))
    data = exchange.export(source / '.git', first)
    target = exchange.initialize(tmp_path / 'exchange', 'demo@identity')
    exchange.publish(target, data, 'refs/heads/people/stable/session', '0' * 40, first)
    assert exchange.read_file(target, first, 'README.md') == b'hello\n'
    assert subprocess.run(['git', '--git-dir', str(target), 'cat-file', '-e', private], capture_output=True).returncode != 0
    assert not (tmp_path / 'executed').exists()
    assert b'dirty-canary' not in data
    with pytest.raises(ValueError):
        exchange.read_file(target, private, 'secret.txt')


def test_exchange_cas_no_force_no_delete_and_corrupt_pack(tmp_path):
    from acc import git_exchange as exchange
    source = make_repo(tmp_path / 'source')
    first = git(source, 'rev-parse', 'HEAD').stdout.strip()
    second = commit(source)
    target = exchange.initialize(tmp_path / 'exchange', 'demo@identity')
    ref = 'refs/heads/people/stable/session'
    exchange.publish(target, exchange.export(source / '.git', first), ref, '0' * 40, first)
    with pytest.raises(ValueError):
        exchange.publish(target, exchange.export(source / '.git', second), ref, '0' * 40, second)
    exchange.publish(target, exchange.export(source / '.git', second), ref, first, second)
    with pytest.raises(ValueError):
        exchange.publish(target, exchange.export(source / '.git', first), ref, second, first)
    with pytest.raises(ValueError):
        exchange.publish(target, b'corrupt', ref, second, second)
    with pytest.raises(ValueError):
        exchange.publish(target, b'', ref, second, '0' * 40)
    assert exchange.refs(target)[ref] == second


def test_exchange_rejects_links_and_identity_mismatch_and_never_follows_tree_links(tmp_path):
    from acc import git_exchange as exchange
    source = make_repo(tmp_path / 'source')
    (source / 'escape').symlink_to('/etc/passwd')
    sha = commit(source)
    target = exchange.initialize(tmp_path / 'exchange', 'demo@identity')
    exchange.publish(target, exchange.export(source / '.git', sha), 'refs/heads/people/stable/session', '0' * 40, sha)
    with pytest.raises(ValueError):
        exchange.read_file(target, sha, 'escape')
    with pytest.raises(ValueError):
        exchange.initialize(target, 'other@identity')
    (source / '.git/objects/info/alternates').write_text('/tmp/other')
    with pytest.raises(ValueError):
        exchange.export(source / '.git', sha)


def test_private_placements_and_session_worktrees_do_not_share_git_metadata(tmp_path):
    from acc import git_exchange as exchange
    from acc.broker import group_git
    root = tmp_path / 'workspaces'
    root.mkdir()
    config = isolated_broker(tmp_path, allowed_roots=(root,))
    a = root / 'groups' / ('a' * 32) / '1' / ('b' * 32)
    b = root / 'groups' / ('a' * 32) / '1' / ('c' * 32)
    group_git.provision(config, str(a))
    group_git.provision(config, str(b))
    source = make_repo(tmp_path / 'source')
    sha = git(source, 'rev-parse', 'HEAD').stdout.strip()
    import base64
    data = base64.b64encode(exchange.export(source / '.git', sha)).decode()
    made = group_git.receive(config, str(a), data, sha, 'd' * 12)
    assert (a / 'sessions' / ('d' * 12) / 'README.md').read_text() == 'hello\n'
    assert made['commit'] == sha
    assert git(a / 'sessions' / ('d' * 12), 'rev-parse', '--is-bare-repository').stdout.strip() == 'false'
    assert not (b / '.git/objects' / sha[:2] / sha[2:]).exists()
    (a / '.git/config').write_text('[core]\n hooksPath = /tmp/evil\n[filter "evil"]\n smudge = touch /tmp/evil\n')
    group_git.receive(config, str(a), data, sha, 'e' * 12)
    assert (a / 'sessions' / ('e' * 12) / 'README.md').read_text() == 'hello\n'
    with pytest.raises(ValueError):
        group_git.provision(config, str(root / 'arbitrary'))
    (root / 'groups' / ('f' * 32)).symlink_to(a)
    with pytest.raises(ValueError):
        group_git.provision(config, str(root / 'groups' / ('f' * 32) / '1' / ('b' * 32)))


def test_exchange_caps_outputs_and_ref_count(tmp_path):
    from acc import git_exchange as exchange
    source = make_repo(tmp_path / 'source')
    sha = commit(source, 'large.txt', 'x' * ((1 << 20) + 1))
    target = exchange.initialize(tmp_path / 'exchange', 'demo')
    exchange.publish(target, exchange.export(source / '.git', sha), 'refs/heads/people/stable/session', '0' * 40, sha)
    with pytest.raises(ValueError):
        exchange.read_file(target, sha, 'large.txt')
    for i in range(128):
        exchange.run(target, 'update-ref', f'refs/heads/ref{i}', sha)
    with pytest.raises(ValueError):
        exchange.publish(target, exchange.export(source / '.git', sha), 'refs/heads/another', '0' * 40, sha)


def test_incoming_pack_cannot_smuggle_unselected_history_or_replace_refs(tmp_path):
    from acc import git_exchange as exchange
    source = make_repo(tmp_path / 'source')
    first = git(source, 'rev-parse', 'HEAD').stdout.strip()
    git(source, 'checkout', '--orphan', 'private')
    private = commit(source, 'secret.txt', 'secret-history')
    # A hostile sender includes both unrelated histories, rather than using our exporter.
    packed = subprocess.run(['git', '-C', str(source), 'pack-objects', '--stdout', '--revs'],
                            input=(first + '\n' + private + '\n').encode(), capture_output=True, check=True).stdout
    target = exchange.initialize(tmp_path / 'target', 'demo')
    exchange.publish(target, packed, 'refs/heads/people/stable/session', '0' * 40, first)
    assert subprocess.run(['git', '--git-dir', str(target), 'cat-file', '-e', private], capture_output=True).returncode != 0
    with pytest.raises(ValueError):
        exchange.publish(target, packed, 'refs/replace/' + first, '0' * 40, private)


def test_private_placement_refuses_world_readable_ancestor_and_objects_symlink(tmp_path):
    from acc.broker import group_git
    root = tmp_path / 'workspaces'
    root.mkdir()
    config = isolated_broker(tmp_path, allowed_roots=(root,))
    groups = root / 'groups'
    groups.mkdir(mode=0o755)
    selected = groups / ('a' * 32) / '1' / ('b' * 32)
    with pytest.raises(ValueError):
        group_git.provision(config, str(selected))
    groups.chmod(0o700)
    group_git.provision(config, str(selected))
    (selected / 'sessions').symlink_to(tmp_path)
    with pytest.raises(ValueError):
        group_git.send(config, str(selected), 'a' * 12, 'b' * 40)


def test_recovery_snapshots_stop_at_storage_budget_without_deleting_history(tmp_path, monkeypatch):
    from acc import git_exchange as exchange
    from acc.server import group_recovery
    source = make_repo(tmp_path / 'source')
    sha = git(source, 'rev-parse', 'HEAD').stdout.strip()
    repo = exchange.initialize(tmp_path / 'repo', 'demo')
    exchange.publish(repo, exchange.export(source / '.git', sha), 'refs/heads/published', '0' * 40, sha)
    monkeypatch.setattr(group_recovery, 'MAX_SNAPSHOTS', 1, raising=False)
    destination = tmp_path / 'snapshots'
    first = group_recovery.snapshot(repo, destination, {})
    with pytest.raises(ValueError):
        group_recovery.snapshot(repo, destination, {})
    assert exchange.read_file(destination / first / 'repo.git', sha, 'README.md') == b'hello\n'


def test_storage_budget_reserves_pack_index_overhead_before_promotion(tmp_path):
    from acc import git_exchange as exchange
    source = make_repo(tmp_path / 'source')
    sha = git(source, 'rev-parse', 'HEAD').stdout.strip()
    data = exchange.export(source / '.git', sha)
    target = exchange.initialize(tmp_path / 'target', 'demo')
    # Logical sparse bytes exercise the boundary without filling the test disk.
    with (target / 'objects/info/budget').open('wb') as filler:
        filler.truncate((128 << 20) - len(data) - 10)
    with pytest.raises(ValueError):
        exchange.publish(target, data, 'refs/heads/published', '0' * 40, sha)
    assert exchange.refs(target) == {}


def test_published_snapshot_remains_readable_when_its_branch_advances(tmp_path):
    from acc import git_exchange as exchange
    source = make_repo(tmp_path / 'source')
    first = git(source, 'rev-parse', 'HEAD').stdout.strip()
    second = commit(source, 'README.md', 'new published text')
    target = exchange.initialize(tmp_path / 'target', 'demo')
    ref = 'refs/heads/people/stable/session'
    exchange.publish(target, exchange.export(source / '.git', first), ref, '0' * 40, first)
    exchange.publish(target, exchange.export(source / '.git', second), ref, first, second)
    assert exchange.read_file(target, first, 'README.md') == b'hello\n'


def test_private_checkout_budgets_expanded_files_before_creating_a_worktree(tmp_path, monkeypatch):
    import base64

    from acc import git_exchange as exchange
    from acc.broker import group_git
    root = tmp_path / 'workspaces'
    root.mkdir()
    config = isolated_broker(tmp_path, allowed_roots=(root,))
    selected = root / 'groups' / ('a' * 32) / '1' / ('b' * 32)
    group_git.provision(config, str(selected))
    source = make_repo(tmp_path / 'source')
    for name in ('one', 'two', 'three'):
        (source / name).write_text('x' * 600)
    sha = commit(source)
    data = base64.b64encode(exchange.export(source / '.git', sha)).decode()
    monkeypatch.setattr(group_git, 'MAX_CHECKOUT_BYTES', 1024, raising=False)
    with pytest.raises(ValueError):
        group_git.receive(config, str(selected), data, sha, 'a' * 12)
    assert not (selected / 'sessions' / ('a' * 12)).exists()
