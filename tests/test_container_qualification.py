"""Phase-1 qualification must fail closed on incomplete runtime evidence."""
import importlib.util
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The launcher's one declaration; the driver reads the installed launcher's (launcher_container_uid).
LAUNCHER_UID = int(re.search(r'^CONTAINER_UID = ([0-9]+)$', (ROOT / 'deploy/containers/acc-containers').read_text(),
                             re.MULTILINE)[1])


@pytest.fixture
def as_agent_account(monkeypatch):
    """probe() rightly refuses root; under a root-run suite, test it as an unprivileged account (nobody)."""
    import os
    if os.getuid() == 0:
        monkeypatch.setattr(os, 'getuid', lambda: 65534)
    return os.getuid()


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/container_qualification' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.path.insert(0, str(ROOT / "scripts/container_qualification"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def test_go_requires_every_mandatory_check_and_launcher():
    q = load('phase1')
    checks = {name: {'status': 'pass', 'reason': 'observed'} for name in q.MANDATORY}
    assert q.verdict(checks, trusted_launcher=True)
    assert not q.verdict(checks, trusted_launcher=False)
    for name in q.MANDATORY:
        for status in ('skip', 'fail'):
            assert not q.verdict(checks | {name: {'status': status, 'reason': 'unavailable'}}, trusted_launcher=True)
        assert not q.verdict({k: v for k, v in checks.items() if k != name}, trusted_launcher=True)


def test_provider_evidence_requires_deny_and_successful_post_for_exact_commands():
    q = load('phase1')
    denied = {'provider': 'codex', 'event': 'PreToolUse', 'hook': {'tool_name': 'exec_command',
              'tool_input': {'command': q.FORBIDDEN}}, 'guard': {'rule': 'custom', 'reason': 'qualification forbidden command'}}
    post = {'provider': 'codex', 'event': 'PostToolUse', 'hook': {'tool_name': 'exec_command',
            'tool_input': {'command': q.ALLOWED}}}
    assert q.provider_evidence('codex', [denied, post], forbidden_exists=False, allowed_exists=True)
    assert not q.provider_evidence('codex', [denied], forbidden_exists=False, allowed_exists=True)
    assert not q.provider_evidence('codex', [denied, post], forbidden_exists=True, allowed_exists=True)
    assert not q.provider_evidence('claude', [denied, post], forbidden_exists=False, allowed_exists=True)
    assert not q.provider_evidence('codex', [denied | {'guard': {}}, post], forbidden_exists=False, allowed_exists=True)


def test_unknown_or_malformed_results_never_go():
    q = load('phase1')
    for value in ({}, None, [], {'status': 'PASS'}, {'status': 'pass', 'reason': ''}):
        assert not q.verdict(dict.fromkeys(q.MANDATORY, value), trusted_launcher=True)


def test_network_none_requires_only_loopback_and_no_routes():
    q = load('phase1')
    assert q.network_none(['lo'], 'Iface Destination Gateway Flags\n', '00000000000000000000000000000000 00 00000000000000000000000000000000 00 00000000000000000000000000000000 ffffffff 00000001 00000000 00200200 lo\n')
    assert not q.network_none(['lo', 'eth0'], '', '')
    assert not q.network_none(['lo'], 'Iface Destination Gateway Flags\nlo 00000000 0100007F 0003\n', '')


def test_replay_only_uses_advertised_shell_tool():
    replay = load('replay')
    for name, key in [('Bash', 'command'), ('exec_command', 'cmd'), ('bash', 'command')]:
        assert replay.tool_arguments(name, 'echo example') == {key: 'echo example'}
    with pytest.raises(ValueError):
        replay.tool_arguments('unknown', 'echo example')



def test_cgroup_limits_read_kernel_controls(tmp_path):
    driver = load('driver')
    for name, value in {'cpu.max': '200000 100000', 'memory.max': str(4096 * 1024 * 1024),
                        'memory.swap.max': '0', 'pids.max': '512'}.items():
        (tmp_path / name).write_text(value)
    assert driver.kernel_limits(tmp_path, {'cpus': 2, 'memory_mib': 4096, 'pids': 512})
    (tmp_path / 'cpu.max').write_text('max 100000')
    assert not driver.kernel_limits(tmp_path, {'cpus': 2, 'memory_mib': 4096, 'pids': 512})


def test_driver_rejects_payload_go_and_unknown_checks():
    driver = load('driver')
    with pytest.raises(ValueError):
        driver.payload_report({'go_phase_1': True, 'checks': {}})
    with pytest.raises(ValueError):
        driver.payload_report({'go_phase_1': False, 'checks': {'invented': {'status': 'pass', 'reason': 'yes'}}})


def test_driver_requires_dedicated_qualification_policy():
    driver = load('driver')
    plan = {'account': 'alex-agent', 'repo': '/srv/demo/repo', 'root': '/srv/demo',
            'policy_id': 'default', 'image': 'sha256:' + 'a' * 64}
    with pytest.raises(ValueError, match='qualification'):
        driver.check_plan(plan)
    plan['policy_id'] = 'qualification-phase1'
    assert driver.check_plan(plan) == plan


def test_provider_evidence_does_not_accept_failed_or_unrelated_post():
    q = load('phase1')
    denied = {'provider': 'claude', 'event': 'PreToolUse', 'hook': {'tool_name': 'Bash',
              'tool_input': {'command': q.FORBIDDEN}}, 'guard': {'rule': 'custom', 'reason': 'qualification'}}
    post = {'provider': 'claude', 'event': 'PostToolUseFailure', 'hook': {'tool_name': 'Bash',
            'tool_input': {'command': q.ALLOWED}}}
    assert not q.provider_evidence('claude', [denied, post], forbidden_exists=False, allowed_exists=True)


def test_replay_drives_real_http_protocol_without_calling_hooks():
    import json
    import urllib.request

    replay = load('replay')
    with replay.Replay(['echo example']) as model:
        request = urllib.request.Request(model.url + '/v1/messages', data=json.dumps({
            'tools': [{'name': 'Bash'}]}).encode(), headers={'Content-Type': 'application/json'})
        output = urllib.request.urlopen(request).read().decode()
        assert '"name": "Bash"' in output
        assert 'echo example' in output
        assert 'message_stop' in output
        assert model.calls == 1 and model.errors == 0


@pytest.mark.parametrize("failed_first", [False, True])
def test_driver_stops_both_generations_and_revokes_grant(tmp_path, monkeypatch, failed_first, as_agent_account):
    import base64
    import json
    from types import SimpleNamespace

    driver = load('driver')
    uid = driver.os.getuid()
    monkeypatch.setattr(driver.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=uid))
    monkeypatch.setattr(driver, 'policy_matches', lambda _: True)
    monkeypatch.setattr(driver, 'host_image_evidence', lambda _: True)
    monkeypatch.setattr(driver, 'launcher_host', lambda **_: [])
    monkeypatch.setattr(driver, 'launcher_container_uid', lambda: LAUNCHER_UID)
    monkeypatch.setattr(driver.time, 'sleep', lambda _: None)
    limits_results = iter([not failed_first, True])
    monkeypatch.setattr(driver, 'cgroup_evidence', lambda _: (next(limits_results), '/example'))
    monkeypatch.setattr(driver, 'broker_pid', lambda _: 12)
    monkeypatch.setattr(driver, 'foreign_processes', lambda *a, **k: [])
    folder = tmp_path / 'grants'
    folder.mkdir(mode=0o750)
    if folder.stat().st_uid != as_agent_account:
        os.chown(folder, as_agent_account, -1)  # root-run suite: the control account owns its grants folder
    generation = 0
    stops = []
    reads = {}
    challenge = None
    def call(account, method, params):
        nonlocal generation, challenge
        if method == 'ping':
            return {'role': 'agents', 'user': account, 'run_dir': '/example/run'}
        if method == 'make_private_checkout':
            return {'path': '/example/work/.worktrees/qualification'}
        if method == 'launch':
            generation = params['container']['generation']
            grant = json.loads(next(folder.glob('*.json')).read_text())
            assert grant['generation'] == generation
            challenge = grant['args'][1] if len(grant['args']) == 2 else str(generation) * 32
            return {'container': {'unit': f'acc-ct-{params["acc_id"]}-g{generation}.service', 'state': 'running'}}
        if method == 'read_artifact':
            reads[generation] = reads.get(generation, 0) + 1
            nonce = '0' * 32 if reads[generation] == 1 else challenge
            payload = {'version': 1, 'go_phase_1': False, 'nonce': nonce, 'uid': LAUNCHER_UID,
                       'no_new_privs': '1', 'checks': {name: {'status': 'pass', 'reason': 'observed'}
                                                       for name in driver.MANDATORY}}
            # A first-generation failure must survive the successful restart.
            if generation == 1 and failed_first:
                payload['checks']['claude_hooks'] = {'status': 'fail', 'reason': 'no denial'}
            data = json.dumps(payload).encode()
            return {'size': len(data), 'data': base64.b64encode(data).decode()}
        if method == 'kill':
            stops.append(params['container']['generation'])
            return {'container': {'stopped': True}}
        pytest.fail(method)
    plan = {'account': 'alex-agent', 'repo': '/example/repo', 'root': '/example/work',
            'policy_id': 'qualification-phase1', 'image': 'sha256:' + 'a' * 64}
    report = driver.qualify(plan, grants=folder, call=call)
    assert stops == [1, 2]
    assert reads == {1: 2, 2: 2}  # stale artifact must never count as the new launch
    assert report['checks']['restart_stop_cleanup']['status'] == 'pass'
    assert report['go_phase_1'] is (not failed_first)
    expected = 'fail' if failed_first else 'pass'
    assert report['checks']['claude_hooks']['status'] == expected
    assert report['checks']['per_session_limits']['status'] == expected
    assert not list(folder.iterdir())


