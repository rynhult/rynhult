"""Only fake image policies, homes, runtime and upstreams; no host sign-ins."""
import json
import socket
from pathlib import Path

import pytest

from acc.broker import container_integrations as integrations
from acc.broker.config import BrokerConfig
from acc.broker.containers import ContainerError


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.codex').mkdir()
    (home / '.codex/auth.json').write_text(json.dumps({'auth_mode': 'apikey', 'OPENAI_API_KEY': 'synthetic-only'}))
    (home / '.codex/auth.json').chmod(0o600)
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setenv('ACC_CONTAINERS', '1')
    monkeypatch.setattr(integrations, '_read_policy', lambda *args: {'credentials': 'readonly-v1'})
    config = BrokerConfig(run_dir=tmp_path / 'run')
    config.run_dir.mkdir()
    yield config
    for session in ('s12345678', 's99999999'):
        integrations.stop(config, session)


def test_two_session_credentials_and_network_lifetimes_are_partitioned(world):
    for session, generation in [('s12345678', 1), ('s99999999', 2)]:
        run = world.run_dir / session; run.mkdir()
        integrations.start(world, session, {'generation': generation, 'policy_id': 'pinned'}, 'codex', run)
    for session, generation in [('s12345678', 1), ('s99999999', 2)]:
        path = world.run_dir / session / '.acc-integrations.sock'
        # The test's UID is not privileged: ordinary payloads cannot bootstrap credentials.
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path)); client.sendall(b'CONNECT api.example.com:443 HTTP/1.1\r\n\r\n')
            assert b'403' in client.recv(4096)  # no egress opt-in in this image policy
        gateway = integrations._gateways[(world.run_dir, session)]
        assert gateway.metadata['generation'] == generation
        assert gateway.metadata['credentials']['session_id'] == session
    integrations.stop(world, 's12345678')
    assert not (world.run_dir / 's12345678/.acc-integrations.sock').exists()
    assert (world.run_dir / 's99999999/.acc-integrations.sock').exists()
    assert not list(world.run_dir.rglob('auth.json'))


@pytest.mark.parametrize('attack', ['disabled', 'no_signin', 'existing_socket', 'bad_policy'])
def test_adapter_refusal_never_leaks_or_falls_back(world, monkeypatch, attack):
    run = world.run_dir / 's12345678'; run.mkdir()
    if attack == 'disabled': monkeypatch.setenv('ACC_CONTAINERS', '0')
    if attack == 'no_signin': (Path.home() / '.codex/auth.json').unlink()
    if attack == 'existing_socket': (run / '.acc-integrations.sock').write_text('do not remove')
    if attack == 'bad_policy': monkeypatch.setattr(integrations, '_read_policy', lambda *args: {'credentials': 'writable-home'})
    with pytest.raises(ContainerError) as error:
        integrations.start(world, 's12345678', {'generation': 1, 'policy_id': 'pinned'}, 'codex', run)
    assert 'synthetic-only' not in str(error.value)
    assert (world.run_dir, 's12345678') not in integrations._gateways
    if attack == 'existing_socket': assert (run / '.acc-integrations.sock').read_text() == 'do not remove'


def test_offline_policy_does_not_read_any_home(world, monkeypatch):
    monkeypatch.setattr(integrations, '_read_policy', lambda *args: {})
    monkeypatch.setattr(integrations, 'snapshot', lambda *args, **kwargs: pytest.fail('offline launch read sign-in'))
    integrations.start(world, 's12345678', {'generation': 1, 'policy_id': 'pinned'}, 'codex', world.run_dir)
    assert not integrations._gateways


def test_stale_stop_cannot_close_a_new_generation_gateway(world):
    run = world.run_dir / 's12345678'; run.mkdir()
    integrations.start(world, 's12345678', {'generation': 2, 'policy_id': 'pinned'}, 'codex', run)
    integrations.stop(world, 's12345678', generation=1)
    assert (run / '.acc-integrations.sock').exists()
    integrations.stop(world, 's12345678', generation=2)
    assert not (run / '.acc-integrations.sock').exists()


