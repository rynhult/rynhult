"""Every provider wire format retains parent attribution, even broken policies."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_guardrails import guard

GUARD = Path(__file__).resolve().parents[1] / 'bin' / 'acc-guard'


@pytest.mark.parametrize('provider,event', [(p, 'PreToolUse') for p in ('claude', 'codex', 'copilot', 'cursor', 'opencode', 'antigravity')] + [('claude', 'PermissionRequest')])
@pytest.mark.parametrize('broken', [False, True])
def test_parent_attribution_survives_hook_refusal_and_policy_loss(tmp_path, provider, event, broken):
    policy = json.dumps({'rules': {'force_push': True}, 'workspace': str(tmp_path)})
    if not broken:
        (tmp_path / 'guardrails.json').write_text(policy)
    result = subprocess.run([sys.executable, str(GUARD), provider, event],
        input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'git push --force origin feature'}}),
        text=True, capture_output=True, env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path),
            'ACC_RUN_DIR': str(tmp_path), 'ACC_PARENT_SESSION_ID': 'rootcap01',
            'ACC_GUARD_FILES': 'guardrails.json=' + hashlib.sha256(policy.encode()).hexdigest()})
    assert result.returncode in (0, 2)
    assert 'limited by rootcap01:' in result.stdout + result.stderr
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    assert events[-1]['guard']['reason'].startswith('limited by rootcap01:')


@pytest.mark.parametrize('failure', ['checker_error', 'too_slow'])
def test_internal_checker_failures_keep_the_parent(monkeypatch, failure):
    def fail(*_args):
        if failure == 'too_slow':
            raise guard.TooSlow()
        raise ValueError('synthetic checker failure')
    monkeypatch.setattr(guard, 'check', fail)
    monkeypatch.setenv('ACC_PARENT_SESSION_ID', 'rootcap01')
    verdict, _ = guard.decide({}, None, 'Bash', {}, '/tmp', '/tmp')
    assert verdict[1].startswith('limited by rootcap01:')


def test_hook_enforces_its_hashed_deadline_and_forces_typed_install_path(tmp_path, monkeypatch):
    from acc.session_capabilities import normalize
    policy = {'rules': {}, 'workspace': str(tmp_path), 'tool_approval': False}
    (tmp_path / 'guardrails.json').write_text(json.dumps(policy))
    caps = {'effective': normalize({'installs': [], 'expires_at': 1}), 'parent': 'rootcap01'}
    (tmp_path / 'session-capabilities.json').write_text(json.dumps(caps))
    hashes = ' '.join(f'{name}={hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}'
                     for name in ('guardrails.json', 'session-capabilities.json'))
    monkeypatch.setenv('ACC_GUARD_FILES', hashes)
    loaded, broken = guard.load_policy(str(tmp_path))
    assert broken is None and loaded['tool_approval']
    verdict, _ = guard.decide(loaded, broken, 'Bash', {'command': 'echo hi'}, str(tmp_path), str(tmp_path))
    assert verdict[0] == 'time_limit' and verdict[1].startswith('limited by rootcap01:')