def test_driver_names_the_step_a_lifecycle_failure_stopped_at(tmp_path, monkeypatch, as_agent_account):
    """Attempt 16 stopped after generation 1 with only 'RuntimeError'. Failing payload rows never stop the run (the
    test above), so the row now says which step stopped it."""
    import base64
    import json
    from types import SimpleNamespace

    driver = load('driver')
    uid = driver.os.getuid()
    monkeypatch.setattr(driver.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=uid))
    monkeypatch.setattr(driver, 'policy_matches', lambda _: True)
    monkeypatch.setattr(driver, 'host_image_evidence', lambda _: True)
    monkeypatch.setattr(driver, 'launcher_host', lambda **_: [])
    monkeypatch.setattr(driver, 'launcher_container_uid', lambda: LAUNCHER_UID)
    monkeypatch.setattr(driver.time, 'sleep', lambda _: None)
    monkeypatch.setattr(driver, 'cgroup_evidence', lambda _: (True, '/example'))
    monkeypatch.setattr(driver, 'broker_pid', lambda _: 12)
    monkeypatch.setattr(driver, 'foreign_processes', lambda *a, **k: [])
    folder = tmp_path / 'grants'
    folder.mkdir(mode=0o750)
    if folder.stat().st_uid != as_agent_account:
        os.chown(folder, as_agent_account, -1)
    state = {}

    def call(account, method, params):
        if method == 'ping':
            return {'role': 'agents', 'user': account, 'run_dir': '/example/run'}
        if method == 'make_private_checkout':
            return {'path': '/example/work/.worktrees/qualification'}
        if method == 'launch':
            state['generation'] = params['container']['generation']
            if state['generation'] == 2:
                raise RuntimeError(driver.refusal('launch', 'OpError: container launch refused: launcher_task_limit: '
                                                            'secret launch detail'))
            state['challenge'] = json.loads(next(folder.glob('*.json')).read_text())['args'][1]
            return {'container': {'unit': f'acc-ct-{params["acc_id"]}-g1.service', 'state': 'running'}}
        if method == 'read_artifact':
            payload = {'version': 1, 'go_phase_1': False, 'nonce': state['challenge'], 'uid': LAUNCHER_UID,
                       'no_new_privs': '1', 'checks': {name: {'status': 'fail', 'reason': 'failing row'}
                                                       for name in driver.MANDATORY}}
            data = json.dumps(payload).encode()
            return {'size': len(data), 'data': base64.b64encode(data).decode()}
        if method == 'kill':
            return {'container': {'stopped': True}}
        pytest.fail(method)
    plan = {'account': 'alex-agent', 'repo': '/example/repo', 'root': '/example/work',
            'policy_id': 'qualification-phase1', 'image': 'sha256:' + 'a' * 64}
    report = driver.qualify(plan, grants=folder, call=call)
    reason = report['checks']['restart_stop_cleanup']['reason']
    assert report['stopped_at'] == 'generation 2: launch'
    assert reason == ('did not complete both generations: stopped at generation 2: launch (broker operation refused: '
                      'launch (launcher: launcher_task_limit)), after 1 completed generation(s); failing payload rows '
                      'never stop the run')
    assert 'secret' not in json.dumps(report)
    assert report['reason'] == 'qualification stopped: broker operation refused: launch (launcher: launcher_task_limit)'


def test_broker_refusals_keep_only_the_launchers_fixed_code():
    driver = load('driver')
    assert driver.refusal('kill', 'OpError: something /home/x/secret') == 'broker operation refused: kill'
    assert driver.refusal('launch', None) == 'broker operation refused: launch'
    assert driver.refusal('launch', 'OpError: container launch refused: Busy Thing: x') == \
        'broker operation refused: launch'


def test_phase1_cli_missing_plan_is_structured_no_go(tmp_path):
    import json
    import os
    import subprocess
    import sys

    completed = subprocess.run([sys.executable, str(ROOT / 'scripts/qualify_containers.py'),
        '--enable-prototype', '--phase-1-plan', str(tmp_path / 'missing.json')],
        env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')}, capture_output=True, text=True)
    assert completed.returncode in (1, 2)
    report = json.loads(completed.stdout)
    assert report['go_phase_1'] is False
    assert report['checks'] and all(item['status'] in ('fail', 'skip') for item in report['checks'].values())


def test_default_qualification_lists_mandatory_skips():
    import json
    import subprocess
    import sys

    completed = subprocess.run([sys.executable, str(ROOT / 'scripts/qualify_containers.py')],
                               capture_output=True, text=True)
    report = json.loads(completed.stdout)
    assert set(report['checks']) == set(load('phase1').MANDATORY)
    assert all(check['status'] == 'skip' and check['reason'] for check in report['checks'].values())


def test_provider_evidence_normalizes_native_toolcall_payload():
    q = load('phase1')
    denied = {'provider': 'copilot', 'event': 'PreToolUse', 'hook': {'toolCall': {
        'name': 'bash', 'args': {'command': q.FORBIDDEN}}}, 'guard': {'rule': 'custom', 'reason': 'qualification'}}
    post = {'provider': 'copilot', 'event': 'PostToolUse', 'hook': {'toolCall': {
        'name': 'bash', 'args': {'command': q.ALLOWED}}}}
    assert q.provider_evidence('copilot', [denied, post], forbidden_exists=False, allowed_exists=True)


def test_driver_does_not_revoke_a_colliding_grant(tmp_path, monkeypatch):
    from types import SimpleNamespace

    driver = load('driver')
    uid = driver.os.getuid()
    monkeypatch.setattr(driver.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=uid))
    monkeypatch.setattr(driver, 'policy_matches', lambda _: True)
    monkeypatch.setattr(driver, 'host_image_evidence', lambda _: True)
    monkeypatch.setattr(driver, 'launcher_host', lambda **_: [])
    monkeypatch.setattr(driver, 'launcher_container_uid', lambda: LAUNCHER_UID)
    monkeypatch.setattr(driver, 'broker_pid', lambda _: 4242)
    monkeypatch.setattr(driver, 'foreign_processes', lambda *a, **k: [])
    monkeypatch.setattr(driver.uuid, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    folder = tmp_path / 'grants'
    folder.mkdir(mode=0o750)
    existing = folder / ('a' * 32 + '.json')
    existing.write_text('existing grant')
    def call(account, method, params):
        if method == 'ping':
            return {'role': 'agents', 'user': account, 'run_dir': '/example/run'}
        if method == 'make_private_checkout':
            return {'path': '/example/work/.worktrees/qualification'}
        pytest.fail('a collision must never reach launch or stop')
    plan = {'account': 'alex-agent', 'repo': '/example/repo', 'root': '/example/work',
            'policy_id': 'qualification-phase1', 'image': 'sha256:' + 'a' * 64}
    report = driver.qualify(plan, grants=folder, call=call)
    assert report['go_phase_1'] is False
    assert existing.read_text() == 'existing grant'


def test_replay_advertises_the_models_requested_by_the_pinned_clis():
    import json
    import urllib.request

    replay = load('replay')
    with replay.Replay([]) as model:
        response = json.load(urllib.request.urlopen(model.url + '/v1/models'))
    assert {'qualification', 'gpt-4o', 'claude-sonnet-4-5'} <= {row['id'] for row in response['data']}


@pytest.mark.parametrize('field', ['account', 'repo', 'root', 'policy_id', 'image'])
def test_malformed_plan_fields_are_refused(field):
    driver = load('driver')
    plan = {'account': 'alex-agent', 'repo': '/example/repo', 'root': '/example/work',
            'policy_id': 'qualification-phase1', 'image': 'sha256:' + 'a' * 64}
    plan[field] = None
    with pytest.raises(ValueError):
        driver.check_plan(plan)


def test_readonly_permissions_on_a_writable_filesystem_are_not_immutable(tmp_path):
    q = load('phase1')
    hooks = tmp_path / 'hooks'
    hooks.mkdir()
    hook = hooks / 'acc.json'
    hook.write_text('{}')
    hook.chmod(0o444)
    hooks.chmod(0o555)
    try:
        assert q.immutable_inputs([hook])['status'] == 'fail'
    finally:
        hooks.chmod(0o755)
        hook.chmod(0o644)


def test_missing_route_evidence_cannot_qualify_network_none():
    q = load('phase1')
    assert not q.network_none(['lo'], '', '')


@pytest.mark.parametrize('reason', [None, True, {}, ''])
def test_check_reasons_must_be_nonempty_strings(reason):
    q = load('phase1')
    with pytest.raises(ValueError):
        q.result('pass', reason)


def test_statusline_writes_only_the_bound_output_with_frozen_inputs(tmp_path):
    import os
    import subprocess

    output = tmp_path / 'statusline.json'
    output.touch()
    tmp_path.chmod(0o555)
    try:
        done = subprocess.run(['/usr/bin/python3', '-I', str(ROOT / 'scripts/container_qualification/statusline.py')], input='{"model":"example"}', text=True,
            env={**os.environ, 'ACC_RUN_DIR': str(tmp_path), 'ACC_SESSION_READONLY_INPUTS': '1'}, capture_output=True)
        assert done.returncode == 0
        assert output.read_text() == '{"model":"example"}\n'
        assert not (tmp_path / 'statusline.json.tmp').exists()
    finally:
        tmp_path.chmod(0o755)


def test_user_manager_accepts_local_image_without_an_archive():
    import argparse
    import importlib.util

    spec = importlib.util.spec_from_file_location('qualify', ROOT / 'scripts/qualify_containers.py')
    q = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(q)
    command, _ = q.user_manager_command(argparse.Namespace(image='sha256:' + 'a' * 64,
                                        image_archive=None, image_store=None, image_runroot=None), 1004)
    assert '--image' in command and '--image-archive' not in command


def test_local_image_spec_has_frozen_inputs_and_no_home_overlay(tmp_path):
    local = load('local')
    command = local.container_command(['podman'], tmp_path, 'sha256:' + 'a' * 64,
                                      'a' * 32, 'b' * 32, 1004, 1004)
    assert '--network=none' in command and '--read-only' in command
    assert '--env=HOME=/home/acc' in command
    assert any('dst=/qualification/input,ro' in item for item in command)
    assert not any(item.startswith('--tmpfs=/home') for item in command)
    assert '--privileged' not in command and not any('unconfined' in item for item in command)


def test_provider_probe_uses_baked_settings_without_writing_the_home(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import acc.providers

    q = load('phase1')
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/container_qualification'))
    monkeypatch.setattr(acc.providers, 'get', lambda _: SimpleNamespace(
        bootstrap=lambda *a, **k: pytest.fail('effective hooks must already be frozen in the image'),
        trust=lambda *a, **k: pytest.fail('runtime must not rewrite trust')))
    class Paths:
        def __new__(cls, value):
            value = str(value)
            return tmp_path / Path(value).name if value.startswith(('/tmp/acc-qualification-', '/tmp/acc-state/')) else Path(value)
        @staticmethod
        def home():
            return tmp_path / 'home'
    monkeypatch.setattr(q, 'Path', Paths)
    def cli(command, **kwargs):
        assert '--settings' not in command
        import json
        assert json.loads((tmp_path / 'claude' / 'settings.json').read_text())['disableAllHooks'] is True
        return 1, ''
    monkeypatch.setattr(q, 'run', cli)
    (tmp_path / 'events.jsonl').touch()
    assert q.provider_probe('claude', tmp_path)['status'] == 'fail'  # CLI error, never a hook-bootstrap write


def test_immutability_requires_kernel_write_refusal_not_just_metadata(tmp_path):
    import os
    from types import SimpleNamespace

    q = load('phase1')
    file = tmp_path / 'guardrails.json'
    file.write_text('original')
    q.os = SimpleNamespace(**(vars(os) | {'access': lambda *a: False,
                           'statvfs': lambda *a: SimpleNamespace(f_flag=os.ST_RDONLY)}))
    assert q.immutable_inputs([file])['status'] == 'fail'
    assert file.read_text() == 'original'


def test_probe_timeout_preserves_bounded_startup_diagnostics():
    import sys

    q = load('phase1')
    code, output = q.run([sys.executable, '-c', 'import time; print("startup marker",flush=True); time.sleep(5)'],
                         timeout=0.1)
    assert code == 124 and 'startup marker' in output and len(output) <= 65536


def test_chrome_failure_reports_its_bounded_sandbox_diagnostic(monkeypatch):
    from types import SimpleNamespace

    q = load('phase1')
    monkeypatch.setattr(q, 'container_mode', lambda: False)
    monkeypatch.setattr(q, 'Path', lambda _: SimpleNamespace(glob=lambda _: [Path('/example/chrome')]))
    def spawn(*args, **kwargs):
        if hasattr(kwargs.get('stderr'), 'write'):
            kwargs['stderr'].write(b'sandbox startup denied')
        return SimpleNamespace(pid=99999999, poll=lambda: 1, wait=lambda: 1)
    monkeypatch.setattr(q.subprocess, 'Popen', spawn)
    monkeypatch.setattr(q.os, 'killpg', lambda *a: None)
    assert 'sandbox startup denied' in q.chrome_sandbox()['reason']


def test_anthropic_beta_query_uses_messages_transport():
    import json
    import urllib.request
    replay = load('replay')
    with replay.Replay(['true']) as fixture:
        request = urllib.request.Request(fixture.url + '/v1/messages?beta=true',
            data=json.dumps({'tools': [{'name': 'Bash'}]}).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request) as response:
            body = response.read().decode()
        assert 'content_block_start' in body and 'input_json_delta' in body
        assert fixture.errors == 0


def test_trusted_launcher_authority_refusal_is_failure_not_a_skipped_run(monkeypatch):
    from types import SimpleNamespace
    driver = load('driver')
    monkeypatch.setattr(driver.os, 'getuid', lambda: 1004)
    monkeypatch.setattr(driver.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=1003))
    plan = {'account': 'alex-agent', 'repo': '/example/repo', 'root': '/example/work',
            'policy_id': 'qualification-phase1', 'image': 'sha256:' + 'a' * 64}
    report = driver.qualify(plan)
    assert report['checks']['trusted_launcher']['status'] == 'fail'
    assert 'control account' in report['checks']['trusted_launcher']['reason']
    assert report['go_phase_1'] is False


def test_missing_protected_policy_is_a_failed_launcher_precondition(tmp_path, monkeypatch):
    from types import SimpleNamespace
    driver = load('driver')
    monkeypatch.setattr(driver.os, 'getuid', lambda: 1003)
    monkeypatch.setattr(driver.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=1003))
    original = driver.Path
    monkeypatch.setattr(driver, 'Path', lambda path: tmp_path / 'missing.json'
        if path == '/etc/acc/containers/policy.json' else original(path))
    plan = {'account': 'alex-agent', 'repo': '/example/repo', 'root': '/example/work',
            'policy_id': 'qualification-phase1', 'image': 'sha256:' + 'a' * 64}
    report = driver.qualify(plan)
    assert report['checks']['trusted_launcher']['status'] == 'fail'
    assert 'FileNotFoundError' in report['checks']['trusted_launcher']['reason']


