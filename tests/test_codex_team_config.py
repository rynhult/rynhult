"""Codex team scope and deferred-tool discovery, without real provider invocations."""
import json
import subprocess

import pytest
from test_agent_contract import position
from test_changes import repo

from acc.broker import ops
from acc.server.agent_contract import render
from acc.server.compiler import LaunchInputs
from acc.server.teams import team_files


@pytest.mark.parametrize('resume', [False, True])
def test_codex_team_gets_only_its_own_git_metadata(env, resume):
    root = env['workspaces'] / 'demo'
    repo(root)
    work = env['workspaces'] / 'helper'
    subprocess.run(['git', '-C', str(root), 'worktree', 'add', '-qb', 'acc/helper/work', str(work)], check=True)
    args = (['resume', '11111111-2222-3333-4444-555555555555'] if resume else []) + ['--sandbox', 'workspace-write']
    additions = ops._codex_team_git_args(env['broker'], work, args, {'guardrails.json': '{"rules":{"team":true}}'})
    assert additions == ['-c', 'sandbox_workspace_write.writable_roots=' + json.dumps([str(root / '.git/worktrees/helper'), str(root / '.git/objects'),
        str(root / '.git/refs/heads/acc/helper'), str(root / '.git/logs/refs/heads/acc/helper')])]


@pytest.mark.parametrize(('sandbox', 'team'), [('read-only', True), ('danger-full-access', True), ('workspace-write', False)])
def test_no_git_scope_widening_for_other_modes(env, sandbox, team):
    files = {'guardrails.json': json.dumps({'rules': {'team': team}})}
    assert ops._codex_team_git_args(env['broker'], env['workspaces'] / 'demo', ['--sandbox', sandbox], files) == []


def test_independent_checkout_needs_no_external_git_root(env):
    root = env['workspaces'] / 'demo'
    repo(root)
    assert ops._codex_team_git_args(env['broker'], root, ['--sandbox', 'workspace-write'],
                                    {'guardrails.json': '{"rules":{"team":true}}'}) == []


@pytest.mark.parametrize('depth', [0, 1, 2])
def test_codex_team_contract_explains_actual_deferred_tool_discovery(depth):
    text = render(LaunchInputs(provider='codex'), position(depth), tools={'report', 'plan'}, v2=True)
    assert 'ALL_TOOLS' in text and 'functions.exec' in text
    assert 'mcp__acc_team__report' in text and 'mcp__acc_team__plan' in text
    assert 'not evidence that the server is unavailable' in text
    assert 'ALL_TOOLS' not in render(LaunchInputs(provider='claude'), position(depth), tools={'report'})


