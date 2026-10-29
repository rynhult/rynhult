"""Security review of rename phase 1 (rc1): reader loading, alias shadowing and installer isolation."""
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HELPERS = ['deploy/accounts/acc-accounts', 'deploy/backup/acc-backup', 'deploy/hoststate/acc-hoststate',
           'deploy/publish/acc-publish', 'deploy/packages/acc-packages', 'deploy/containers/acc-containers',
           'deploy/updates/acc-update', 'deploy/containers/acc-session', 'deploy/lib/node-accounts.py',
           'bin/acc-guard', 'bin/acc-request', 'bin/acc-mcp-team']


def installed(tmp_path, helper, reader_mode=0o644):
    folder = tmp_path / 'sbin'
    folder.mkdir()
    shutil.copy(REPO / helper, folder / 'helper')
    if reader_mode is not None:
        shutil.copy(REPO / 'src/acc/env.py', folder / 'acc_env.py')
        (folder / 'acc_env.py').chmod(reader_mode)
    return folder / 'helper'


def load_reader(script, relative='../../src/acc/env.py'):
    namespace = {'__file__': str(script)}
    # Exercise the bootstrap only: a refusal regression must never start a root job.
    source = script.read_text().split('_compat = _compat_reader(', 1)[0]
    exec(compile(source, str(script), 'exec'), namespace)
    return namespace['_compat_reader'](relative)


@pytest.fixture
def root_owned_files(monkeypatch):
    real = Path.stat

    def stat(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        return os.stat_result((*tuple(info)[:4], 0, 0, *tuple(info)[6:]))

    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(Path, 'stat', stat)


@pytest.mark.parametrize('helper', HELPERS)
@pytest.mark.parametrize('reader_mode', [0o664, 0o666])
def test_helpers_refuse_a_reader_others_can_write(tmp_path, helper, reader_mode, root_owned_files):
    script = installed(tmp_path, helper, reader_mode=reader_mode)
    with pytest.raises(SystemExit):
        load_reader(script)


@pytest.mark.parametrize('helper', HELPERS)
def test_helpers_refuse_a_replaceable_reader(tmp_path, helper, root_owned_files):
    script = installed(tmp_path, helper)
    script.parent.chmod(0o775)
    with pytest.raises(SystemExit):
        load_reader(script)


@pytest.mark.parametrize('helper', HELPERS)
def test_helpers_cannot_resolve_past_an_untrusted_fallback_path(tmp_path, helper, root_owned_files):
    script = installed(tmp_path, helper, reader_mode=None)
    trusted, untrusted = tmp_path / 'trusted', tmp_path / 'untrusted'
    trusted.mkdir()
    untrusted.mkdir(mode=0o775)
    untrusted.chmod(0o775)
    marker = tmp_path / 'executed'
    (trusted / 'env.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    (trusted / 'env.py').chmod(0o644)
    (untrusted / 'source').symlink_to(trusted, target_is_directory=True)
    with pytest.raises(SystemExit) as refused:
        load_reader(script, '../untrusted/source/env.py')
    assert refused.value.code == 2
    assert not marker.exists()


@pytest.mark.parametrize('helper', HELPERS)
def test_helpers_refuse_a_reader_owned_by_someone_else(tmp_path, helper, monkeypatch):
    script = installed(tmp_path, helper)
    real = Path.stat

    def stat(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        if Path(path).name == 'acc_env.py':
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid + 1,
                                   *tuple(info)[5:]))
        return info
    monkeypatch.setattr(Path, 'stat', stat)
    with pytest.raises(SystemExit):
        load_reader(script)


def test_guard_fails_closed_without_its_reader(tmp_path):
    script = installed(tmp_path, 'bin/acc-guard', reader_mode=None)
    result = subprocess.run([sys.executable, '-I', str(script), 'claude', 'PreToolUse'], input='{}', text=True,
                            capture_output=True, env={'PATH': '/usr/bin:/bin', 'ACC_RUN_DIR': str(tmp_path)})
    assert result.returncode == 2, result.stderr  # 2 blocks the tool call; 1 would let it run unchecked


@pytest.mark.parametrize('mode', [None, 0o664, 0o666])
def test_wheel_reader_refuses_missing_or_unsafe_canonical_reader(tmp_path, mode, root_owned_files):
    script = tmp_path / 'acc/_scripts/acc_env.py'
    script.parent.mkdir(parents=True)
    shutil.copy2(REPO / 'bin/acc_env.py', script)
    marker = tmp_path / 'executed'
    if mode is not None:
        reader = script.parent.parent / 'env.py'
        reader.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
        reader.chmod(mode)
    with pytest.raises(SystemExit) as refused:
        runpy.run_path(str(script))
    assert refused.value.code == 2
    assert not marker.exists()


def test_unit_alias_refuses_to_shadow_a_vendor_unit(tmp_path):
    install = runpy.run_path(str(REPO / 'deploy/lib/unit-aliases.py'))['install_aliases']
    etc, vendor = tmp_path / 'etc', tmp_path / 'lib'
    etc.mkdir()
    vendor.mkdir()
    (etc / 'acc-backup.service').write_text('[Service]\n')
    (vendor / 'rynhult-backup.service').write_text('[Service]\nExecStart=/usr/bin/unrelated\n')
    with pytest.raises(ValueError, match='conflict'):
        install(etc, search_dirs=(vendor,))
    assert not os.path.lexists(etc / 'rynhult-backup.service')
    (vendor / 'rynhult-backup.service').unlink()
    (vendor / 'rynhult-backup.service.d').mkdir()
    with pytest.raises(ValueError, match='conflict'):
        install(etc, search_dirs=(vendor,))


def test_root_installers_run_alias_helper_isolated():
    for script in ('deploy/install.sh', 'deploy/node/install.sh', 'deploy/dns/install.sh'):
        for line in (REPO / script).read_text().splitlines():
            if 'unit-aliases.py' in line:
                assert 'python3 -I ' in line, f'{script}: {line.strip()}'