def test_option_a_bwrap_is_not_applicable_only_with_verified_contract(monkeypatch):
    q = load('phase1')
    monkeypatch.setattr(q, 'container_mode', lambda: True, raising=False)
    monkeypatch.setattr(q, 'run', lambda *a, **k: pytest.fail('Option A does not run nested bubblewrap'))
    check = q.bundled_bwrap()
    assert check['status'] == 'pass'
    assert check['reason'] == 'bubblewrap: not applicable (container is the sandbox)'


def test_bwrap_is_never_not_applicable_outside_container_mode(monkeypatch):
    """Review of phase1-in-container (question 4): without a verified contract the row runs the real nested probe, and a
    probe that doesn't isolate fails; the not-applicable pass is reachable only through container_mode()."""
    from types import SimpleNamespace

    q = load('phase1')
    monkeypatch.setattr(q, 'container_mode', lambda: False, raising=False)
    monkeypatch.setattr(q, 'Path', lambda value: SimpleNamespace(glob=lambda _: [Path('/example/bwrap')],
                                                                 exists=lambda: False))
    monkeypatch.setattr(q.os, 'readlink', lambda _: 'user:[1]')
    calls = []

    def run(command, **kwargs):
        calls.append(command[0])
        return 1, "bwrap: Can't mount proc on /newroot/proc: Operation not permitted"
    monkeypatch.setattr(q, 'run', run)
    check = q.bundled_bwrap()
    assert calls == ['/example/bwrap']
    assert check['status'] == 'fail' and 'not applicable' not in check['reason']


def test_native_chrome_still_requires_its_own_sandbox():
    q = load('phase1')
    status = 'NoNewPrivs:\t1\nSeccomp:\t2\n'
    assert q.renderer_evidence(status, b'--type=renderer', isolated_pid=True, container=False)
    assert not q.renderer_evidence(status, b'--type=renderer --no-sandbox', isolated_pid=True, container=False)
    assert not q.renderer_evidence(status, b'--type=renderer', isolated_pid=False, container=False)
    assert q.renderer_evidence(status, b'--type=renderer --no-sandbox', isolated_pid=False, container=True)


def test_container_boundary_refuses_unverified_context_without_host_writes(monkeypatch):
    q = load('phase1')
    monkeypatch.setattr(q, 'container_mode', lambda: False, raising=False)
    monkeypatch.setattr(q, 'Path', lambda *a: pytest.fail('must not probe host paths without contract'))
    assert q.container_boundary()['status'] == 'fail'


@pytest.mark.parametrize('provider, name, needle', [
    ('codex', '.codex/config.toml', 'hooks = false'),
    ('claude', '.claude/settings.json', '"disableAllHooks": true'),
    ('claude', '.claude/settings.local.json', '"disableAllHooks": true'),
    ('copilot', '.copilot/settings.json', '"disableAllHooks": true'),
])
def test_ct01_provider_probe_runs_in_a_repository_that_tries_to_disable_hooks(tmp_path, monkeypatch, provider, name,
                                                                                needle):
    q = load('phase1')
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/container_qualification'))
    seen = {}
    real_path = q.Path

    def paths(value):
        value = str(value)
        markers = ('/tmp/acc-qualification-forbidden', '/tmp/acc-qualification-allowed', '/tmp/acc-state/')
        return tmp_path / real_path(value).name if value.startswith(markers) else real_path(value)
    monkeypatch.setattr(q, 'Path', paths)

    def cli(command, **kwargs):
        workspace = real_path(kwargs['cwd'])
        if (workspace / name).exists():  # Copilot's refusal runs use other repositories
            seen['text'] = (workspace / name).read_text()
            seen['git'] = (workspace / '.git').is_dir()
        return 1, ''
    monkeypatch.setattr(q, 'run', cli)
    (tmp_path / 'events.jsonl').touch()
    assert q.provider_probe(provider, tmp_path)['status'] == 'fail'
    assert needle in seen['text'] and seen['git']


