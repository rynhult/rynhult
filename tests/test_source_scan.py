"""Optional scanner regression: fixture annotations never suppress real source findings."""
import os
import secrets
import shutil
import string
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Match scripts/security-check.sh: an explicit tools directory wins over PATH.
GITLEAKS = os.environ.get('GITLEAKS') or (str(Path(os.environ['ACC_TOOLS']) / 'gitleaks')
                                       if os.environ.get('ACC_TOOLS') else shutil.which('gitleaks'))


@pytest.mark.skipif(not GITLEAKS, reason='optional gitleaks executable is not installed')
def test_scanner_remains_strict_outside_generated_browser_output(tmp_path):
    import json

    # Older distro packages exit 1 for the unsupported `dir` command, exactly
    # the default findings status, without ever creating a report.
    capability = subprocess.run([GITLEAKS, 'dir', '--help'], capture_output=True, text=True)
    if capability.returncode and 'unknown command "dir"' in capability.stderr:
        pytest.skip('installed gitleaks lacks dir; use a current build via ACC_TOOLS')
    assert capability.returncode == 0, capability.stderr

    # Random invalid credential, generated only in scratch storage; no real token is used.
    token = 'ghp_' + ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
    (tmp_path / 'probe.txt').write_text(token)
    generated = tmp_path / 'web/e2e/.results/run'
    generated.mkdir(parents=True)
    (generated / 'trace.txt').write_text(token)
    (tmp_path / 'fixture.py').write_text('token = ' + repr(token) + ' # gitleaks:allow -- synthetic fixture\n')
    report = tmp_path.parent / (tmp_path.name + '-report.json')
    try:
        result = subprocess.run([GITLEAKS, 'dir', str(tmp_path), '--config', str(ROOT / '.gitleaks.toml'),
                                 '--redact', '--no-banner', '--exit-code', '42', '--report-format', 'json', '--report-path', str(report)],
                                capture_output=True, text=True)
        assert result.returncode == 42, result.stderr
        assert report.is_file(), f'gitleaks did not create its report: {result.stderr}'
        findings = json.loads(report.read_text())
        assert [Path(finding['File']).name for finding in findings] == ['probe.txt']
    finally:
        report.unlink(missing_ok=True)
