"""Contributor scans discover tools on PATH without an operator installation."""
import os
import subprocess


def test_security_check_uses_path_tools(tmp_path):
    log = tmp_path / 'calls'
    for name in ('gitleaks', 'osv-scanner'):
        tool = tmp_path / name
        tool.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$SCAN_LOG"\n')
        tool.chmod(0o755)
    result = subprocess.run(['bash', 'scripts/security-check.sh'], capture_output=True, text=True,
                            env=(os.environ | {'PATH': f'{tmp_path}:' + os.environ['PATH'], 'SCAN_LOG': str(log),
                                               'ACC_TOOLS': ''}))
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert len(calls) == 3
    assert 'gitleaks git . --redact' in calls[0]
    assert 'gitleaks dir . --redact' in calls[1]
    assert 'osv-scanner scan source --lockfile uv.lock' in calls[2]