def fake_proc(root, processes):
    for pid, value in processes.items():
        uid, cgroup, parent = (*value, 1) if len(value) == 2 else value
        folder = root / str(pid)
        folder.mkdir(parents=True)
        (folder / 'status').write_text(f'Name:\tx\nPPid:\t{parent}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
        (folder / 'cgroup').write_text(f'0::{cgroup}\n')
    (root / 'self').mkdir(exist_ok=True)


def test_ct03_census_counts_only_account_processes_outside_the_unit_and_broker(tmp_path):
    driver = load('driver')
    unit = '/system.slice/acc-containers-1004.slice/acc-ct-x-g1.service'
    fake_proc(tmp_path, {
        10: (1004, unit + '/libpod-payload'),                 # the container payload
        11: (1004, unit),                                     # conmon
        12: (1004, '/system.slice/acc-broker@alex-agent.service'),  # the broker MainPID
        13: (1004, '/user.slice/user-1004.slice/user@1004.service/init.scope'),  # its user manager
        14: (1004, '/system.slice/acc-broker@alex-agent.service'),  # a native session under the broker
        15: (1004, '/user.slice/user-1004.slice/user@1004.service/app.slice/timer.service'),
        16: (1005, '/system.slice/other.service'),            # another account: not this payload's writer
        17: (1004, unit + '-sibling'),                        # prefix match is not containment
    })
    assert [pid for pid, _, _ in driver.foreign_processes(1004, unit, 12, proc=tmp_path)] == [14, 15, 17]
    assert driver.foreign_processes(1004, unit, 99, proc=tmp_path) is None  # broker hidden (hidepid): fail closed


BROKER = '/system.slice/system-acc\\x2dbroker.slice/acc-broker@alex-agent.service'
MANAGER = '/user.slice/user-1004.slice/user@1004.service'


def test_census_names_each_foreign_process_with_a_fixed_label_and_escaped_cgroup(tmp_path):
    """Attempt 13 failed with only a count (3); the owner had to take a census by hand."""
    driver = load('driver')
    unit = '/system.slice/acc-ct-x-g1.service'
    fake_proc(tmp_path, {
        12: (1004, BROKER),
        20: (1004, MANAGER + '/user.slice/podman-pause-c93de532.scope'),
        21: (1004, MANAGER + '/session.slice/dbus.service'),
        22: (1004, BROKER),
        23: (1004, MANAGER + '/app.slice/evil\x1b[2J;$(id)\u202e.service'),
        24: (1004, '/user.slice/user-1004.slice/session-3.scope'),
    })
    foreign = driver.foreign_processes(1004, unit, 12, proc=tmp_path)
    assert [(pid, where) for pid, where, _ in foreign] == [
        (20, 'Podman pause scope'), (21, 'session bus'), (22, 'broker cgroup'), (23, 'user manager unit'),
        (24, 'login session')]
    assert foreign[0][2] == MANAGER + '/user.slice/podman-pause-c93de532.scope'
    assert foreign[2][2] == BROKER.replace('\\', '\\x5c')  # a backslash is escaped too, systemd's own \\x2d included
    odd = foreign[3][2]
    assert '\x1b' not in odd and '$(' not in odd and '\u202e' not in odd and ';' not in odd
    assert odd.endswith('evil\\x1b\\x5b2J\\x3b\\x24\\x28id\\x29\\xe2\\x80\\xae.service')
    assert len(driver.shown('/' + 'a' * 500)) == 163
    # A name that spells an escape can't pass for one (census review L-3).
    assert driver.shown('/evil\\x1b.service') == '/evil\\x5cx1b.service'
    text = driver.describe(foreign)
    assert text.startswith('PID 20 Podman pause scope /user.slice/') and 'PID 22 broker cgroup' in text
    assert driver.describe([(n, 'other', '/x') for n in range(10)]).endswith('; and 2 more')


def test_census_attributes_only_the_brokers_tmux_server_children_and_pane_after_a_strict_start(tmp_path):
    """Attempt 13: the tmux server the broker started for the session counted as a possible forger."""
    driver = load('driver')
    unit = '/system.slice/acc-ct-x-g1.service'
    processes = {
        12: (1004, BROKER, 1),          # the broker
        30: (1004, BROKER, 1),          # its tmux server (daemonized)
        31: (1004, BROKER, 30),         # the session's pane: the fixed attach command
        32: (1004, BROKER, 31),         # something the attach command started
        33: (1004, BROKER, 12),         # a tmux client the broker runs for accd
        40: (1004, unit, 1),            # the unit
    }
    fake_proc(tmp_path, processes)
    assert driver.foreign_processes(1004, unit, 12, pane=31, proc=tmp_path) == []
    # Before the launch nothing in the broker's cgroup is attributed: a server there may hold planted hooks.
    assert [pid for pid, _, _ in driver.foreign_processes(1004, unit, 12, proc=tmp_path)] == [30, 31, 32, 33]
    # Another pane or tmux job (a child of the server), a respawned pane (new PID) and an unknown parent: foreign.
    fake_proc(tmp_path, {34: (1004, BROKER, 30), 35: (1004, BROKER, 30)})
    (tmp_path / '36').mkdir()
    (tmp_path / '36' / 'status').write_text('Uid:\t1004\t1004\t1004\t1004\n')
    (tmp_path / '36' / 'cgroup').write_text(f'0::{BROKER}\n')
    assert [pid for pid, _, _ in driver.foreign_processes(1004, unit, 12, pane=31, proc=tmp_path)] == [34, 35, 36]


def test_census_trusts_no_tmux_server_once_a_second_process_detaches_in_the_brokers_cgroup(tmp_path):
    driver = load('driver')
    unit = '/system.slice/acc-ct-x-g1.service'
    fake_proc(tmp_path, {12: (1004, BROKER, 1), 30: (1004, BROKER, 1), 31: (1004, BROKER, 30),
                         50: (1004, BROKER, 1)})  # e.g. a pane's `setsid -f` helper, or Podman's orphaned pause
    foreign = driver.foreign_processes(1004, unit, 12, pane=31, proc=tmp_path)
    assert [pid for pid, _, _ in foreign] == [30, 50]
    assert {where for _, where, _ in foreign} == {'broker cgroup'}


def test_ct03_foreign_account_process_during_qualification_is_no_go(tmp_path, monkeypatch, as_agent_account):
    import base64
    import json
    from types import SimpleNamespace

    driver = load('driver')
    uid = driver.os.getuid()
    monkeypatch.setattr(driver.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=uid))
    monkeypatch.setattr(driver, 'policy_matches', lambda _: True)
    monkeypatch.setattr(driver, 'host_image_evidence', lambda _: True)
    monkeypatch.setattr(driver, 'launcher_host', lambda **_: [])
    monkeypatch.setattr(driver, 'launcher_container_uid', lambda: LAUNCHER_UID)
    monkeypatch.setattr(driver.time, 'sleep', lambda _: None)
    monkeypatch.setattr(driver, 'cgroup_evidence', lambda _: (True, '/example'))
    monkeypatch.setattr(driver, 'broker_pid', lambda _: 12)
    seen = []

    def census(uid, unit, broker, *, pane=None):
        seen.append(pane)
        return [] if pane is None else [(768520, 'Podman pause scope', '/user.slice/user-1.slice/user@1.service/'
                                         'user.slice/podman-pause-c93de532.scope')]
    monkeypatch.setattr(driver, 'foreign_processes', census)
    folder = tmp_path / 'grants'
    folder.mkdir(mode=0o750)
    if folder.stat().st_uid != as_agent_account:
        os.chown(folder, as_agent_account, -1)  # root-run suite: the control account owns its grants folder
    stops = []

    def call(account, method, params):
        if method == 'ping':
            return {'role': 'agents', 'user': account, 'run_dir': '/example/run'}
        if method == 'make_private_checkout':
            return {'path': '/example/work/.worktrees/qualification'}
        if method == 'launch':
            generation = params['container']['generation']
            return {'pid': 4242, 'container': {'unit': f'acc-ct-{params["acc_id"]}-g{generation}.service',
                                               'state': 'running'}}
        if method == 'read_artifact':
            grant = json.loads(next(folder.glob('*.json')).read_text())
            data = json.dumps({'version': 1, 'go_phase_1': False, 'nonce': grant['args'][1], 'uid': uid,
                               'no_new_privs': '1', 'checks': {name: {'status': 'pass', 'reason': 'forged'}
                                                               for name in driver.MANDATORY}}).encode()
            return {'size': len(data), 'data': base64.b64encode(data).decode()}
        if method == 'kill':
            stops.append(params['container']['generation'])
            return {'container': {'stopped': True}}
        pytest.fail(method)
    plan = {'account': 'alex-agent', 'repo': '/example/repo', 'root': '/example/work',
            'policy_id': 'qualification-phase1', 'image': 'sha256:' + 'a' * 64}
    report = driver.qualify(plan, grants=folder, call=call)
    assert report['go_phase_1'] is False
    assert report['checks']['trusted_launcher']['status'] == 'fail'
    reason = report['checks']['trusted_launcher']['reason']
    assert 'other processes' in reason
    assert '(1: PID 768520 Podman pause scope /user.slice/user-1.slice/user@1.service/user.slice/podman-pause-c93de532.scope)' in reason
    assert report['foreign_processes'] == [{'pid': 768520, 'where': 'Podman pause scope',
                                            'cgroup': '/user.slice/user-1.slice/user@1.service/user.slice/podman-pause-c93de532.scope'}]
    assert seen[0] is None and seen[1] == 4242  # strict before launch, then with the pane the broker reported
    assert stops and not list(folder.iterdir())


def test_ct03_a_busy_account_before_launch_is_named_and_never_launched(tmp_path, monkeypatch, as_agent_account):
    from types import SimpleNamespace

    driver = load('driver')
    monkeypatch.setattr(driver.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=driver.os.getuid()))
    monkeypatch.setattr(driver, 'policy_matches', lambda _: True)
    monkeypatch.setattr(driver, 'host_image_evidence', lambda _: True)
    monkeypatch.setattr(driver, 'broker_pid', lambda _: 12)
    # Not this host's units: their systemctl call waits for its child with time.sleep, which the census count below
    # would take in whenever systemctl closes its output before it exits (about 1 in 3 on a loaded gate host).
    monkeypatch.setattr(driver, 'launcher_host', lambda **_: [])
    monkeypatch.setattr(driver, 'launcher_container_uid', lambda: LAUNCHER_UID)
    sleeps = []
    monkeypatch.setattr(driver.time, 'sleep', sleeps.append)
    monkeypatch.setattr(driver, 'foreign_processes',
                        lambda *a, **k: [(770337, 'broker cgroup', BROKER)])
    methods = []

    def call(account, method, params):
        methods.append(method)
        return {'role': 'agents', 'user': account, 'run_dir': '/example/run'}
    plan = {'account': 'alex-agent', 'repo': '/example/repo', 'root': '/example/work',
            'policy_id': 'qualification-phase1', 'image': 'sha256:' + 'a' * 64}
    report = driver.qualify(plan, grants=tmp_path, call=call)
    reason = report['checks']['trusted_launcher']['reason']
    assert report['go_phase_1'] is False and report['checks']['trusted_launcher']['status'] == 'fail'
    assert reason.startswith('the account was not quiet before the launch (1: PID 770337 broker cgroup ')
    assert "the broker's tmux server" in reason
    assert methods == ['ping'] and sleeps == [1] * 5  # retried, then refused before any checkout or launch


def test_ct02_qualification_inspects_image_modes_behind_the_readonly_mount(tmp_path):
    q = load('phase1')
    tree = tmp_path / 'opt'
    (tree / 'bin').mkdir(parents=True)
    (tree / 'bin/tool').write_text('x')
    tree.chmod(0o755)
    (tree / 'bin').chmod(0o755)
    (tree / 'bin/tool').chmod(0o755)
    assert q.image_modes([str(tree)])['status'] == 'pass'
    (tree / 'bin/tool').chmod(0o777)
    assert q.image_modes([str(tree)])['status'] == 'fail'
    (tree / 'bin/tool').chmod(0o4755)
    assert q.image_modes([str(tree)])['status'] == 'fail'
    (tree / 'bin/tool').chmod(0o755)
    (tree / 'bin').chmod(0o775)
    assert q.image_modes([str(tree)])['status'] == 'fail'


def test_ct05_host_probe_detects_writable_image(tmp_path):
    probe = load('image_store')
    guard = tmp_path / 'acc-guard'
    guard.write_text('reviewed')
    assert not probe.denied([str(guard)])
    assert guard.read_text() == 'reviewed'


@pytest.mark.parametrize('target,check', [('self', 'namespace_access'), ('missing', 'namespace_service_exited')])
def test_ct05_namespace_proof_rejects_accessible_or_missing_process(monkeypatch, target, check, as_agent_account):
    # An exited namespace service is its own failure, never reported as "the host can access the namespace".
    import os

    probe = load('image_store')
    monkeypatch.setattr(probe.subprocess, 'run', lambda *args, **kwargs: pytest.fail('must refuse before Podman'))
    pid = os.getpid() if target == 'self' else 2147483647
    with pytest.raises(probe.CheckFailed) as failure:
        probe.probe('sha256:' + 'a' * 64, pid)
    assert failure.value.check == check


def test_ct05_namespace_service_exiting_during_the_check_is_named(monkeypatch, as_agent_account):
    probe = load('image_store')
    monkeypatch.setattr(probe.subprocess, 'run', lambda *args, **kwargs: pytest.fail('must refuse before Podman'))
    states = iter([True, False])  # alive before the access check, gone after it
    monkeypatch.setattr(probe, 'alive', lambda pid: next(states))
    with pytest.raises(probe.CheckFailed) as failure:
        probe.probe('sha256:' + 'a' * 64, 2147483647)
    assert failure.value.check == 'namespace_service_exited'


def test_ct05_probe_prints_one_line_naming_the_failed_check(monkeypatch, capsys):
    probe = load('image_store')
    def failing(image, pid):
        raise probe.CheckFailed('unshare_write', 'unshare write denial not demonstrated (exit 1)')
    monkeypatch.setattr(probe, 'probe', failing)
    with pytest.raises(SystemExit) as code:
        probe.main(['sha256:' + 'a' * 64, '1234'])
    assert code.value.code == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert err == ['image_store: FAILED unshare_write: unshare write denial not demonstrated (exit 1)']


