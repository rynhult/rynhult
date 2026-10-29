"""Release artifacts must be reproducible and use the public-source boundary."""
import fnmatch
import os
import tarfile
from pathlib import Path

import yaml
from test_acc_install_root import machine  # noqa: F401

from acc import bundle
from acc.updates import newer, version

ROOT = Path(__file__).resolve().parents[1]


def test_source_release_bytes_do_not_depend_on_checkout_timestamps(tmp_path):
    tree = tmp_path / 'source'
    tree.mkdir()
    (tree / 'pyproject.toml').write_text('[project]\nversion="0.15.0-rc.1"\n')
    (tree / 'deploy').mkdir()
    (tree / 'deploy/acc-install').write_text('#!/bin/sh\nexit 0\n')
    (tree / 'deploy/acc-install').chmod(0o755)
    first = bundle.build(tree, tmp_path / 'one').read_bytes()
    for path in [tree, *tree.rglob('*')]:
        os.utime(path, (1700000000, 1700000000))
    second = bundle.build(tree, tmp_path / 'two').read_bytes()
    assert second == first
    with tarfile.open(tmp_path / 'two/acc-0.15.0-rc.1.tar.gz') as archive:
        assert archive.getmember('acc-0.15.0-rc.1/deploy/acc-install').mode == 0o755


def test_public_release_workflow_accepts_the_planned_zero_major_rc():
    workflow = yaml.safe_load((ROOT / '.github/workflows/releases.yml').read_text())
    patterns = workflow.get('on', workflow.get(True))['push']['tags']
    assert any(fnmatch.fnmatchcase('v0.15.0-rc.1', pattern) for pattern in patterns)


def test_rc_orders_after_previous_release_and_before_its_final():
    assert version('0.15.0-rc.1') > version('0.14.0')
    assert not newer('0.15.0-rc.1', '0.14.0')  # automatic updates deliberately require stable candidates
    assert newer('0.15.0', '0.15.0-rc.1')
    assert not newer('0.14.0', '0.15.0-rc.1')


def test_built_python_distributions_have_no_operator_documents():
    """Opt-in integration check over actual uv-build output, not a copy of Hatch's configuration."""
    import zipfile

    import pytest
    directory = os.environ.get('RELEASE_TEST_DIST')
    if not directory:
        pytest.skip('set RELEASE_TEST_DIST to the dry-run Python artifacts')
    artifacts = sorted(Path(directory).glob('rynhult-*'))
    assert len(artifacts) == 2
    for artifact in artifacts:
        names = zipfile.ZipFile(artifact).namelist() if artifact.suffix == '.whl' else tarfile.open(artifact).getnames()
        assert not any(part in name for name in names for part in (
            '/docs/handoffs/', '/docs/reviews/', '/docs/security/', '/docs/wiki/Internal/', '/host/', '/denylist'))


def test_installer_from_built_linux_artifact_uses_only_the_fake_root(tmp_path, monkeypatch, request):
    import hashlib

    import pytest
    import test_acc_install_root as installer

    artifact = os.environ.get('RELEASE_TEST_LINUX')
    if not artifact:
        pytest.skip('set RELEASE_TEST_LINUX to the dry-run Linux tarball')
    archive = Path(artifact)
    extracted = bundle.unpack(archive, tmp_path / 'extracted', '0.15.0-rc.1',
                              hashlib.sha256(archive.read_bytes()).hexdigest())
    monkeypatch.setattr(installer, 'REPO', extracted)
    run, root, calls, _ = request.getfixturevalue('machine')
    result = run('--main', '--yes', ACC_OWNER='alex', ACC_ZONE='example.com', CF_API_TOKEN='synthetic-only')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Rynhult is installed' in result.stdout
    assert calls.read_text().splitlines()[0] == '--yes'
    assert (root / 'var/log/acc-install.log').is_file()
    assert 'synthetic-only' not in (root / 'var/log/acc-install.log').read_text()
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob('*')) == [
        'var', 'var/log', 'var/log/acc-install.log']
