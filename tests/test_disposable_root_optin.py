"""The explicit VM opt-in is pytest state, never an inherited runtime capability."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(('euid', 'opted_in', 'missing'), [
    (0, True, None),
    (0, False, 'explicit opt-in'),
    (1004, True, 'EUID 0'),
    (1004, False, 'EUID 0'),
])
def test_qualification_requires_both_actual_root_and_explicit_optin(monkeypatch, euid, opted_in, missing):
    import host_isolation
    monkeypatch.setattr(host_isolation.os, 'geteuid', lambda: euid)
    config = SimpleNamespace(getoption=lambda name: opted_in)
    reason = host_isolation.disposable_root_skip_reason(config)
    if missing is None:
        assert reason is None
    else:
        assert missing in reason
        if euid != 0 and not opted_in:
            assert 'explicit opt-in' in reason


@pytest.mark.parametrize(('value', 'flag', 'expected'), [('1', False, True), ('true', False, False), ('', True, True)])
def test_optin_survives_actual_plugin_scrub_and_xdist_without_forwarding_environment(tmp_path, value, flag, expected):
    """Child tests are harmless probes, not the OS-mutating qualification cases."""
    repo = Path(__file__).resolve().parents[1]
    case = tmp_path / 'test_probe.py'
    case.write_text(f'''import os
import pytest
from host_isolation import HostGuard, HostAccessError, _GUARD

@pytest.mark.parametrize('sample', [1, 2])
def test_runtime_config_survives_scrub(request, sample):
    assert request.config.getoption('disposable_root_vm') is {expected!r}
    assert _GUARD.disposable_root_vm is {expected!r}
    assert 'ACC_FOLDER_QUALIFICATION_DISPOSABLE' not in os.environ
    assert 'ACC_NOT_A_TEST_CAPABILITY' not in os.environ
    # The flag is not permission for arbitrary host commands, even in a root VM.
    with pytest.raises(HostAccessError):
        HostGuard().command('/usr/sbin/useradd', ['useradd', 'unreviewed'], {{}})

@pytest.mark.usefixtures('disposable_root_vm')
def test_gated_probe_only():
    assert os.geteuid() == 0
''')
    args = [sys.executable, '-m', 'pytest', '-q', '-rs', '--color=no', '-n', '2', '-p', 'host_isolation', str(case)]
    if flag:
        args.append('--disposable-root-vm')
    result = subprocess.run(args, cwd=tmp_path, env={**os.environ,
        'PYTHONPATH': f'{repo / "tests"}:{repo / "src"}',
        'ACC_FOLDER_QUALIFICATION_DISPOSABLE': value, 'ACC_NOT_A_TEST_CAPABILITY': 'never-forward'},
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    if expected and os.geteuid() == 0:
        assert '3 passed' in result.stdout
    else:
        assert '2 passed, 1 skipped' in result.stdout
        assert ('explicit opt-in' if not expected else 'EUID 0') in result.stdout