@pytest.mark.parametrize('unshare_rc,unshare_output,readonly,run_fails,mismatch', [
    (0, 'denied', True, False, None), (0, 'allowed', True, False, None), (125, '', True, False, None),
    (0, 'denied', False, False, None), (0, 'denied', True, True, None),
    (0, 'denied', True, False, 'container_root'), (0, 'denied', True, False, 'store_path'),
    (0, 'denied', True, False, 'unreadable'),
])
def test_ct05_host_probe_checks_runtime_and_unshare_then_cleans_only_its_container(
        tmp_path, monkeypatch, unshare_rc, unshare_output, readonly, run_fails, mismatch, as_agent_account):
    import json
    import os
    import subprocess

    probe = load('image_store')
    image = 'sha256:' + 'a' * 64
    rootfs = tmp_path / 'rootfs' / image[7:]
    rootfs.mkdir(parents=True)
    (rootfs / '.acc-image-id').write_text(image + '\n')
    (rootfs / 'acc-guard').write_text('reviewed')
    real_lstat, real_fstat, real_stat = Path.lstat, os.fstat, os.stat
    root_info = real_stat(rootfs)
    commands = []
    source = None

    def owned(info):
        values = list(info)
        values[4] = 0
        values[0] &= ~0o022
        return os.stat_result(values)

    def run(argv, **kwargs):
        nonlocal source
        commands.append(argv)
        if argv[1] == 'run':
            source = argv[argv.index('--entrypoint=/bin/sleep') + 1]
            if run_fails:
                raise subprocess.TimeoutExpired(argv, 90)
        if argv[1:3] == ['container', 'inspect']:
            output = json.dumps([{'Rootfs': source, 'State': {'Pid': 123456789},
                                  'HostConfig': {'ReadonlyRootfs': readonly}}])
        elif argv[1] == 'unshare':
            if argv[-1] == '--attempt-read':
                return subprocess.CompletedProcess(argv, 0, 'denied', '')
            return subprocess.CompletedProcess(argv, unshare_rc, unshare_output, '')
        else:
            output = ''
        return subprocess.CompletedProcess(argv, 0, output, '')

    monkeypatch.setattr(probe, 'STORE', tmp_path)
    storage(tmp_path, images=[{'id': image[7:], 'layer': 'f' * 64}], layers=[{'id': 'f' * 64}])
    monkeypatch.setattr(probe, 'account_store', lambda _: tmp_path / 'clean-account-store')
    monkeypatch.setattr(Path, 'lstat', lambda path, **kwargs: owned(real_lstat(path, **kwargs)))
    monkeypatch.setattr(probe.os, 'fstat', lambda fd: owned(real_fstat(fd)))
    other = real_stat(tmp_path)  # any other directory: a swapped or substituted root

    def stat(path, *args, **kwargs):
        if str(path) == '/proc/123456789/root':
            if mismatch == 'unreadable':
                raise PermissionError(13, 'Permission denied', str(path))
            return other if mismatch == 'container_root' else root_info
        if str(path) == str(rootfs) and mismatch == 'store_path' and source is not None:
            return other  # the store path resolves elsewhere once the container runs
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(probe.os, 'stat', stat)
    monkeypatch.setattr(probe, 'denied', lambda paths, **kwargs: bool(paths))
    monkeypatch.setattr(probe, 'alive', lambda pid: True)  # the owner's namespace service is running
    monkeypatch.setattr(probe.subprocess, 'run', run)
    if unshare_rc == 0 and unshare_output == 'denied' and readonly and not run_fails and not mismatch:
        assert probe.probe(image, 123456789)['execution_source'] == 'held-rootfs'
        # What the launcher does (a bind of the held root): the protected store path, never another process's
        # /proc/PID/fd, which rootless Podman can't open from inside its user namespace.
        assert source == str(rootfs) and '/proc/' not in source
    else:
        with pytest.raises(subprocess.TimeoutExpired if run_fails else ValueError):
            probe.probe(image, 123456789)
    launched = next(arg.split('=', 1)[1] for argv in commands if argv[1] == 'run'
                    for arg in argv if arg.startswith('--name='))
    assert commands[-1] == ['/usr/bin/podman', 'rm', '--force', '--ignore', launched]


def test_ct05_missing_host_evidence_cannot_go(tmp_path):
    driver = load('driver')
    assert not driver.host_image_evidence({'account': 'alex-agent', 'image': 'sha256:' + 'a' * 64}, folder=tmp_path)


@pytest.mark.parametrize('field,value', [('time', 0), ('image', 'sha256:' + 'b' * 64),
    ('direct_write', 'allowed'), ('unshare_write', 'unavailable'), ('runtime_layers', 'copied'), ('files', 0),
    ('execution_source', 'image-lookup'), ('namespace_boundary', 'unproven')])
def test_ct05_host_proof_rejects_stale_wrong_or_incomplete_evidence(tmp_path, monkeypatch, field, value):
    import json
    import os
    import time

    driver = load('driver')
    proof = {'account': 'alex-agent', 'image': 'sha256:' + 'a' * 64, 'time': time.time(),
             'status': 'pass', 'direct_write': 'denied', 'unshare_write': 'denied',
             'runtime_layers': 'protected', 'execution_source': 'held-rootfs',
             'namespace_boundary': 'denied', 'files': 3}
    path = tmp_path / 'alex-agent.json'
    path.write_text(json.dumps(proof))
    # Model root ownership while retaining real file reads and protected mode bits.
    real_fstat = os.fstat
    real_lstat = Path.lstat

    def owned(info, *, directory=False):
        fields = list(info)
        fields[4] = 0
        if directory:
            fields[0] = 0o40755
        return os.stat_result(fields)

    monkeypatch.setattr(driver.os, 'fstat', lambda fd: owned(real_fstat(fd)))
    monkeypatch.setattr(Path, 'lstat', lambda self, **kwargs: owned(real_lstat(self, **kwargs), directory=True))
    plan = {'account': 'alex-agent', 'image': 'sha256:' + 'a' * 64}
    assert driver.host_image_evidence(plan, folder=tmp_path)
    proof[field] = value
    path.write_text(json.dumps(proof))
    assert not driver.host_image_evidence(plan, folder=tmp_path)


def test_ct05_failing_podman_unshare_is_rerun_once_with_debug_log_and_runs_from_root(monkeypatch, capsys, as_agent_account):
    # Podman prints only "Error: setting up the process"; the reason is on earlier lines or in its debug log.
    import subprocess

    probe = load('image_store')
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if '--log-level=debug' in argv:
            lines = '\n'.join(f'time="t" level=debug msg="step {n}\x1b[31m\x07"' for n in range(50))
            return subprocess.CompletedProcess(argv, 125, '', lines + '\nError: setting up the process\n')
        return subprocess.CompletedProcess(argv, 125, '', 'cannot chdir to /root: Permission denied\n'
                                                          'Error: setting up the process\n')
    monkeypatch.setattr(probe.subprocess, 'run', run)
    monkeypatch.setattr(probe, 'alive', lambda pid: True)
    monkeypatch.setattr(probe, 'denied', lambda paths, **kwargs: bool(paths))
    with pytest.raises(probe.CheckFailed) as failure:
        probe.probe('sha256:' + 'a' * 64, 4321)
    assert failure.value.check == 'namespace_unshare'
    assert [argv[1] for argv, _ in calls] == ['unshare', '--log-level=debug']  # exactly one debug rerun
    assert all(kwargs.get('cwd') == '/' for _, kwargs in calls)
    log = failure.value.log
    assert 'podman: cannot chdir to /root: Permission denied' in log
    debug = [line for line in log if line.startswith('podman-debug: ')]
    assert len(debug) == 30 and debug[-1] == 'podman-debug: Error: setting up the process'
    assert not any('\x1b' in line or '\x07' in line or len(line) > 320 for line in log)
    monkeypatch.setattr(probe, 'probe', lambda image, pid: (_ for _ in ()).throw(failure.value))
    with pytest.raises(SystemExit):
        probe.main(['sha256:' + 'a' * 64, '4321'])
    err = capsys.readouterr().err.strip().splitlines()
    assert err[-1].startswith('image_store: FAILED namespace_unshare:') and 'podman: cannot chdir' in err[0]


def test_ct05_the_checks_own_allowed_result_is_not_rerun(monkeypatch, as_agent_account):
    import subprocess

    probe = load('image_store')
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, '', 'allowed: an image or namespace path opened\n')
    monkeypatch.setattr(probe.subprocess, 'run', run)
    monkeypatch.setattr(probe, 'alive', lambda pid: True)
    monkeypatch.setattr(probe, 'denied', lambda paths, **kwargs: bool(paths))
    with pytest.raises(probe.CheckFailed):
        probe.probe('sha256:' + 'a' * 64, 4321)
    assert len(calls) == 1


@pytest.mark.parametrize("leftover", [False, True])
def test_a_failed_start_reports_cleanup_only_as_verified_as_the_launcher_says(tmp_path, monkeypatch, leftover,
                                                                              as_agent_account):
    """Attempt 10: the launch failed and the report said cleanup_verified: true while the pause process lived on.
    The launcher now fails that cleanup and names the process; the report must carry both."""
    from types import SimpleNamespace

    driver = load('driver')
    uid = driver.os.getuid()
    monkeypatch.setattr(driver.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=uid))
    monkeypatch.setattr(driver, 'policy_matches', lambda _: True)
    monkeypatch.setattr(driver, 'host_image_evidence', lambda _: True)
    monkeypatch.setattr(driver, 'launcher_host', lambda **_: [])
    monkeypatch.setattr(driver, 'launcher_container_uid', lambda: LAUNCHER_UID)
    monkeypatch.setattr(driver, 'broker_pid', lambda _: 12)
    monkeypatch.setattr(driver, 'foreign_processes', lambda *a, **k: [])  # the kit's strict census runs first
    folder = tmp_path / 'grants'
    folder.mkdir(mode=0o750)
    if folder.stat().st_uid != as_agent_account:
        os.chown(folder, as_agent_account, -1)
    detail = ('cleanup_failed: the container didn\'t stop cleanly; Rynhult retries, and won\'t relaunch it until it has '
              '(still running: 2951658 (catatonit))')

    def call(account, method, params):
        if method == 'ping':
            return {'role': 'agents', 'user': account, 'run_dir': '/example/run'}
        if method == 'make_private_checkout':
            return {'path': '/example/work/.worktrees/qualification'}
        if method == 'launch':
            raise RuntimeError('container launch refused: start_failed')
        if method == 'kill':
            return {'container': {'stopped': False, 'error': detail} if leftover else {'stopped': True}}
        pytest.fail(method)
    plan = {'account': 'alex-agent', 'repo': '/example/repo', 'root': '/example/work',
            'policy_id': 'qualification-phase1', 'image': 'sha256:' + 'a' * 64}
    report = driver.qualify(plan, grants=folder, call=call)
    assert report['cleanup_verified'] is (not leftover)
    if leftover:
        assert '2951658 (catatonit)' in report['cleanup_detail']
    else:
        assert 'cleanup_detail' not in report
    assert report['go_phase_1'] is False


def storage(root, images=(), layers=(), containers=()):
    """containers/storage metadata as Podman writes it: images name a top layer, layers name their parent."""
    import json
    for kind, rows in (('images', images), ('layers', layers), ('containers', containers)):
        folder = root / f'overlay-{kind}'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'{kind}.json').write_text(json.dumps(list(rows)))