def test_stop_closes_egress_even_when_root_helper_is_unreachable(world, monkeypatch):
    from acc.broker import ops
    run = world.run_dir / 's12345678'; run.mkdir()
    integrations.start(world, 's12345678', {'generation': 1, 'policy_id': 'pinned'}, 'codex', run)
    def refused(*args, **kwargs): raise ContainerError('synthetic helper outage')
    monkeypatch.setattr(ops.containers, 'ask', refused)
    answer = ops.stop_container(world, 's12345678', {'generation': 1, 'policy_id': 'pinned'})
    assert answer['stopped'] is False
    assert not (run / '.acc-integrations.sock').exists()


def test_failed_root_launch_closes_gateway_without_native_fallback(world, tmp_path, monkeypatch):
    import subprocess

    from acc.broker import ops
    checkout = tmp_path / 'workspace'; (checkout / '.git').mkdir(parents=True)
    monkeypatch.setattr(ops, '_under_roots', lambda *args: checkout)
    monkeypatch.setattr(ops, 'exclude_artifacts', lambda *args: None)
    monkeypatch.setattr(ops, 'prepare_tmux', lambda *args: None)
    monkeypatch.setattr(ops, 'tmux', lambda *args, **kwargs: subprocess.CompletedProcess([], 1, '', ''))
    monkeypatch.setattr(ops.providers, 'get', lambda *args: pytest.fail('native bootstrap fallback'))
    def refused(*args, **kwargs):
        assert (world.run_dir / 's12345678/.acc-integrations.sock').exists()
        raise ContainerError('synthetic helper outage')
    monkeypatch.setattr(ops.containers, 'ask', refused)
    with pytest.raises(ops.OpError, match='container launch refused'):
        ops.launch(world, {'acc_id': 's12345678', 'tmux_name': 'acc-test', 'provider': 'codex', 'cwd': str(checkout),
                           'container': {'generation': 1, 'policy_id': 'pinned'}})
    assert not (world.run_dir / 's12345678/.acc-integrations.sock').exists()


def test_absent_policy_is_offline_even_with_a_private_fixture_prefix(tmp_path):
    # No root metadata or live /etc policy is needed for an offline fake launcher.
    with pytest.raises(FileNotFoundError):
        integrations._read_policy(tmp_path / 'absent.json', 'pinned')


def test_a_refused_relaunch_keeps_the_running_generation_and_replaces_an_older_one(world, tmp_path, monkeypatch):
    """Review CP2-2: a launch refused because this generation's gateway already serves a running container must not
    close that gateway; a new generation replaces an older generation's gateway left behind by a container that ended
    without a broker stop."""
    import subprocess

    from acc.broker import ops
    run = world.run_dir / 's12345678'; run.mkdir()
    integrations.start(world, 's12345678', {'generation': 1, 'policy_id': 'pinned'}, 'codex', run)
    running = integrations._gateways[(world.run_dir, 's12345678')]
    checkout = tmp_path / 'workspace'; (checkout / '.git').mkdir(parents=True)
    monkeypatch.setattr(ops, '_under_roots', lambda *args: checkout)
    monkeypatch.setattr(ops, 'exclude_artifacts', lambda *args: None)
    monkeypatch.setattr(ops, 'prepare_tmux', lambda *args: None)
    monkeypatch.setattr(ops, 'tmux', lambda *args, **kwargs: subprocess.CompletedProcess([], 1, '', ''))
    monkeypatch.setattr(ops.containers, 'ask', lambda *args, **kwargs: pytest.fail('launched over a running generation'))
    with pytest.raises(ops.OpError, match='container launch refused'):
        ops.launch(world, {'acc_id': 's12345678', 'tmux_name': 'acc-test', 'provider': 'codex', 'cwd': str(checkout),
                           'container': {'generation': 1, 'policy_id': 'pinned'}})
    assert integrations._gateways[(world.run_dir, 's12345678')] is running
    assert (run / '.acc-integrations.sock').exists()
    integrations.start(world, 's12345678', {'generation': 2, 'policy_id': 'pinned'}, 'codex', run)
    assert integrations._gateways[(world.run_dir, 's12345678')].metadata['generation'] == 2
    assert running._stop.is_set()
    with pytest.raises(ContainerError):  # never back to an older generation
        integrations.start(world, 's12345678', {'generation': 1, 'policy_id': 'pinned'}, 'codex', run)
    assert integrations._gateways[(world.run_dir, 's12345678')].metadata['generation'] == 2


