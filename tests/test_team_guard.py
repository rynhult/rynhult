import pytest
from test_guardrails import guard, rule


@pytest.mark.parametrize('command', ['gh pr merge 42', 'env gh --repo demo/app pr merge --auto',
                                       'gh pr --repo demo/app merge 42',
                                       'gh pr -Rdemo/app merge --auto',
                                       'git push origin HEAD:main', 'git push origin master'])
def test_team_guard_blocks_integration_commands(command):
    policy = {'rules': {'team': True}, 'workspace': '/srv/ws'}
    assert rule(command, policy=policy) is not None


def test_team_merge_on_main_refused(tmp_path):
    (tmp_path/'.git').mkdir()
    (tmp_path/'.git/HEAD').write_text('ref: refs/heads/main\n')
    guard.BRANCHES.clear()
    assert rule('git merge acc/helper', policy={'rules': {'team': True}}, cwd=str(tmp_path)) == 'team'


def test_team_allows_topic_merge_and_pr_create(tmp_path):
    (tmp_path/'.git').mkdir()
    (tmp_path/'.git/HEAD').write_text('ref: refs/heads/acc/lead\n')
    guard.BRANCHES.clear()
    assert rule('git merge acc/helper', policy={'rules': {'team': True}}, cwd=str(tmp_path)) is None
    assert rule('gh pr create --title Work', policy={'rules': {'team': True}}, cwd=str(tmp_path)) is None


@pytest.mark.parametrize('command', ['git checkout main && git merge acc/helper',
                                       'git switch master; git merge acc/helper',
                                       'git symbolic-ref HEAD refs/heads/main && git merge acc/helper',
                                       'git update-ref refs/heads/main HEAD',
                                       'git checkout -B main acc/helper'])
def test_team_cannot_switch_to_or_rewrite_protected_branch_before_merge(tmp_path, command):
    (tmp_path/'.git').mkdir()
    (tmp_path/'.git/HEAD').write_text('ref: refs/heads/acc/lead\n')
    guard.BRANCHES.clear()
    assert rule(command, policy={'rules': {'team': True}}, cwd=str(tmp_path)) == 'team'


@pytest.mark.parametrize('tool,team,allowed', [
    ('mcp__acc-team__report', True, True),
    ('mcp__acc-team__plan', True, True),
    ('mcp__acc-team__brief', True, True),
    ('mcp__acc-team__plan_update', True, True),
    ('mcp__acc-team-other__report', True, False),
    ('mcp__acc-team__other__report', True, False),
    ('Write', True, False), ('Bash', True, False),
    ('mcp__acc-team__report', False, False),
])
def test_plan_permission_request_is_only_for_team_tools(tmp_path, monkeypatch, capsys, tool, team, allowed):
    import io
    import json
    import sys

    (tmp_path/'guardrails.json').write_text(json.dumps({'rules': {'team': team}}))
    monkeypatch.setenv('ACC_RUN_DIR', str(tmp_path))
    monkeypatch.delenv('ACC_GUARD_FILES', raising=False)
    monkeypatch.setattr(sys, 'argv', ['acc-guard', 'claude', 'PermissionRequest'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({
        'tool_name': tool, 'tool_input': {}, 'cwd': str(tmp_path), 'permission_mode': 'plan'})))
    assert guard.main() == 0
    output = capsys.readouterr().out
    assert bool(output) is allowed
    if allowed:
        assert json.loads(output)['hookSpecificOutput'] == {
            'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}


@pytest.mark.parametrize('broken', ['hash', 'missing', 'explanation'])
def test_plan_team_permission_cannot_override_guard_refusal(tmp_path, monkeypatch, capsys, broken):
    import io
    import json
    import sys

    policy = tmp_path/'guardrails.json'
    if broken != 'missing':
        policy.write_text(json.dumps({'rules': {'team': True}, 'explanation_only': broken == 'explanation'}))
    monkeypatch.setenv('ACC_RUN_DIR', str(tmp_path))
    monkeypatch.delenv('ACC_GUARD_FILES', raising=False)
    if broken == 'hash':
        monkeypatch.setenv('ACC_GUARD_FILES', 'guardrails.json='+'0'*64)
    monkeypatch.setattr(sys, 'argv', ['acc-guard', 'claude', 'PermissionRequest'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({
        'tool_name': 'mcp__acc-team__report', 'tool_input': {}, 'cwd': str(tmp_path)})))
    assert guard.main() == 0
    assert json.loads(capsys.readouterr().out)['hookSpecificOutput']['decision']['behavior'] == 'deny'


@pytest.mark.parametrize('provider,event,mode', [
    ('claude', 'PreToolUse', 'plan'), ('codex', 'PermissionRequest', 'plan'),
    ('claude', 'PermissionRequest', 'acceptEdits'), ('claude', 'PermissionRequest', None),
])
def test_team_permission_handler_does_not_approve_other_boundaries(tmp_path, monkeypatch, capsys, provider, event, mode):
    import io
    import json
    import sys

    (tmp_path/'guardrails.json').write_text(json.dumps({'rules': {'team': True}}))
    monkeypatch.setenv('ACC_RUN_DIR', str(tmp_path))
    monkeypatch.delenv('ACC_GUARD_FILES', raising=False)
    monkeypatch.setattr(sys, 'argv', ['acc-guard', provider, event])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({
        'tool_name': 'mcp__acc-team__report', 'tool_input': {},
        'cwd': str(tmp_path), 'permission_mode': mode})))
    assert guard.main() == 0
    assert not capsys.readouterr().out