PINNED = 'sha256:' + '7e' * 32
BASE, CHECK, TOP, OTHER, MAPPED, WORK = ('1' * 64, '2' * 64, '3' * 64, '4' * 64, '5' * 64, '6' * 64)


def shadowed_account(tmp_path):
    protected, home = tmp_path / 'protected', tmp_path / 'home'
    storage(protected, images=[{'id': PINNED[7:], 'layer': TOP}],
            layers=[{'id': BASE}, {'id': TOP, 'parent': BASE}])
    storage(home / '.local/share/containers/storage',
            images=[{'id': 'a' * 64, 'layer': BASE},       # the `checked` stage left by a build
                    {'id': 'b' * 64, 'layer': CHECK},      # built on it
                    {'id': 'c' * 64, 'layer': OTHER},      # the account's unrelated image
                    {'id': 'd' * 64, 'layer': MAPPED}],    # a keep-id copy: new layer, new ID
            layers=[{'id': BASE}, {'id': CHECK, 'parent': BASE}, {'id': OTHER}, {'id': MAPPED},
                    {'id': TOP, 'parent': BASE}, {'id': WORK, 'parent': BASE}],
            containers=[{'id': 'e' * 64, 'layer': WORK}, {'id': 'f' * 64, 'layer': OTHER}])
    return protected, home


def test_store_check_finds_only_the_pinned_image_layers_and_what_holds_them(tmp_path):
    """Qualification attempt 11: a build's intermediate stage left the image's first layer in the account's store."""
    probe = load('image_store')
    protected, home = shadowed_account(tmp_path)
    found = probe.shadowing(PINNED, home, protected)
    assert found['layers'] == sorted([BASE, TOP])
    assert found['images'] == ['b' * 64, 'a' * 64]  # the child first, so neither removal needs --force
    assert found['containers'] == ['e' * 64] and found['unreferenced'] == [TOP]
    commands = probe.cleanup_commands(found)
    assert commands[0].endswith('containers rm -- ' + 'e' * 64)
    assert commands[1].endswith('containers image rm -- ' + 'b' * 64 + ' ' + 'a' * 64)
    assert all('CONTAINERS_STORAGE_CONF=' + str(probe.ACCOUNT_PROFILE) in c and '--force' not in c for c in commands)
    text = probe.explain(PINNED, found)
    assert 'c' * 64 not in text and 'd' * 64 not in text and 'f' * 64 not in text  # never the account's other storage
    assert 'cannot remove a bare layer' in text and TOP in text
    profile = (ROOT / 'deploy/containers/account-store.conf').read_text()
    assert 'additionalimagestores' not in profile and '$HOME' not in profile and 'rootless_storage_path' not in profile
    storage(home / '.local/share/containers/storage', images=[{'id': 'c' * 64, 'layer': OTHER}], layers=[{'id': OTHER}])
    assert probe.shadowing(PINNED, home, protected)['layers'] == []
    storage(protected, images=[], layers=[])
    with pytest.raises(ValueError, match='not in the protected store'):
        probe.shadowing(PINNED, home, protected)


def test_host_probe_stops_with_the_cleanup_commands_before_running_podman(tmp_path, monkeypatch, as_agent_account):
    import os
    import subprocess

    probe = load('image_store')
    protected, home = shadowed_account(tmp_path)
    rootfs = protected / 'rootfs' / PINNED[7:]
    rootfs.mkdir(parents=True)
    real_lstat = Path.lstat

    def owned(info):
        values = list(info)
        values[4] = 0
        values[0] &= ~0o022
        return os.stat_result(values)

    def run(argv, **kwargs):
        if argv[1] == 'unshare':
            return subprocess.CompletedProcess(argv, 0, 'denied', '')
        pytest.fail('nothing may run before the store check passes: ' + ' '.join(argv))

    monkeypatch.setattr(probe, 'STORE', protected)
    monkeypatch.setattr(probe, 'account_store', lambda _: home / '.local/share/containers/storage')
    monkeypatch.setattr(Path, 'lstat', lambda path, **kwargs: owned(real_lstat(path, **kwargs)))
    monkeypatch.setattr(probe, 'denied', lambda paths, **kwargs: bool(paths))
    monkeypatch.setattr(probe.subprocess, 'run', run)
    monkeypatch.setattr(probe, 'alive', lambda pid: True)  # the kit checks the namespace service first
    with pytest.raises(ValueError) as refused:
        probe.probe(PINNED, 123456789)
    assert 'containers image rm -- ' + 'b' * 64 + ' ' + 'a' * 64 in str(refused.value)


def test_store_check_never_prints_account_controlled_ids_it_cannot_vouch_for(tmp_path):
    """The account writes its own store's metadata: an ID there could be a flag, a name, shell syntax or a fake line.
    Only full hex IDs are named, quoted after `--`; anything else is counted, never echoed."""
    import shlex

    probe = load('image_store')
    protected, home = shadowed_account(tmp_path)
    hostile = ['--force', '-a', 'alpine', '$(touch /tmp/owned)', 'a' * 64 + '\n  sudo rm -rf /', 'A' * 64, 'f' * 63]
    storage(home / '.local/share/containers/storage',
            images=[{'id': 'a' * 64, 'layer': BASE}, *({'id': name, 'layer': BASE} for name in hostile),
                    {'id': '9' * 64, 'layer': [BASE]}],   # unhashable metadata: no crash
            layers=[{'id': BASE}, {'id': WORK, 'parent': {'x': 1}}],
            containers=[{'id': '--all', 'layer': BASE}, {'id': 'e' * 64, 'layer': BASE}])
    found = probe.shadowing(PINNED, home, protected)
    assert found['images'] == ['a' * 64] and found['containers'] == ['e' * 64] and found['malformed'] == len(hostile) + 1
    commands = probe.cleanup_commands(found)
    assert [shlex.split(c)[-3:] for c in commands] == [['rm', '--', 'e' * 64], ['rm', '--', 'a' * 64]]
    assert all(shlex.split(c)[2] == 'CONTAINERS_STORAGE_CONF=' + str(probe.ACCOUNT_PROFILE) for c in commands)
    text = probe.explain(PINNED, found)
    assert not any(name in text for name in ('alpine', '$(', 'sudo', 'A' * 64, 'f' * 63, '--all'))  # flags: commands above
    assert '8 record(s)' in text and 'ask the owner' in text


def test_store_check_reports_an_account_image_that_reuses_the_pinned_id(tmp_path):
    """BS-5: same ID, different layers. Podman resolves the ID to the account's copy first, so the launcher refuses;
    the check must not say all is well."""
    import shlex

    probe = load('image_store')
    protected, home = shadowed_account(tmp_path)
    storage(home / '.local/share/containers/storage', images=[{'id': PINNED[7:], 'layer': OTHER}],
            layers=[{'id': OTHER}], containers=[{'id': 'e' * 64, 'layer': WORK, 'image': PINNED[7:]}])
    found = probe.shadowing(PINNED, home, protected)
    assert found['layers'] == [] and found['reused_id'] and probe.shadowed(found)
    assert found['images'] == [PINNED[7:]] and found['containers'] == ['e' * 64]
    assert [shlex.split(c)[-3:] for c in probe.cleanup_commands(found)] == [['rm', '--', 'e' * 64],
                                                                            ['rm', '--', PINNED[7:]]]
    assert 'has an image with the pinned ID' in probe.explain(PINNED, found)
    storage(home / '.local/share/containers/storage', images=[{'id': 'c' * 64, 'layer': OTHER}], layers=[{'id': OTHER}])
    assert not probe.shadowed(probe.shadowing(PINNED, home, protected))


def test_store_check_commands_name_the_checked_store_by_absolute_path(tmp_path, monkeypatch):
    """BS-6: never $HOME; the printed commands address the store that was checked, whatever the shell's HOME is."""
    import os
    import shlex

    probe = load('image_store')
    protected, home = shadowed_account(tmp_path)
    monkeypatch.setenv('HOME', '/somewhere/else')
    found = probe.shadowing(PINNED, home, protected)
    for command in probe.cleanup_commands(found):
        words = shlex.split(command)
        assert '$' not in command and '~' not in command
        assert words[words.index('--root') + 1] == str(home / '.local/share/containers/storage')
        assert words[words.index('--runroot') + 1] == f'/run/user/{os.getuid()}/containers'
        assert all(os.path.isabs(w.split('=', 1)[-1]) for w in words if w.startswith('CONTAINERS_STORAGE_CONF='))


def podman_database(store, run_root, tmp_dir):
    """The DBConfig row Podman 4.9 writes to <graphroot>/libpod/db.sql on first use, and validates on every start."""
    import sqlite3
    (store / 'libpod').mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store / 'libpod/db.sql') as db:
        db.execute('CREATE TABLE DBConfig(ID INTEGER PRIMARY KEY NOT NULL, SchemaVersion INTEGER NOT NULL, OS TEXT NOT '
                   'NULL, StaticDir TEXT NOT NULL, TmpDir TEXT NOT NULL, GraphRoot TEXT NOT NULL, RunRoot TEXT NOT NULL, '
                   'GraphDriver TEXT NOT NULL, VolumeDir TEXT NOT NULL, CHECK (ID IN (1)))')
        db.execute('INSERT INTO DBConfig VALUES (1, 1, "linux", ?, ?, ?, ?, "overlay", ?)',
                   (str(store / 'libpod'), tmp_dir, str(store), run_root, str(store / 'volumes')))


def test_store_check_commands_use_the_run_root_and_tmp_dir_podman_recorded(tmp_path):
    """Podman refuses to start when its run root or tmp dir differs from the ones its database recorded ("database
    run root ... does not match our run root"), so the cleanup commands use the recorded ones, not an assumed
    /run/user/UID."""
    import shlex

    probe = load('image_store')
    protected, home = shadowed_account(tmp_path)
    other = f'/run/user/{os.getuid()}/podman-elsewhere'  # recorded by an earlier Podman, not the default
    podman_database(home / '.local/share/containers/storage', other + '/containers', other + '/libpod/tmp')
    found = probe.shadowing(PINNED, home, protected)
    commands = probe.cleanup_commands(found)
    assert len(commands) == 2
    for command in commands:
        words = shlex.split(command)
        assert words[words.index('--runroot') + 1] == other + '/containers'
        assert words[words.index('--tmpdir') + 1] == other + '/libpod/tmp'
    assert "the run root and tmp dir Podman recorded" in probe.explain(PINNED, found)


