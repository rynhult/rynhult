"""Team permission exceptions grant only the fixed acc-team server."""
import copy
import json

import pytest

from acc.server.catalog import Catalog
from acc.server.compiler import LaunchInputs, compile_files, session_environment
from acc.server.teams import team_files


@pytest.mark.parametrize('lead', [False, True])
@pytest.mark.parametrize('provider', ['claude', 'codex', 'copilot', 'cursor', 'opencode', 'antigravity'])
def test_team_permissions_preserve_unrelated_servers_and_command_approval(provider, lead, monkeypatch):
    monkeypatch.setenv("ACC_ORCHESTRATOR", "1")
    monkeypatch.setenv("ACC_ORCHESTRATOR_V2", "1")
    catalog = Catalog(root=None, agents={}, mcps={}, skills={}, profiles={}, deferred=[], revision='test')
    inputs = LaunchInputs(provider=provider, permission_mode='acceptEdits')
    files = compile_files(inputs, '/test/acc-hook', '/test/status', catalog=catalog,
                          guardrails={'rules': {}}).files
    if provider == 'claude':
        settings = json.loads(files['settings.json'])
        settings['permissions'] = {'allow': ['Read'], 'deny': ['Bash(rm:*)'], 'ask': ['mcp__other__*']}
        files['settings.json'] = json.dumps(settings)
    elif provider == 'codex':
        settings = json.loads(files['codex-overrides.json'])
        settings += ['mcp_servers.other.command="/test/other"', 'mcp_servers.other.default_tools_approval_mode="prompt"']
        files['codex-overrides.json'] = json.dumps(settings)
    elif provider == 'opencode':
        files['opencode-mcp.json'] = json.dumps({'mcp': {'servers': {'other': {'type':'local', 'command':['/test/other']}}}})
    else:
        key = {'copilot':'mcp.json', 'cursor':'cursor/acc/.mcp.json',
               'antigravity':'antigravity/.agents/plugins/acc/mcp_config.json'}[provider]
        files[key] = json.dumps({'mcpServers': {'other': {'command':'/test/other', 'args':[]}}})
    original = copy.deepcopy(files)
    output = team_files(files, provider, '/test/acc-hook', lead=lead, inputs=inputs)
    assert files == original  # Frozen launch input is not modified.
    if provider == 'claude':
        settings = json.loads(output['settings.json'])
        assert settings['permissions'] == {
            'allow':['Read', 'mcp__acc-team__*'], 'deny':['Bash(rm:*)'], 'ask':['mcp__other__*']}
        hooks = dict(settings['hooks'])
        approval = hooks['PermissionRequest'].pop()
        assert hooks == json.loads(original['settings.json'])['hooks']
        assert approval['matcher'] == r'^mcp__acc-team__[a-z][a-z0-9]*(_[a-z0-9]+)*$'
        assert approval['hooks'][0]['command'] == '/test/acc-guard claude PermissionRequest'
        assert 'skipDangerousModePermissionPrompt' not in settings
    elif provider == 'codex':
        settings = json.loads(output['codex-overrides.json'])
        assert 'mcp_servers.acc-team.default_tools_approval_mode="approve"' in settings
        assert 'mcp_servers.other.default_tools_approval_mode="prompt"' in settings
        assert all(x in settings for x in json.loads(original['codex-overrides.json']))
    elif provider == 'opencode':
        before = json.loads(session_environment(inputs, catalog, '/run', original)['OPENCODE_CONFIG_CONTENT'])
        after = json.loads(session_environment(inputs, catalog, '/run', output)['OPENCODE_CONFIG_CONTENT'])
        assert after['permissions'][:len(before['permissions'])] == before['permissions']
        exceptions = after['permissions'][len(before['permissions']):]
        assert {'action':'acc-team_start', 'resource':'*', 'effect':'allow'} in exceptions
        assert {'action':'acc-team_brief', 'resource':'*', 'effect':'allow'} in exceptions
        assert all(rule['effect'] == 'allow' and rule['resource'] == '*' and
                   rule['action'].startswith('acc-team_') and '*' not in rule['action'] for rule in exceptions)
        assert not any(rule['action'] == 'acc-team_other_start' for rule in exceptions)
        assert json.loads(output['opencode-mcp.json'])['mcp']['servers']['other'] == {
            'type':'local', 'command':['/test/other']}
    else:
        settings = json.loads(output[key])
        assert settings['mcpServers']['other'] == {'command':'/test/other', 'args':[]}
        server = settings['mcpServers']['acc-team']
        assert server['command'] == '/test/acc-mcp-team'
        if provider == 'copilot':
            assert server['tools'] == ['*']
        else:
            # Neither CLI offers a documented per-session permission overlay here.
            # Keep their account-wide approval stores and unrelated MCP policy untouched.
            assert set(server) == {'command', 'args'}
            assert set(output) == set(original) | {'guardrails.json'}


