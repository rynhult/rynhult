"""Security review 9 follow-ups: SR9-12, the installer dry run's backup-capture probe is read-only (it reads the
kernel's lock table and never takes the capture or worker lease); SR9-9, Android Gradle verification metadata."""
import importlib.util
from pathlib import Path

import pytest

from acc.maintenance import LeaseBusy, lease

ROOT = Path(__file__).resolve().parents[1]
LOCKS = ('.capture-update.lock',)


@pytest.fixture
def aliases():
    spec = importlib.util.spec_from_file_location('public_aliases', ROOT / 'deploy/lib/public-aliases.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('lock', LOCKS)
def test_the_dry_run_probe_never_takes_the_capture_lease(aliases, tmp_path, monkeypatch, lock):
    data = tmp_path / 'srv/acc/data'
    (data / 'backup-service').mkdir(parents=True)
    (data / lock).touch()
    def refused(*args, **kwargs):
        raise AssertionError('the dry-run probe took a lock')
    monkeypatch.setattr(aliases.fcntl, 'flock', refused)
    monkeypatch.setattr(aliases.fcntl, 'lockf', refused)
    assert aliases.capture_running(tmp_path) is False


@pytest.mark.parametrize('lock', LOCKS)
def test_the_probe_still_sees_a_held_lease_and_its_release(aliases, tmp_path, lock):
    data = tmp_path / 'srv/acc/data'
    (data / 'backup-service').mkdir(parents=True)
    with lease(data / lock):
        assert aliases.capture_running(tmp_path) is True
        with pytest.raises(LeaseBusy), lease(data / lock, timeout=0):  # the probe didn't take or release it
            pass
    assert aliases.capture_running(tmp_path) is False


def test_the_probe_does_not_guess_when_the_lock_table_is_unreadable(aliases, tmp_path, monkeypatch, capsys):
    data = tmp_path / 'srv/acc/data'
    data.mkdir(parents=True)
    with lease(data / '.capture-update.lock'):
        monkeypatch.setattr(aliases, 'lease_held', lambda info: None)
        assert aliases.capture_running(tmp_path) is False
    assert "can't read /proc/locks" in capsys.readouterr().err


# ---- SR9-9: Gradle dependency verification for mobile/android ---------------------------------------------------

METADATA = ROOT / 'mobile/android/gradle/verification-metadata.xml'


def test_the_android_build_check_carries_the_verification_metadata_into_the_build():
    """scripts/android_build_check.sh copies mobile/android into its project without dropping gradle/, so a committed
    verification-metadata.xml is enforced in the disposable build."""
    script = (ROOT / 'scripts/android_build_check.sh').read_text()
    copy = script[script.index("shutil.copytree(repo / 'mobile/android'"):]
    ignored = copy[copy.index('ignore_patterns('):copy.index(')', copy.index('ignore_patterns('))]
    assert "'gradle'" not in ignored and "'build', '.gradle'" in ignored


@pytest.mark.skipif(not METADATA.exists(), reason='generated on the disposable Android VM (handoff 2026-10-27 sr9-followups)')
def test_committed_verification_metadata_verifies_every_artifact_by_sha256():
    import xml.etree.ElementTree as ElementTree
    ns = {'g': 'https://schema.gradle.org/dependency-verification'}
    root = ElementTree.parse(METADATA).getroot()
    assert root.findtext('g:configuration/g:verify-metadata', namespaces=ns) == 'true'
    assert root.find('g:configuration/g:trusted-artifacts', ns) is None  # nothing exempted
    artifacts = root.findall('g:components/g:component/g:artifact', ns)
    assert artifacts and all(a.find('g:sha256', ns) is not None for a in artifacts)
    assert not root.findall('.//g:sha1', ns) and not root.findall('.//g:md5', ns)