@pytest.mark.parametrize('case', ['bolt', 'hostile', 'relative', 'no table', 'outside', 'dotdot', 'c1', 'bidi'])
def test_store_check_prints_no_commands_when_the_recorded_paths_cannot_be_used(tmp_path, case):
    """A BoltDB store (Podman before 4.8) can't be read here, and the account writes its own database: a recorded path
    that isn't a plain absolute one is never put in a command. The owner is told how to find the real values."""
    probe = load('image_store')
    protected, home = shadowed_account(tmp_path)
    store = home / '.local/share/containers/storage'
    if case == 'bolt':
        (store / 'libpod').mkdir(parents=True)
        (store / 'libpod/bolt_state.db').write_bytes(b'bolt')
    elif case == 'no table':
        (store / 'libpod').mkdir(parents=True)
        (store / 'libpod/db.sql').write_bytes(b'not a database')
    else:
        # Kit review M-1: the account writes these, and the owner may paste the commands in a root shell.
        uid = os.getuid()
        run_root = {'hostile': '/run/x\n  sudo rm -rf /', 'relative': 'run/containers',
                    'outside': '/etc/cron.d',  # root Podman would write its run state there
                    'dotdot': f'/run/user/{uid}/../../../etc',
                    'c1': f'/run/user/{uid}/\x9b2J',  # CSI to an 8-bit terminal: rewrites what the owner reads
                    'bidi': f'/run/user/{uid}/\u202econtainers'}[case]  # displays reversed
        podman_database(store, run_root, f'/run/user/{uid}/libpod/tmp')
    found = probe.shadowing(PINNED, home, protected)
    assert probe.shadowed(found) and probe.cleanup_commands(found) == []
    text = probe.explain(PINNED, found)
    assert 'sudo' not in text and 'run podman info as this account' in text
    assert 'b' * 64 in text and 'a' * 64 in text  # what to remove is still named


# ---- attempt 14: host settings the launcher needs, checked before any grant ----------------------------------------

def systemd_show(values):
    return lambda unit: values.get(unit, {'LoadState': 'not-found', 'TasksMax': '4915'})


READY = {'acc-containers@qualification.service': {'LoadState': 'loaded', 'TasksMax': '256'},
         'acc-launcher.slice': {'LoadState': 'loaded', 'TasksMax': '4352'}}


def test_launcher_host_passes_a_missing_attach_root_and_the_reviewed_limits(tmp_path):
    driver = load('driver')
    assert driver.launcher_host(attach_root=tmp_path / 'missing', owner=os.getuid(), show=systemd_show(READY)) == []
    root = tmp_path / 'attach'
    root.mkdir()
    root.chmod(0o755)
    assert driver.launcher_host(attach_root=root, owner=os.getuid(), show=systemd_show(READY)) == []


@pytest.mark.parametrize('mode', [0o700, 0o775, 0o777])
def test_launcher_host_names_an_attach_root_the_account_cannot_reach(tmp_path, mode):
    """Attempt 14: an earlier launcher left /run/acc-ct-attach root's 0700; the guard got EACCES below it."""
    driver = load('driver')
    root = tmp_path / 'attach'
    root.mkdir()
    root.chmod(mode)
    problems = driver.launcher_host(attach_root=root, owner=os.getuid(), show=systemd_show(READY))
    assert len(problems) == 1 and f'{mode:04o}' in problems[0] and 'chmod 0755' in problems[0]


def test_launcher_host_refuses_a_linked_or_foreign_attach_root(tmp_path):
    driver = load('driver')
    target = tmp_path / 'target'
    target.mkdir()
    (tmp_path / 'link').symlink_to(target)
    problems = driver.launcher_host(attach_root=tmp_path / 'link', owner=os.getuid(), show=systemd_show(READY))
    assert len(problems) == 1 and 'not a folder' in problems[0]
    target.chmod(0o755)
    problems = driver.launcher_host(attach_root=target, owner=os.getuid() + 1, show=systemd_show(READY))
    assert len(problems) == 1 and 'not root' in problems[0]


@pytest.mark.parametrize('unit,value', [('acc-containers@qualification.service', '32'),
                                        ('acc-launcher.slice', '128'),
                                        ('acc-launcher.slice', '1024'),  # review L-1: below 16 x 256 + 32
                                        ('acc-containers@qualification.service', 'garbage')])
def test_launcher_host_names_a_task_limit_below_the_bound(tmp_path, unit, value):
    """Attempt 14: TasksMax=32 rejected a fork while Podman ran as the account in the launcher's unit."""
    driver = load('driver')
    values = {name: dict(props) for name, props in READY.items()}
    values[unit]['TasksMax'] = value
    problems = driver.launcher_host(attach_root=tmp_path / 'missing', owner=os.getuid(), show=systemd_show(values))
    bound = '4128' if unit == 'acc-launcher.slice' else '256'
    assert len(problems) == 1 and unit in problems[0] and bound in problems[0]


def test_launcher_host_accepts_no_task_limit_and_refuses_a_missing_unit(tmp_path):
    driver = load('driver')
    values = {name: dict(props) for name, props in READY.items()}
    values['acc-launcher.slice']['TasksMax'] = 'infinity'
    assert driver.launcher_host(attach_root=tmp_path / 'missing', owner=os.getuid(), show=systemd_show(values)) == []
    del values['acc-launcher.slice']
    problems = driver.launcher_host(attach_root=tmp_path / 'missing', owner=os.getuid(), show=systemd_show(values))
    assert len(problems) == 1 and 'acc-launcher.slice' in problems[0] and 'not installed' in problems[0]


def test_launcher_bound_matches_the_installed_unit():
    driver = load('driver')
    unit = (ROOT / 'deploy/containers/acc-containers@.service').read_text().splitlines()
    assert f'TasksMax={driver.LAUNCHER_TASKS}' in unit
    setting = lambda name, key: int(next(line.split('=', 1)[1] for line in (ROOT / 'deploy/containers' / name)
                                         .read_text().splitlines() if line.startswith(key + '=')))
    assert driver.SLICE_TASKS == (setting('acc-containers.socket', 'MaxConnections') * driver.LAUNCHER_TASKS
                                  + setting('acc-containers-reconcile.service', 'TasksMax'))


def test_qualification_stops_before_any_grant_when_the_launcher_host_differs(tmp_path, monkeypatch, as_agent_account):
    from types import SimpleNamespace
    driver = load('driver')
    uid = driver.os.getuid()
    monkeypatch.setattr(driver.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=uid))
    monkeypatch.setattr(driver, 'policy_matches', lambda _: True)
    monkeypatch.setattr(driver, 'host_image_evidence', lambda _: True)
    monkeypatch.setattr(driver, 'launcher_host', lambda **_: ['/run/acc-ct-attach is 0700, not 0755: ...'])
    monkeypatch.setattr(driver, 'launcher_container_uid', lambda: LAUNCHER_UID)
    folder = tmp_path / 'grants'
    folder.mkdir(mode=0o750)
    plan = {'account': 'alex-agent', 'repo': '/example/repo', 'root': '/example/work',
            'policy_id': 'qualification-phase1', 'image': 'sha256:' + 'a' * 64}
    report = driver.qualify(plan, grants=folder, call=lambda *a: pytest.fail('no broker call before the host is ready'))
    assert report['go_phase_1'] is False and report['launcher_host'] == ['/run/acc-ct-attach is 0700, not 0755: ...']
    assert report['checks']['trusted_launcher']['status'] == 'fail'
    assert '/run/acc-ct-attach is 0700' in report['reason']
    assert not list(folder.iterdir())


# ---- attempt 15: the payload's identity is the container uid the launcher maps for the account ----------------------

def test_the_driver_takes_the_container_uid_from_the_launcher_and_keeps_no_copy(tmp_path):
    """L-2 (independent review): the payload's uid is written once, as the launcher's CONTAINER_UID, which builds its
    keep-id and --user flags; the driver reads it from the installed, root-owned launcher."""
    driver = load('driver')
    assert not hasattr(driver, 'CONTAINER_UID')
    source = (ROOT / 'scripts/container_qualification/driver.py').read_text()
    assert not re.search(r'^\s*CONTAINER_UID\s*=', source, re.MULTILINE)
    source = (ROOT / 'deploy/containers/acc-containers').read_text()
    assert source.count(f'CONTAINER_UID = {LAUNCHER_UID}\n') == 1
    launcher = tmp_path / 'acc-containers'
    launcher.write_text(source)
    launcher.chmod(0o755)
    assert driver.launcher_container_uid(launcher, owner=os.getuid()) == LAUNCHER_UID
    launcher.write_text(source.replace(f'CONTAINER_UID = {LAUNCHER_UID}\n', 'CONTAINER_UID = 2345\n'))
    assert driver.launcher_container_uid(launcher, owner=os.getuid()) == 2345


@pytest.mark.parametrize('case', ['missing-line', 'two-lines', 'zero', 'group-writable', 'foreign-owner', 'link'])
def test_the_driver_refuses_a_launcher_without_one_protected_container_uid(tmp_path, case):
    driver = load('driver')
    launcher = tmp_path / 'acc-containers'
    text = {'missing-line': 'CONTAINER_UIDS = 1000\n', 'two-lines': 'CONTAINER_UID = 1000\nCONTAINER_UID = 1001\n',
            'zero': 'CONTAINER_UID = 0\n'}.get(case, 'CONTAINER_UID = 1000\n')
    launcher.write_text('#!/usr/bin/python3\n' + text)
    launcher.chmod(0o775 if case == 'group-writable' else 0o755)
    owner = os.getuid() + 1 if case == 'foreign-owner' else os.getuid()
    if case == 'link':
        (tmp_path / 'link').symlink_to(launcher)
        launcher = tmp_path / 'link'
    with pytest.raises((OSError, ValueError)):
        driver.launcher_container_uid(launcher, owner=owner)


def test_qualification_stops_before_any_grant_when_the_launcher_declares_no_container_uid(tmp_path, monkeypatch,
                                                                                        as_agent_account):
    from types import SimpleNamespace
    driver = load('driver')
    uid = driver.os.getuid()
    monkeypatch.setattr(driver.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=uid))
    monkeypatch.setattr(driver, 'policy_matches', lambda _: True)
    monkeypatch.setattr(driver, 'host_image_evidence', lambda _: True)
    monkeypatch.setattr(driver, 'launcher_host', lambda **_: [])
    monkeypatch.setattr(driver, 'LAUNCHER', tmp_path / 'missing')
    monkeypatch.setattr(driver, 'launcher_container_uid', lambda: driver.os.open(tmp_path / 'missing', os.O_RDONLY))
    folder = tmp_path / 'grants'
    folder.mkdir(mode=0o750)
    plan = {'account': 'alex-agent', 'repo': '/example/repo', 'root': '/example/work',
            'policy_id': 'qualification-phase1', 'image': 'sha256:' + 'a' * 64}
    report = driver.qualify(plan, grants=folder, call=lambda *a: pytest.fail('no broker call before the host check'))
    assert report['go_phase_1'] is False
    assert report['launcher_host'] == [f'{tmp_path / "missing"} does not declare one protected container uid '
                                       '(FileNotFoundError): install the reviewed launcher']
    assert report['checks']['trusted_launcher']['status'] == 'fail'
    assert not list(folder.iterdir())