def test_solo_claude_has_no_team_permission_exception():
    files = compile_files(LaunchInputs(provider='claude'), '/test/acc-hook', '/test/status').files
    assert 'permissions' not in json.loads(files['settings.json'])


def test_opencode_covers_discovered_tools_even_when_server_will_refuse_a_disabled_slice(monkeypatch):
    from test_orchestrator_slices import listed

    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '0')
    inputs = LaunchInputs(provider='opencode')
    files = compile_files(inputs, '/test/acc-hook', '/test/status', guardrails={'rules': {}}).files
    output = team_files(files, 'opencode', '/test/acc-hook')
    permissions = json.loads(session_environment(inputs, None, '/run', output)['OPENCODE_CONFIG_CONTENT'])['permissions']
    # Discovery only, using the existing isolated stdio fixture; no tool calls or live spool.
    tools = listed('--orchestrator', '--orchestrator-v2')
    granted = {rule['action'] for rule in permissions if rule['effect'] == 'allow'}
    assert {f'acc-team_{tool}' for tool in tools} <= granted


@pytest.mark.parametrize('lead', [False, True])
def test_claude_plan_team_keeps_plan_mode_and_uses_scoped_permission_handler(lead):
    import re

    inputs = LaunchInputs(provider='claude', permission_mode='plan')
    compiled = compile_files(inputs, '/test/acc-hook', '/test/status', guardrails={'rules': {}})
    files = team_files(compiled.files, 'claude', '/test/acc-hook', lead=lead)
    hooks = json.loads(files['settings.json'])['hooks']
    handler = hooks['PermissionRequest'][-1]
    for name in ('report', 'plan', 'brief', 'plan_update'):
        assert re.fullmatch(handler['matcher'], 'mcp__acc-team__'+name)
    for name in ('Write', 'Edit', 'Bash', 'mcp__other__report', 'mcp__acc-team__evil__report'):
        assert not re.fullmatch(handler['matcher'], name)
    assert 'bypassPermissions' not in files['settings.json']
    assert 'acceptEdits' not in files['settings.json']
    assert json.loads(files['guardrails.json'])['rules']['team'] is True


@pytest.mark.parametrize('provider', ['codex', 'copilot', 'opencode'])
@pytest.mark.parametrize('lead', [False, True])
def test_provider_team_discovery_covers_document_tools_without_widening_permissions(provider, lead, monkeypatch):
    from test_orchestrator_slices import listed

    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    inputs = LaunchInputs(provider=provider, sandbox='read-only', approval='on-request',
                          options={'mode': 'plan', 'permissions': 'ask'} if provider == 'copilot' else {})
    compiled = compile_files(inputs, '/test/acc-hook', '/test/status', guardrails={'rules': {}})
    output = team_files(compiled.files, provider, '/test/acc-hook', lead=lead, inputs=inputs)
    if provider == 'codex':
        settings = json.loads(output['codex-overrides.json'])
        args = json.loads(next(s.split('=', 1)[1] for s in settings if s.startswith('mcp_servers.acc-team.args=')))
        assert 'mcp_servers.acc-team.default_tools_approval_mode="prompt"' in settings
        assert 'mcp_servers.acc-team.tools.brief_documents.approval_mode="approve"' in settings
        assert 'mcp_servers.acc-team.tools.brief_share.approval_mode="prompt"' in settings
        assert all(s in settings for s in json.loads(compiled.files['codex-overrides.json']))
    elif provider == 'copilot':
        server = json.loads(output['mcp.json'])['mcpServers']['acc-team']
        args = server['args']
        assert server['tools'] == ['*']
        assert inputs.options == {'mode': 'plan', 'permissions': 'ask'}
    else:
        server = json.loads(output['opencode-mcp.json'])['mcp']['servers']['acc-team']
        args = server['command'][1:]
        permissions = json.loads(session_environment(inputs, None, '/run', output)['OPENCODE_CONFIG_CONTENT'])['permissions']
        assert {f'acc-team_{name}' for name in ('brief_documents', 'brief_propose_documents', 'brief_share')} <= {
            rule['action'] for rule in permissions if rule['effect'] == 'allow'}
    assert {'brief_documents', 'brief_propose_documents', 'brief_share'} <= set(listed(*args))
    assert inputs.sandbox == 'read-only' and inputs.approval == 'on-request'


