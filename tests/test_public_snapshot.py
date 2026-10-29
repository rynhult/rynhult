"""Exercise publication against a real disposable git repository, never the worktree."""
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/public-snapshot.sh'


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / 'source'
    repo.mkdir()
    git(repo, 'init')
    for name in ['README.md', 'docs/PLAN.md', 'docs/OPERATIONS.md',
                 'docs/SESSION_LOG.md', 'docs/handoffs/note.md', 'docs/qualification/result.md',
                 'docs/research/node-test-plan.md', 'host/private', 'docs/reviews/report.md',
                 'docs/RELEASE-RC1-CHECKLIST.md', 'docs/RENAME-STAGE-2.md',
                 'deploy/github/ruleset-protect-main.json',
                 'docs/research/competitors.md', 'docs/research/competitors-2026-10-22.md',
                 'docs/research/competitors-2026-10-27.md',
                 'docs/decisions/0008-owner-answers-2026-10-22.md',
                 'docs/decisions/0020-owner-answers-2026-10-24.md',
                 'docs/decisions/0050-owner-answers-2026-10-24-afternoon.md',
                 'docs/STATUS-BOARD.md', 'docs/PARKED.md',
                 'docs/research/orchestrator-v2-quota-sizing.md', 'spike/nested/probe.py']:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('committed\n')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Example', '-c', 'user.email=alex@example.com', 'commit', '-m', 'fixture')
    deny = tmp_path / 'deny'
    deny.write_text('PRIVATE_MARKER\n')
    return repo, deny, tmp_path / 'public'


def export(source):
    repo, deny, output = source
    return subprocess.run(['bash', str(SCRIPT), str(deny), str(output)], cwd=repo, capture_output=True)


def test_exports_only_public_head_without_history(source):
    repo, _, output = source
    (repo / 'README.md').write_text('dirty PRIVATE_MARKER')
    (repo / 'untracked').write_text('PRIVATE_MARKER')
    result = export(source)
    assert result.returncode == 0, result.stderr
    assert (output / 'README.md').read_text() == 'committed\n'
    assert sorted(str(p.relative_to(output)) for p in output.rglob('*') if p.is_file()) == [
        'README.md', 'deploy/github/ruleset-protect-main.json']
    assert not (output / '.git').exists()


@pytest.mark.parametrize('location', ['README.md', 'PRIVATE_MARKER.txt', 'binary.dat'])
def test_denylist_scans_contents_names_and_binary_and_removes_failed_export(source, location):
    repo, _, output = source
    (repo / location).write_bytes(b'normal contents' if location == 'PRIVATE_MARKER.txt' else b'\x00PRIVATE_MARKER\xff')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Example', '-c', 'user.email=alex@example.com', 'commit', '-m', 'private')
    result = export(source)
    assert result.returncode != 0
    assert not output.exists()
    assert b'PRIVATE_MARKER' not in result.stderr  # no private matching text in logs


def test_refuses_empty_denylist_and_existing_destination(source):
    _, deny, output = source
    deny.write_text(' \n')
    assert export(source).returncode != 0
    deny.write_text('PRIVATE_MARKER\n')
    output.mkdir()
    (output / 'keep').write_text('safe')
    assert export(source).returncode != 0
    assert (output / 'keep').read_text() == 'safe'


def test_preserves_safe_catalog_links(source):
    repo, _, output = source
    (repo / 'catalog').mkdir()
    (repo / 'catalog/skill.md').write_text('safe')
    (repo / 'skill.md').symlink_to('catalog/skill.md')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Example', '-c', 'user.email=alex@example.com', 'commit', '-m', 'link')
    assert export(source).returncode == 0
    assert (output / 'skill.md').is_symlink()
    assert (output / 'skill.md').read_text() == 'safe'


def test_refuses_links_outside_export(source):
    repo, _, output = source
    (repo / 'escape').symlink_to('../private')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Example', '-c', 'user.email=alex@example.com', 'commit', '-m', 'link')
    assert export(source).returncode != 0
    assert not output.exists()