@pytest.mark.parametrize('reported', ['host', 'root-ish', 'launcher-changed'])
def test_a_fresh_report_with_the_wrong_uid_fails_fast_with_a_clear_reason(tmp_path, monkeypatch, reported,
                                                                         as_agent_account):
    """Attempt 15: the driver compared the payload's uid with the host account's (1006) and waited out a valid
    report from uid 1000 until "payload report timed out". A report with this run's nonce and another uid now stops
    the run at once, saying which uid it expected and why."""
    import base64
    import json
    from types import SimpleNamespace

    driver = load('driver')
    host_uid = driver.os.getuid()  # the control and payload accounts share it here, as in the other driver tests
    expected = 2345 if reported == 'launcher-changed' else LAUNCHER_UID  # L-2: the launcher's value is used
    if host_uid == expected:
        pytest.skip('the runner has the container uid')
    monkeypatch.setattr(driver, 'policy_matches', lambda _: True)
    monkeypatch.setattr(driver, 'host_image_evidence', lambda _: True)
    monkeypatch.setattr(driver, 'launcher_host', lambda **_: [])
    monkeypatch.setattr(driver, 'launcher_container_uid', lambda: expected)
    monkeypatch.setattr(driver.time, 'sleep', lambda _: None)
    monkeypatch.setattr(driver, 'cgroup_evidence', lambda _: (True, '/example'))
    monkeypatch.setattr(driver, 'broker_pid', lambda _: 12)
    monkeypatch.setattr(driver, 'foreign_processes', lambda *a, **k: [])
    folder = tmp_path / 'grants'
    folder.mkdir(mode=0o750)
    if folder.stat().st_uid != as_agent_account:
        os.chown(folder, as_agent_account, -1)  # root-run suite: the control account owns its grants folder
    reads = []

    def call(account, method, params):
        if method == 'ping':
            return {'role': 'agents', 'user': account, 'run_dir': '/example/run'}
        if method == 'make_private_checkout':
            return {'path': '/example/work/.worktrees/qualification'}
        if method == 'launch':
            generation = params['container']['generation']
            return {'container': {'unit': f'acc-ct-{params["acc_id"]}-g{generation}.service', 'state': 'running'}}
        if method == 'read_artifact':
            reads.append(1)
            grant = json.loads(next(folder.glob('*.json')).read_text())
            data = json.dumps({'version': 1, 'go_phase_1': False, 'nonce': grant['args'][1],
                               'uid': {'host': host_uid, 'root-ish': 4242}.get(reported, LAUNCHER_UID), 'no_new_privs': '1',
                               'checks': {name: {'status': 'pass', 'reason': 'observed'}
                                          for name in driver.MANDATORY}}).encode()
            return {'size': len(data), 'data': base64.b64encode(data).decode()}
        if method == 'kill':
            return {'container': {'stopped': True}}
        pytest.fail(method)
    monkeypatch.setattr(driver, 'pwd', SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_uid=host_uid)))
    plan = {'account': 'alex-agent', 'repo': '/example/repo', 'root': '/example/work',
            'policy_id': 'qualification-phase1', 'image': 'sha256:' + 'a' * 64}
    report = driver.qualify(plan, grants=folder, call=call)
    assert len(reads) == 1  # no waiting for a report that can never match
    assert report['go_phase_1'] is False
    assert f'not uid {expected}' in report['reason'] and 'payload' in report['reason']
    assert report['checks']['trusted_launcher']['status'] == 'fail'
    assert not list(folder.iterdir())


# ---- attempt 15: a failed hook probe says what it saw ---------------------------------------------------------------

def test_hook_evidence_summary_names_what_was_and_was_not_seen():
    """Attempt 15: claude_hooks and copilot_hooks failed with exit 0 and three replay requests, and the report said only
    what was required. The reason now lists the hook events seen, the denials by rule and both markers."""
    q = load('phase1')
    tampered = {'provider': 'claude', 'event': 'PreToolUse', 'hook': {'tool_input': {'command': q.FORBIDDEN}},
                'guard': {'rule': 'tampered', 'reason': 'x'}}
    errored = {'provider': 'claude', 'event': 'PreToolUse', 'hook': {'tool_input': {'command': q.ALLOWED}},
               'guard': {'rule': 'guard_error', 'reason': 'x'}, 'guard_error': 'KeyError'}
    other = {'provider': 'codex', 'event': 'PostToolUse', 'hook': {'tool_input': {'command': q.ALLOWED}}}
    text = q.evidence_summary('claude', [tampered, errored, other, 'junk'], forbidden_exists=False,
                              allowed_exists=False)
    assert 'claude hook events: 2 (PreToolUse 2)' in text
    assert 'denials: tampered, guard_error' in text and 'guard errors: KeyError' in text
    assert 'custom denial of the forbidden command: no' in text and 'post-tool event for the allowed command: no' in text
    assert 'forbidden marker: absent' in text and 'allowed marker: absent' in text
    none = q.evidence_summary('copilot', [], forbidden_exists=True, allowed_exists=True)
    assert 'copilot hook events: 0' in none and 'forbidden marker: present' in none and 'denials: none' in none


def test_hook_probe_reason_carries_the_summary_even_when_the_cli_succeeds(tmp_path, monkeypatch):
    import json

    q = load('phase1')
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/container_qualification'))

    class Paths:
        def __new__(cls, value):
            value = str(value)
            return tmp_path / Path(value).name if value.startswith(('/tmp/acc-qualification-', '/tmp/acc-state/')) else Path(value)
    monkeypatch.setattr(q, 'Path', Paths)
    events = tmp_path / 'events.jsonl'
    events.write_text('')

    def cli(command, **kwargs):
        with events.open('a') as stream:
            stream.write(json.dumps({'provider': 'claude', 'event': 'PreToolUse', 'guard': {'rule': 'custom'},
                                     'hook': {'tool_input': {'command': q.FORBIDDEN}}}) + '\n')
        return 0, 'early output\n' + 'x' * 2000 + '\nhook: UserPromptSubmit Blocked'
    monkeypatch.setattr(q, 'run', cli)
    reason = q.provider_probe('claude', tmp_path)['reason']
    assert 'claude hook events: 1 (PreToolUse 1)' in reason and 'custom denial of the forbidden command: yes' in reason
    # Attempt 16: Codex exited 0 without a model request and the reason said nothing; a failing row now ends with the
    # last part of the CLI's own output.
    assert reason.endswith('hook: UserPromptSubmit Blocked') and 'early output' not in reason


def copilot_refusal_context(tmp_path, monkeypatch, refuses):
    import json

    q = load('phase1')
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/container_qualification'))
    session = tmp_path / 'copilot-home'
    session.mkdir()
    monkeypatch.setenv('COPILOT_HOME', str(session))
    real_path = q.Path

    def paths(value):
        value = str(value)
        markers = ('/tmp/acc-qualification-forbidden', '/tmp/acc-qualification-allowed')
        return tmp_path / real_path(value).name if value.startswith(markers) else real_path(value)
    monkeypatch.setattr(q, 'Path', paths)
    (tmp_path / 'events.jsonl').write_text('')
    runs = []

    def cli(command, **kwargs):
        workspace = real_path(kwargs['cwd'])
        hostile = sorted(str(p.relative_to(workspace)) for p in workspace.rglob('settings*.json'))
        user = (session / 'settings.json').exists()
        runs.append((hostile, user, command[0], kwargs['env']['COPILOT_HOME']))
        if refuses and (user or any(name.startswith('.github/') for name in hostile)):
            return 78, "Rynhult: Copilot's hooks can't be verified, so it won't start: ... sets disableAllHooks"
        return 0, json.dumps({})
    monkeypatch.setattr(q, 'run', cli)
    return q, session, runs


def test_copilot_probe_requires_the_wrapper_to_refuse_settings_that_switch_its_hooks_off(tmp_path, monkeypatch):
    q, session, runs = copilot_refusal_context(tmp_path, monkeypatch, refuses=True)
    reason = q.provider_probe('copilot', tmp_path)['reason']
    assert [run[:2] for run in runs] == [(['.github/copilot/settings.json'], False),
                                        (['.github/copilot/settings.local.json'], False),
                                        ([], True),
                                        (['.copilot/settings.json'], False)]  # then hooks must be enforced
    assert all(run[2] == 'copilot' and run[3] == str(session) for run in runs)
    assert not (session / 'settings.json').exists()
    assert ('refusals before any model request: .github/copilot/settings.json: refused; '
            '.github/copilot/settings.local.json: refused; session settings.json: refused; ') in reason


def test_copilot_probe_fails_when_the_wrapper_lets_hook_disabling_settings_through(tmp_path, monkeypatch):
    q, _, _ = copilot_refusal_context(tmp_path, monkeypatch, refuses=False)
    checked = q.provider_probe('copilot', tmp_path)
    assert checked['status'] == 'fail' and '.github/copilot/settings.json: not refused (exit=0)' in checked['reason']


def test_copilot_probe_expects_the_image_wrappers_refusal():
    q = load('phase1')
    policy = (ROOT / 'deploy/containers/sandbox_policy.py').read_text()
    assert q.COPILOT_REFUSED == 78 and 'COPILOT_REFUSED = 78' in policy
    assert "Copilot's hooks can't be verified" in policy
    assert "'/opt/acc/src/acc/providers/copilot_settings.py'" in policy  # the broker's own check, shared
    shared = (ROOT / 'src/acc/providers/copilot_settings.py').read_text()
    assert all(f'"{name}"' in shared for name in q.COPILOT_REFUSALS if name)


def test_the_guard_denies_the_forbidden_command_by_the_custom_rule_and_allows_the_other():
    """Attempt 15's hook rows: with the driver's policy and the attempt's paths, the real guard decides as required, so a
    missing denial or post-tool event came from the CLI side, not from the rule."""
    import importlib.machinery
    import json

    q = load('phase1')
    loader = importlib.machinery.SourceFileLoader('acc_guard_phase1', str(ROOT / 'bin/acc-guard'))
    guard = importlib.util.module_from_spec(importlib.util.spec_from_loader('acc_guard_phase1', loader))
    loader.exec_module(guard)
    source = (ROOT / 'scripts/container_qualification/driver.py').read_text()
    pattern = json.loads('"' + source.split("'pattern': '", 1)[1].split("'", 1)[0] + '"')
    policy = {'rules': {}, 'custom': [{'pattern': pattern, 'reason': 'qualification forbidden command'}],
              'workspace': '/srv/acc/workspaces/qual/.worktrees/qualification-1'}
    cwd, run_dir = '/tmp/acc-qualification-repo-1', '/srv/acc/run/qual-agent/1'
    assert guard.check(policy, 'Bash', {'command': q.FORBIDDEN}, cwd, run_dir)[0] == 'custom'
    assert guard.check(policy, 'Bash', {'command': q.ALLOWED}, cwd, run_dir) is None