@pytest.mark.parametrize('sandbox', ['read-only', 'workspace-write'])
@pytest.mark.parametrize('approval', ['on-request', 'never'])
@pytest.mark.parametrize('lead', [False, True])
def test_codex_mutations_prompt_only_in_readonly_team_launches(sandbox, approval, lead, monkeypatch):
    import tomllib

    from test_orchestrator_slices import listed

    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    inputs = LaunchInputs(provider='codex', sandbox=sandbox, approval=approval)
    compiled = compile_files(inputs, '/test/acc-hook', '/test/status', guardrails={'rules': {}})
    files = team_files(compiled.files, 'codex', '/test/acc-hook', lead=lead, inputs=inputs)
    config = tomllib.loads('\n'.join(json.loads(files['codex-overrides.json'])))['mcp_servers']['acc-team']
    readonly = sandbox == 'read-only'
    assert config['default_tools_approval_mode'] == ('prompt' if readonly else 'approve')
    tools = listed(*config['args'])
    if readonly:
        for name in ('brief', 'brief_documents', 'plan', 'list', 'report'):
            assert config['tools'][name]['approval_mode'] == 'approve'
        for name in ('brief_propose_documents', 'brief_share', 'brief_reference', 'plan_update', 'merge', 'gate'):
            assert config['tools'][name]['approval_mode'] == 'prompt'
        # Default prompt closes future names; every currently known mutation is
        # also explicit, overriding any account-level per-tool approval.
        from test_guardrails import guard
        for name in tools:
            assert config['tools'][name]['approval_mode'] == (
                'approve' if name in guard.READ_ONLY_TEAM_TOOLS or name == 'report' else 'prompt')
    else:
        assert not config.get('tools')
    from acc.server.compiler import build_args
    argv = build_args(inputs, run_dir='/test/run', tmux_name='test', native_id=None, resume=False, files=files)
    assert argv[argv.index('--sandbox') + 1] == sandbox
    assert argv[argv.index('--ask-for-approval') + 1] == approval
    assert json.loads(compiled.files['codex-overrides.json']) != json.loads(files['codex-overrides.json'])


@pytest.mark.parametrize('mode', ['plan', 'interactive'])
@pytest.mark.parametrize('lead', [False, True])
def test_copilot_team_discovery_is_not_a_mutation_approval(mode, lead, monkeypatch):
    from test_orchestrator_slices import listed

    from acc.server.compiler import build_args

    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    inputs = LaunchInputs(provider='copilot', options={'mode': mode, 'permissions': 'ask'})
    compiled = compile_files(inputs, '/test/acc-hook', '/test/status', guardrails={'rules': {}})
    files = team_files(compiled.files, 'copilot', '/test/acc-hook', lead=lead, inputs=inputs)
    server = json.loads(files['mcp.json'])['mcpServers']['acc-team']
    assert server['tools'] == ['*']
    assert {'brief_documents', 'brief_share', 'brief_propose_documents'} <= set(listed(*server['args']))
    argv = build_args(inputs, run_dir='/test/run', tmux_name='test', native_id='a' * 32, resume=False, files=files)
    assert '--allow-all-tools' not in argv and '--allow-all' not in argv and '--allow-tool' not in argv
    assert ('--mode' in argv) is (mode == 'plan')
    if mode == 'plan':
        assert argv[argv.index('--mode') + 1] == 'plan'
    assert inputs.options == {'mode': mode, 'permissions': 'ask'}


def test_missing_codex_inputs_never_fall_back_to_server_wide_approval():
    import tomllib

    compiled = compile_files(LaunchInputs(provider='codex'), '/test/acc-hook', '/test/status', guardrails={'rules': {}})
    files = team_files(compiled.files, 'codex', '/test/acc-hook')
    settings = tomllib.loads('\n'.join(json.loads(files['codex-overrides.json'])))['mcp_servers']['acc-team']
    assert settings['default_tools_approval_mode'] == 'prompt'
    assert settings['tools']['brief_documents']['approval_mode'] == 'approve'
    assert settings['tools']['brief_share']['approval_mode'] == 'prompt'
    assert settings['tools']['brief_propose_documents']['approval_mode'] == 'prompt'


def test_codex_readonly_explicit_mutation_overrides_follow_prior_approval_settings():
    inputs = LaunchInputs(provider='codex', sandbox='read-only', approval='on-request')
    compiled = compile_files(inputs, '/test/acc-hook', '/test/status', guardrails={'rules': {}})
    prior = json.loads(compiled.files['codex-overrides.json'])
    prior += ['mcp_servers.acc-team.tools.brief_share.approval_mode="approve"',
              'mcp_servers.acc-team.tools.brief_propose_documents.approval_mode="approve"']
    files = team_files({**compiled.files, 'codex-overrides.json': json.dumps(prior)}, 'codex', '/test/acc-hook', inputs=inputs)
    # Codex receives ordered -c overrides: the last value for a key wins.
    settings = dict(entry.split('=', 1) for entry in json.loads(files['codex-overrides.json']))
    assert settings['mcp_servers.acc-team.tools.brief_share.approval_mode'] == '"prompt"'
    assert settings['mcp_servers.acc-team.tools.brief_propose_documents.approval_mode'] == '"prompt"'