@pytest.mark.parametrize('credentials,mode', [({'ANTHROPIC_API_KEY': 'synthetic-claude-key'}, 'api_key'),
                                             ({'ANTHROPIC_AUTH_TOKEN': 'synthetic-claude-token'}, 'api_key'),
                                             ({'CLAUDE_CODE_USE_BEDROCK': '1', 'AWS_ACCESS_KEY_ID': 'synthetic-id',
                                               'AWS_SECRET_ACCESS_KEY': 'synthetic-secret'}, 'cloud')])
def test_claude_launch_credentials_replace_account_subscription(world, monkeypatch, credentials, mode):
    from acc.broker.claude_auth import supplied_mode
    run = world.run_dir / 's12345678'; run.mkdir()
    monkeypatch.setattr(integrations, 'snapshot', lambda *a, **kw: pytest.fail('read account subscription'))
    integrations.start(world, 's12345678', {'generation': 1, 'policy_id': 'pinned'}, 'claude', run,
                       env={**credentials, 'UNRELATED_PROJECT_KEY': 'must-not-deliver'})
    envelope = integrations._gateways[(world.run_dir, 's12345678')].metadata['credentials']
    assert envelope['credentials'] == {'launch_env': credentials}
    assert supplied_mode(envelope['credentials']['launch_env']) == mode
    assert not list(run.rglob('auth.json'))


@pytest.mark.parametrize('reported', ['subscription', 'unknown', 'cloud'])
def test_container_auth_is_downgrade_only(world, monkeypatch, reported):
    from acc.broker import claude_auth, containers
    actual = ['api_key']
    monkeypatch.setattr(containers, 'auth_mode', lambda *args: actual[0])
    world.auth_contexts['acc-test'] = dict(acc_id='s12345678', provider='claude', env={'ANTHROPIC_API_KEY': 'synthetic'},
                                           container={'generation': 1, 'policy_id': 'pinned'})
    assert claude_auth.refresh(world, 'acc-test')['auth_mode'] == 'api_key'
    actual[0] = reported
    assert claude_auth.refresh(world, 'acc-test')['auth_mode'] == (reported if reported != 'cloud' else 'unknown')
    actual[0] = 'api_key'
    assert claude_auth.refresh(world, 'acc-test')['auth_mode'] == 'unknown'


@pytest.mark.parametrize('trusted', [False, True])
def test_container_auth_probe_requires_the_session_cgroup(world, monkeypatch, trusted):
    from acc.broker import container_attach, containers
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): assert value == 10
        def connect(self, path): assert path.endswith('/auth.sock')
        def recv(self, size):
            assert trusted, 'untrusted listener must not supply an auth mode'
            return b'{"auth_mode":"api_key"}\n'
    monkeypatch.setattr(containers.socket, 'socket', lambda *args: Client())
    def identity(conn, unit):
        assert unit == containers.unit_cgroup('s12345678', 1)
        return trusted
    monkeypatch.setattr(container_attach, 'relay_is_the_units', identity)
    assert containers.auth_mode(world, 's12345678', {'generation': 1}) == ('api_key' if trusted else 'unknown')