def test_codex_mcp_catalog_is_required_before_launch(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    files = team_files({'guardrails.json': '{"rules":{}}', 'codex-overrides.json': '[]'}, 'codex', '/test/acc-hook')
    settings = json.loads(files['codex-overrides.json'])
    assert 'mcp_servers.acc-team.startup_readiness="catalog"' in settings
    assert 'mcp_servers.acc-team.required=true' in settings


def test_external_worktree_metadata_is_not_granted(env, tmp_path):
    outside = tmp_path / 'outside-repo'
    repo(outside)
    work = env['workspaces'] / 'helper'
    subprocess.run(['git', '-C', str(outside), 'worktree', 'add', '-qb', 'acc/helper/work', str(work)], check=True)
    with pytest.raises(ops.OpError, match='outside this account'):
        ops._codex_team_git_args(env['broker'], work, ['--sandbox', 'workspace-write'],
                                {'guardrails.json': '{"rules":{"team":true}}'})


def test_broker_launch_applies_codex_git_scope_after_resolving_worktree(env):
    import shlex
    root = env['workspaces'] / 'demo'
    repo(root)
    work = env['workspaces'] / 'helper'
    subprocess.run(['git', '-C', str(root), 'worktree', 'add', '-qb', 'acc/helper/work', str(work)], check=True)
    result = ops.launch(env['broker'], {'acc_id': 'abcdef123456', 'tmux_name': 'acc-codex-abcdef12', 'provider': 'codex',
        'args': ['--sandbox', 'workspace-write', '--ask-for-approval', 'on-request'], 'cwd': str(work),
        'files': {'guardrails.json': '{"rules":{"team":true}}'}})
    command = shlex.split(result['command'])
    assert 'sandbox_workspace_write.writable_roots=' + json.dumps(
        [str(root / '.git/worktrees/helper'), str(root / '.git/objects'),
        str(root / '.git/refs/heads/acc/helper'), str(root / '.git/logs/refs/heads/acc/helper')]) in command


@pytest.mark.parametrize('name', ['objects', 'refs', 'logs'])
def test_git_data_links_cannot_redirect_grants_to_hooks(env, name):
    root = env['workspaces'] / 'demo'
    repo(root)
    work = env['workspaces'] / 'helper'
    subprocess.run(['git', '-C', str(root), 'worktree', 'add', '-qb', 'acc/helper/work', str(work)], check=True)
    data = root / '.git' / name
    data.rename(data.with_name(name + '.saved'))
    data.symlink_to(root / '.git/hooks', target_is_directory=True)
    with pytest.raises(ops.OpError, match='not links or files'):
        ops._codex_team_git_args(env['broker'], work, ['--sandbox', 'workspace-write'],
                                {'guardrails.json': '{"rules":{"team":true}}'})


@pytest.mark.parametrize('reflogs', [True, False])
def test_only_needed_missing_git_data_directories_are_prepared(env, reflogs):
    import shutil
    root = env['workspaces'] / 'demo'
    repo(root)
    work = env['workspaces'] / 'helper'
    subprocess.run(['git', '-C', str(root), 'worktree', 'add', '-qb', 'acc/helper/work', str(work)], check=True)
    subprocess.run(['git', '-C', str(root), 'config', 'core.logAllRefUpdates', str(reflogs).lower()], check=True)
    subprocess.run(['git', '-C', str(root), 'pack-refs', '--all'], check=True)
    shutil.rmtree(root / '.git/logs')
    config_before = (root / '.git/config').read_bytes()
    additions = ops._codex_team_git_args(env['broker'], work, ['--sandbox', 'workspace-write'],
                             {'guardrails.json': '{"rules":{"team":true}}'})
    roots = json.loads(additions[1].split('=', 1)[1])
    assert (str(root / '.git/logs/refs/heads/acc/helper') in roots) is reflogs
    assert str(root / '.git/refs') not in roots and str(root / '.git/logs') not in roots
    assert (root / '.git/refs').is_dir()
    assert (root / '.git/logs').exists() is reflogs
    assert (root / '.git/config').read_bytes() == config_before


def test_legacy_shared_ref_directory_is_not_granted(env):
    root = env['workspaces'] / 'demo'
    repo(root)
    work = env['workspaces'] / 'helper'
    subprocess.run(['git', '-C', str(root), 'worktree', 'add', '-qb', 'acc/legacy', str(work)], check=True)
    with pytest.raises(ops.OpError, match='private branch'):
        ops._codex_team_git_args(env['broker'], work, ['--sandbox', 'workspace-write'],
                                {'guardrails.json': '{"rules":{"team":true}}'})


@pytest.mark.parametrize('name', ['refs/heads/acc', 'refs/heads/acc/helper',
                                  'logs/refs/heads/acc', 'logs/refs/heads/acc/helper'])
def test_private_ref_namespace_links_cannot_redirect_grants(env, name):
    root = env['workspaces'] / 'demo'
    repo(root)
    work = env['workspaces'] / 'helper'
    subprocess.run(['git', '-C', str(root), 'worktree', 'add', '-qb', 'acc/helper/work', str(work)], check=True)
    data = root / '.git' / name
    data.rename(data.with_name(data.name + '.saved'))
    data.symlink_to(root / '.git/hooks', target_is_directory=True)
    with pytest.raises(ops.OpError, match='not links or files'):
        ops._codex_team_git_args(env['broker'], work, ['--sandbox', 'workspace-write'],
                                {'guardrails.json': '{"rules":{"team":true}}'})


def test_private_branch_bundle_names_do_not_alias_legacy_branches():
    from acc.server.orchestrator.gates import BRANCH
    assert ops.BRANCH.fullmatch('acc/team-example/work')
    assert BRANCH.fullmatch('acc/team-example/work')
    assert ops._merge_bundle('acc/team-example/work') != ops._merge_bundle('acc/team-example-work')
    assert '/' not in ops._merge_bundle('acc/team-example/work')


async def test_new_codex_team_lead_uses_a_private_ref_namespace(api, project, env):
    from test_team import grant
    repo(env['workspaces'] / 'demo')
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'codex',
        'name': 'Codex lead', 'worktree': True, 'sandbox': 'workspace-write', 'approval': 'on-request',
        'orchestrator': grant()})
    assert response.status_code == 201, response.text
    branch = response.json()['worktree_branch']
    assert branch.startswith('acc/') and branch.endswith('/work')


@pytest.mark.parametrize('sandbox', ['read-only', 'workspace-write'])
async def test_preview_and_launch_use_actual_codex_sandbox_for_mcp_approvals(api, project, env, monkeypatch, sandbox):
    import tomllib

    from test_team import grant

    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    repo(env['workspaces'] / 'demo')
    body = {'project_id': project['id'], 'provider': 'codex', 'name': 'Approval scope',
            'worktree': True, 'sandbox': sandbox, 'approval': 'on-request', 'orchestrator': grant()}
    for address, expected_status in (('/api/sessions/preview', 200), ('/api/sessions', 201)):
        response = await api.post(address, json=body)
        assert response.status_code == expected_status, response.text
        result = response.json()
        files = result['files'] if address.endswith('preview') else result['launch_spec']['files']
        settings = tomllib.loads('\n'.join(json.loads(files['codex-overrides.json'])))['mcp_servers']['acc-team']
        assert settings['default_tools_approval_mode'] == ('prompt' if sandbox == 'read-only' else 'approve')
        if sandbox == 'read-only':
            assert settings['tools']['brief_documents']['approval_mode'] == 'approve'
            assert settings['tools']['brief_propose_documents']['approval_mode'] == 'prompt'
            assert settings['tools']['brief_share']['approval_mode'] == 'prompt'
