"""Node update compatibility and retention; no live downloads, root or services."""
import hashlib
import io
import json
import os
import tarfile

import pytest

from acc import bundle, update_helper


@pytest.fixture
def node_release(tmp_path, monkeypatch):
    source = tmp_path / 'app'
    source.mkdir()
    (source / 'pyproject.toml').write_text('[project]\nversion="0.14.0"\n')
    new = tmp_path / 'release'
    (new / 'docs').mkdir(parents=True)
    (new / 'pyproject.toml').write_text('[project]\nversion="0.14.0"\n')
    (new / 'docs/MULTIUSER.md').write_text('Public documentation now included.\n')
    archive = bundle.build(new, tmp_path / 'bundles')
    build = bundle.identity(new)
    config = tmp_path / 'node.env'
    config.write_text('ACC_NODE_PRIMARY=https://primary.example.lan\nACC_NODE_ID=demo\nACC_NODE_TRUST=system\n')
    monkeypatch.setattr(update_helper, 'fetch', lambda *a, **kw: archive.read_bytes())
    job = update_helper.NodeTransaction(tmp_path / 'updates', source, tmp_path / 'data', '0.14.0', {
        'node_config': str(config), 'node_state': str(tmp_path / 'node-state'),
        'build': build, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()})
    return job, archive, build


def test_old_publication_rules_accept_new_authenticated_bundle(node_release, monkeypatch):
    job, _, build = node_release
    # Simulate the old installed helper's rules, after the NEW primary built/stamped its bundle.
    monkeypatch.setattr(bundle, 'PUBLIC_EXCLUDED', (*bundle.PUBLIC_EXCLUDED, 'docs/MULTIUSER.md'))
    tree = job.download()
    assert bundle.identity(tree) != build  # actual rule drift, not a mocked digest
    assert (tree / '.acc-build').read_text().strip() == build
    assert (tree / 'docs/MULTIUSER.md').is_file()


def test_authenticated_archive_hash_still_rejects_tampering(node_release):
    job, archive, _ = node_release
    archive.write_bytes(archive.read_bytes() + b'tampered')
    with pytest.raises(ValueError, match='checksum|SHA|hash'):
        job.download()


@pytest.mark.parametrize('stamp', [None, 'bad stamp', 'f' * 64])
def test_missing_or_wrong_build_stamp_cannot_fall_back_to_local_rehash(node_release, stamp):
    job, archive, _ = node_release
    with tarfile.open(archive, 'r:gz') as tar:
        entries = [(info, tar.extractfile(info).read() if info.isfile() else None) for info in tar]
    with tarfile.open(archive, 'w:gz') as tar:
        for info, content in entries:
            if info.name.endswith('/.acc-build'):
                if stamp is None:
                    continue
                content = (stamp + '\n').encode()
                info.size = len(content)
            tar.addfile(info, io.BytesIO(content) if content is not None else None)
    job.config['sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='version or build mismatch'):
        job.download()


@pytest.mark.parametrize(('healthy', 'cleanup_fails'), [(True, False), (False, False), (True, True)])
def test_previous_sources_are_bounded_only_after_health_passes(tmp_path, monkeypatch, healthy, cleanup_fails):
    source = tmp_path / 'app'
    source.mkdir()
    (source / 'pyproject.toml').write_text('[project]\nversion="0.14.0"\n')
    old = []
    for index in range(5):
        path = tmp_path / f'app.previous-{index:032x}'
        path.mkdir()
        (path / 'retained').write_text(str(index))
        os.utime(path, (100 + index, 100 + index))
        old.append(path)
    unrelated = tmp_path / 'app.previous-manual'
    unrelated.mkdir()
    (unrelated / 'keep').write_text('Never follow old-tree links here.')
    (old[0] / 'nested-link').symlink_to(unrelated, target_is_directory=True)
    os.utime(old[0], (100, 100))
    linked = tmp_path / ('app.previous-' + 'f' * 32)
    linked.symlink_to(unrelated, target_is_directory=True)

    if cleanup_fails:
        original = update_helper.shutil.rmtree
        def denied(path):
            if path in old:
                raise PermissionError('private-path-do-not-display')
            return original(path)
        monkeypatch.setattr(update_helper.shutil, 'rmtree', denied)

    class Local(update_helper.NodeTransaction):
        def download(self):
            self.stage.mkdir()
            path = self.stage / 'new'
            path.mkdir()
            (path / 'pyproject.toml').write_text('[project]\nversion="0.14.0"\n')
            return path

        def install(self, source):
            pass

        def command(self, argv, env=None):
            assert argv == ['systemctl', 'stop', 'acc-node.service']  # record only; never execute

        def health(self):
            if not healthy:
                raise ValueError('fixture node did not reconnect')

    job = Local(tmp_path / 'updates', source, tmp_path / 'data', '0.14.0', {})
    state = job.apply()
    assert state['ok'] is healthy
    if cleanup_fails:
        assert all(p.exists() for p in old) and job.previous.is_dir()
        assert 'cleanup failed' in state['detail'] and 'private-path' not in state['detail']
        assert not state.get('rolled_back')
    elif healthy:
        retained = [p for p in [*old, job.previous] if p.exists()]
        assert set(retained) == {old[-1], job.previous}
    else:
        assert state['rolled_back'] and all(p.exists() for p in old)
    assert source.is_dir() and unrelated.is_dir() and linked.is_symlink()
    assert (unrelated / 'keep').read_text() == 'Never follow old-tree links here.'


def test_helper_failure_detail_survives_node_update_state_and_primary_filter(tmp_path, monkeypatch):
    from acc.node.updates import Updates
    from acc.server.nodes import update_result
    monkeypatch.setattr('acc.node.updates.node_info', lambda: {})

    class Failed(update_helper.NodeTransaction):
        def download(self):
            raise ValueError('node bundle version or build mismatch')

    root = tmp_path / 'updates'
    result = Failed(root, tmp_path / 'app', tmp_path / 'data', '0.14.0', {}).apply()
    assert not result['ok']
    message = Updates(root).message()
    clean = update_result(message['result'], message['refused'])
    assert clean['detail'] == 'node bundle version or build mismatch'
    assert json.loads((root / 'state.json').read_text())['detail'] == clean['detail']
