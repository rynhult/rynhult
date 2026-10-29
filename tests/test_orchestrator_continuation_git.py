"""Continuation commit pins through real, disposable Git repositories only."""
from pathlib import Path

from test_changes import git, repo
from test_team_git_hardening import config, output

from acc.broker import ops


def test_private_continuation_can_seed_an_unmerged_retained_attempt_head(tmp_path):
    source = tmp_path / 'source'
    repo(source)
    initial = output(source, 'branch', '--show-current')
    git(source, 'checkout', '-qb', 'acc/old-attempt')
    (source / 'retained.txt').write_text('Committed on the old attempt only')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'Old attempt')
    head = output(source, 'rev-parse', 'HEAD')
    git(source, 'checkout', '-q', initial)
    made = ops.make_private_checkout(config(tmp_path), str(source), str(tmp_path), 'continued', 'acc/continued', base_commit=head)
    assert made['base_commit'] == head
    assert (Path(made['path']) / 'retained.txt').read_text() == 'Committed on the old attempt only'
    assert output(source, 'for-each-ref', '--format=%(refname)', 'refs/acc/') == ''


def test_archived_attempt_branch_can_be_bundled_at_its_pinned_head(tmp_path):
    source = tmp_path / 'source'
    repo(source)
    original = output(source, 'branch', '--show-current')
    git(source, 'checkout', '-qb', 'acc/archived-attempt')
    (source / 'retained.txt').write_text('Retained remote attempt')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'Attempt head')
    head = output(source, 'rev-parse', 'HEAD')
    git(source, 'checkout', '-q', original)
    cfg = config(tmp_path)
    bundle = ops.team_bundle(cfg, str(source), 'acc/archived-attempt', base_commit=head)
    made = ops.team_checkout(cfg, 'continued-remote', 'acc/continued-remote', **bundle)
    assert output(Path(made['path']), 'rev-parse', 'HEAD') == head
    assert (Path(made['path']) / 'retained.txt').read_text() == 'Retained remote attempt'