def test_destination_cannot_be_inside_checkout_via_parent_symlink(source):
    repo, deny, output = source
    alias = output.parent / 'alias'
    alias.symlink_to(repo, target_is_directory=True)
    result = subprocess.run(['bash', str(SCRIPT), str(deny), str(alias / 'public')], cwd=repo, capture_output=True)
    assert result.returncode != 0
    assert not (repo / 'public').exists()


@pytest.mark.parametrize('checkout', [True, False])
def test_both_archive_paths_exclude_future_private_content(source, checkout):
    import tarfile

    from acc.bundle import build

    repo, _, output = source
    private = ['host/future/deep/inventory', 'docs/handoffs/future/note.md',
               'docs/reviews/future/report.md', 'docs/qualification/future/result.md',
               'docs/security/future/findings.md', 'DoCs/SeCuRiTy/nested/review.md',
               'docs/RENAME-STAGE-2.md', '.acc/artifacts/future/dry-run.json',
               'docs/runbooks/future/owner-setup.md', 'DoCs/RuNbOoKs/future/setup.md',
               'docs/drills/future/recovery.md',
               'docs/decisions/0050-owner-answers-2026-10-24-afternoon.md',
               'SpIkE/future/probe.py',
               'docs/drafts/future/owner-brief.md', 'docs/decisions/drafts/future/placement.md']
    public = ['docs/MULTIUSER.md', 'docs/wiki/Internal.md', 'docs/wiki/Internal/Architecture.md',
              'docs/wiki/Internal/Development.md', 'docs/wiki/Internal/Operations.md',
              'docs/wiki/Internal/Website.md',
              'docs/decisions/0063-scheduled-downtime-until-reconnect.md']
    for name in private:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('private')
    for name in public:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('portable developer documentation\n')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Example', '-c', 'user.email=alex@example.com', 'commit', '-m', 'future')
    assert export(source).returncode == 0
    assert all(not (output / name).exists() for name in private)
    assert all((output / name).read_text() == 'portable developer documentation\n' for name in public)
    # Inspect an archive of the snapshot too, rather than only its directory listing.
    snapshot = output.parent / 'snapshot.tar'
    with tarfile.open(snapshot, 'w') as archive:
        archive.add(output, arcname='snapshot')
    if not checkout:
        import shutil
        shutil.rmtree(repo / '.git')
    bundle = build(repo, output.parent / 'bundles', version='test')
    for path in (snapshot, bundle):
        with tarfile.open(path) as archive:
            names = {name.split('/', 1)[1] for name in archive.getnames() if '/' in name}
        assert not any(name in names for name in private)
        assert all(name in names for name in public)
        assert 'README.md' in names
        assert 'deploy/github/ruleset-protect-main.json' in names
        assert not any(name == prefix or name.startswith(prefix + '/') for name in names for prefix in (
            'host', 'docs/handoffs', 'docs/reviews', 'docs/qualification', 'docs/PLAN.md',
            'docs/OPERATIONS.md', 'docs/SESSION_LOG.md', 'docs/research/node-test-plan.md',
            'docs/RELEASE-RC1-CHECKLIST.md'))


def test_public_tree_points_at_the_public_repository(source):
    repo, _, output = source
    (repo / 'config.py').write_text('source_url = "https://github.com/rynhult/rynhult"\nREPO = "rynhult/rynhult"\n')
    git(repo, 'add', 'config.py')
    git(repo, '-c', 'user.name=Example', '-c', 'user.email=alex@example.com', 'commit', '-m', 'source url')
    assert export(source).returncode == 0
    text = (output / 'config.py').read_text()
    assert 'https://github.com/rynhult/rynhult' in text and 'REPO = "rynhult/rynhult"' in text
    assert 'joshuafulton' not in text
    # The private checkout keeps updating from the private repository.
    assert 'rynhult/rynhult' in (repo / 'config.py').read_text()


def test_desktop_dependency_locks_do_not_export_private_constraint_provenance():
    root = Path(__file__).resolve().parents[1]
    for lock in (root / 'desktop').glob('requirements-*.lock'):
        text = lock.read_text()
        assert not any(marker in text for marker in ('/tmp/claude-', '/srv/acc/workspaces/.worktrees')), lock.name
