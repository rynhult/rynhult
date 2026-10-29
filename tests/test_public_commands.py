"""Public entry points retain the legacy command contract without starting services."""
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
SCRIPTS = PROJECT['scripts']


def command(name, *args, input=None, env=None):
    # Resolve the distribution entry point against this checkout, never the host's editable install.
    target = SCRIPTS[name]
    module, function = target.split(':')
    code = f'from {module} import {function}; raise SystemExit({function}())'
    return subprocess.run([sys.executable, '-c', code, *args], input=input, text=True,
                          capture_output=True, env={**os.environ, 'ACC_RUN_DIR': '', 'PYTHONPATH': str(ROOT / 'src'),
                                                    **(env or {})}, timeout=20)


@pytest.mark.parametrize('name', ['rynhult', 'acc', 'rynhultd', 'accd'])
def test_public_and_legacy_commands_offer_help_and_version(name):
    help_result = command(name, '--help')
    assert help_result.returncode == 0, help_result.stderr
    assert 'Rynhult' in help_result.stdout
    version_result = command(name, '--version')
    assert version_result.returncode == 0, version_result.stderr
    assert version_result.stdout == f'Rynhult {PROJECT["version"]}\n'


@pytest.mark.parametrize('name', ['rynhult-node', 'acc-node'])
def test_node_alias_rejects_invalid_enrollment_before_writing(name, tmp_path):
    result = command(name, 'enroll', '--state-dir', str(tmp_path), '--primary', 'http://example.lan',
                     '--code', 'example', '--name', 'demo')
    assert result.returncode != 0
    assert not list(tmp_path.iterdir())
    assert command(name, '--help').returncode == 0


@pytest.mark.parametrize('name', ['rynhult-guard', 'acc-guard'])
def test_guard_alias_allows_tool_outside_session(name):
    result = command(name, 'claude', 'PreToolUse', input=json.dumps({
        'tool_name': 'Bash', 'tool_input': {'command': 'echo demo'}}))
    assert result.returncode == 0, result.stderr
    assert 'deny' not in result.stdout


@pytest.mark.parametrize('name', ['rynhult-guard', 'acc-guard'])
def test_guard_alias_fails_closed_when_session_policy_is_missing(name, tmp_path):
    result = command(name, 'claude', 'PreToolUse', input=json.dumps({
        'tool_name': 'Bash', 'tool_input': {'command': 'echo demo'}}), env={'ACC_RUN_DIR': str(tmp_path)})
    assert result.returncode == 2
    assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert 'guardrails.json' in result.stderr


@pytest.mark.parametrize('name', ['rynhult-guard', 'acc-guard'])
def test_guard_alias_uses_isolated_interpreter(name, tmp_path):
    (tmp_path / 'json.py').write_text('raise RuntimeError("untrusted import")\n')
    result = command(name, 'claude', 'PreToolUse', input='{}',
                     env={'PYTHONPATH': f'{ROOT / "src"}:{tmp_path}'})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('name', ['rynhult', 'acc'])
def test_cli_dispatches_node_help_to_node_parser(name):
    result = command(name, 'node', '--help')
    assert result.returncode == 0, result.stderr
    assert 'enroll' in result.stdout
    assert 'status' in result.stdout