@pytest.mark.parametrize('tool', ['mcp__acc-team__merge', 'mcp__acc-team__restack', 'mcp__acc-team__gate'])
def test_plan_mode_still_asks_before_team_tools_that_change_the_workspace(tmp_path, monkeypatch, capsys, tool):
    # Security review 3, S3-1: Plan mode is the person's "ask before changes"; merging into the lead's worktree,
    # restacking a branch or running branch code in a gate keep the normal prompt.
    import io
    import json
    import sys

    (tmp_path/'guardrails.json').write_text(json.dumps({'rules': {'team': True}}))
    monkeypatch.setenv('ACC_RUN_DIR', str(tmp_path))
    monkeypatch.delenv('ACC_GUARD_FILES', raising=False)
    monkeypatch.setattr(sys, 'argv', ['acc-guard', 'claude', 'PermissionRequest'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({
        'tool_name': tool, 'tool_input': {}, 'cwd': str(tmp_path), 'permission_mode': 'plan'})))
    assert guard.main() == 0
    assert not capsys.readouterr().out


@pytest.mark.parametrize('mode', ['plan', 'auto'])
@pytest.mark.parametrize('name,allowed', [
    ('brief', True), ('brief_documents', True),
    ('brief_propose_documents', False), ('brief_share', False), ('future_write_tool', False),
])
def test_document_permissions_use_explicit_read_tools(tmp_path, monkeypatch, capsys, mode, name, allowed):
    import io
    import json
    import sys

    (tmp_path / 'guardrails.json').write_text(json.dumps({'rules': {'team': True}}))
    monkeypatch.setenv('ACC_RUN_DIR', str(tmp_path))
    monkeypatch.delenv('ACC_GUARD_FILES', raising=False)
    monkeypatch.setattr(sys, 'argv', ['acc-guard', 'claude', 'PermissionRequest'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({
        'tool_name': f'mcp__acc-team__{name}', 'tool_input': {},
        'cwd': str(tmp_path), 'permission_mode': mode})))
    assert guard.main() == 0
    output = capsys.readouterr().out
    assert bool(output) is allowed
    if allowed:
        assert json.loads(output)['hookSpecificOutput']['decision']['behavior'] == 'allow'
