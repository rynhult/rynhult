"""Trusted-launcher integrations; all runtime commands and peer identities are fakes."""
import fcntl
import json
import os
from pathlib import Path

import pytest
from test_containers_helper import ask, calls, env, grant, request  # noqa: F401 (env is a fixture)


def enable(env, egress=False):  # noqa: F811
    source = Path(__file__).resolve().parents[1] / 'src/acc/container_credentials.py'
    (env.root / 'credentials.py').write_bytes(source.read_bytes())
    (env.root / 'credentials.py').chmod(0o644)
    policy = json.loads(env.policy.read_text())
    policy['images']['default']['credentials'] = 'readonly-v1'
    if egress:
        policy['images']['default']['egress'] = {'mode': 'internet-v1', 'deny_hosts': ['control.example.com'],
                                                'deny_networks': ['9.9.9.9/32']}
    env.policy.write_text(json.dumps(policy))
    return env.m.load_policy('default')


def test_adapter_launch_seals_credential_and_mounts_only_fixed_readonly_views(env, monkeypatch):  # noqa: F811
    from acc.container_credentials import policy_digest
    policy = enable(env)
    g = grant(env, provider='codex')
    policy['providers']['codex'] = '/usr/local/bin/codex'
    envelope = {'version': 1, 'provider': 'codex', 'session_id': g['session_id'], 'generation': 1,
                'credentials': {'auth_mode': 'apikey', 'OPENAI_API_KEY': 'SYNTHETIC-SECRET'}}
    monkeypatch.setattr(env.m, 'bootstrap_integrations', lambda *args: ({'credentials': envelope,
        'policy_digest': policy_digest(policy), 'provider': 'codex', 'session_id': g['session_id'], 'generation': 1}, None))
    entry = env.m.pwd.getpwnam('alex-agent')
    held, _ = env.m.check_mounts(g, entry, env.m.broker_settings('alex-agent'))
    try:
        env.m.freeze_integrations(g, held, entry, policy)
        credential_fd = held[g['_credential_fd']]
        assert json.loads(os.pread(credential_fd, 131073, 0)) == envelope['credentials']
        seals = fcntl.fcntl(credential_fd, fcntl.F_GET_SEALS)
        assert seals & fcntl.F_SEAL_WRITE and seals & fcntl.F_SEAL_GROW and seals & fcntl.F_SEAL_SHRINK
        with pytest.raises(OSError): os.write(credential_fd, b'changed')
        # No credential file is left on disk (including the frozen run snapshot).
        assert all(b'SYNTHETIC-SECRET' not in p.read_bytes() for p in env.root.rglob('*') if p.is_file())
        argv = env.m.podman_argv(g, policy, entry)
        assert any('dst=/run/acc-credentials/auth.json,ro' in arg for arg in argv)
        assert '--network=none' in argv
        # Replay must retain main's live read-only team channel alongside sealed auth.
        assert any("/team,dst=" in arg and arg.endswith("/team,ro") for arg in argv)
        held.append(os.open(env.root, os.O_RDONLY | os.O_DIRECTORY))
        unit = env.m.systemd_run_argv(g, policy, entry, held=held)
        assert any(f"/fd/{held[4]}:" in arg and arg.endswith("/team:norbind") for arg in unit)
        assert any(f"/fd/{credential_fd}:" in arg and arg.endswith("/auth:norbind") for arg in unit)
        assert 'SYNTHETIC-SECRET' not in str(argv)
        assert not any(env.home.as_posix() in arg for arg in argv)
    finally:
        for fd in held: os.close(fd)


@pytest.mark.parametrize('change', ['generation', 'session_id', 'provider', 'policy_digest', 'extra_config'])
def test_launcher_revalidates_broker_snapshot_without_secret_error_details(env, monkeypatch, change):  # noqa: F811
    from acc.container_credentials import policy_digest
    policy = enable(env)
    g = grant(env)
    secret = {'version': 1, 'provider': 'claude', 'session_id': g['session_id'], 'generation': 1,
              'credentials': {'claudeAiOauth': {'accessToken': 'SYNTHETIC-SECRET', 'expiresAt': 9999999999999,
                                                'scopes': ['user:inference']}}}
    value = {'credentials': secret, 'policy_digest': policy_digest(policy), 'provider': 'claude',
             'session_id': g['session_id'], 'generation': 1}
    if change == 'extra_config': secret['credentials']['hooks'] = {'evil': True}
    else: value[change] = 'wrong'
    monkeypatch.setattr(env.m, 'bootstrap_integrations', lambda *args: (value, None))
    entry = env.m.pwd.getpwnam('alex-agent')
    held, _ = env.m.check_mounts(g, entry, env.m.broker_settings('alex-agent'))
    try:
        with pytest.raises(env.m.Refused) as error:
            env.m.freeze_integrations(g, held, entry, policy)
        assert 'SYNTHETIC-SECRET' not in error.value.detail
        assert len(held) == 2
    finally:
        for fd in held: os.close(fd)


def test_missing_broker_adapter_refuses_and_audits(env):  # noqa: F811
    enable(env)
    grant(env)
    answer = ask(env, request())
    assert not answer['ok'] and answer['code'] == 'bad_integrations'
    assert not any('systemd-run ' in call for call in calls(env))
    assert 'bad_integrations' in ''.join(p.read_text() for p in (env.root / 'audit').glob('*'))


def test_fake_end_to_end_launch_authenticates_gateway_and_keeps_network_none(env, monkeypatch):  # noqa: F811
    from acc.container_credentials import policy_digest
    from acc.container_egress import Gateway, Policy
    policy = enable(env, egress=True)
    g = grant(env)
    envelope = {'version': 1, 'provider': 'claude', 'session_id': g['session_id'], 'generation': 1,
                'credentials': {'claudeAiOauth': {'accessToken': 'SYNTHETIC-SECRET', 'expiresAt': 9999999999999,
                                                  'scopes': ['user:inference']}}}
    observed = []
    original = env.m.systemd_run_argv
    def argv(grant, policy, entry, *, held):
        assert json.loads(os.pread(held[grant['_credential_fd']], 131073, 0)) == envelope['credentials']
        assert env.m.stat.S_ISSOCK(os.fstat(held[grant['_egress_fd']]).st_mode)
        observed.extend(original(grant, policy, entry, held=held))
        return observed
    monkeypatch.setattr(env.m, 'systemd_run_argv', argv)
    with Gateway(env.run_dir / '.acc-integrations.sock', g['session_id'], 1, 'claude', envelope, Policy(),
                 control_uid=os.getuid(), policy_digest=policy_digest(policy)):
        answer = ask(env, request())
    assert answer['ok'], answer
    assert '--network=none' in observed
    assert any('dst=/run/acc-egress/socket,ro' in arg for arg in observed)
    assert len([arg for arg in observed if 'BindReadOnlyPaths=' in arg]) == 3
    assert 'SYNTHETIC-SECRET' not in str(calls(env))
    assert all(b'SYNTHETIC-SECRET' not in p.read_bytes() for p in env.root.rglob('*') if p.is_file())
    contract = json.loads((env.m.input_dir(g) / '.acc-launch-contract.json').read_text())
    assert contract['egress'] is True and contract['provider'] == 'claude'
    audit = ''.join(p.read_text() for p in (env.root / 'audit').glob('*'))
    assert '"credential_adapter": "readonly-v1"' in audit and '"egress": "internet-v1"' in audit
    assert 'SYNTHETIC-SECRET' not in audit


@pytest.mark.parametrize('attack', ['wrong_pid', 'wrong_uid', 'regular_file', 'symlink'])
def test_gateway_identity_cannot_be_forged_by_session_process(env, monkeypatch, attack):  # noqa: F811
    from acc.container_egress import Gateway, Policy
    enable(env)
    g = grant(env)
    path = env.run_dir / '.acc-integrations.sock'
    if attack in ('regular_file', 'symlink'):
        if attack == 'regular_file': path.write_text('{}')
        else: path.symlink_to('/dev/null')
        answer = ask(env, request())
    else:
        monkeypatch.setattr(env.m, 'peer', lambda conn: (1, 1004) if attack == 'wrong_pid' else (4242, 1005))
        with Gateway(path, g['session_id'], 1, 'claude', {}, Policy(), control_uid=os.getuid()):
            answer = ask(env, request())
    assert not answer['ok'] and answer['code'] == 'bad_integrations'
    assert not any('systemd-run ' in call for call in calls(env))


def _held_run(env, g, monkeypatch):  # noqa: F811
    """Real SO_PEERCRED and this runner's uid: the sockets below are really this process's (or a child's)."""
    from types import SimpleNamespace
    entry = env.m.pwd.getpwnam('alex-agent')
    held, _ = env.m.check_mounts(g, entry, env.m.broker_settings('alex-agent'))
    # Mount admission above models the agent account; bootstrap below uses real
    # sockets and SO_PEERCRED, so descriptor/name ownership must be real as well.
    # Otherwise a root runner sees peer uid 0 but fixture fstat reports uid 1004.
    monkeypatch.setattr(env.m.os, 'fstat', os.fstat)
    monkeypatch.setattr(env.m.os, 'stat', os.stat)
    monkeypatch.setattr(env.m, 'peer', env.real_peer)
    monkeypatch.setattr(env.m, 'broker_main_pid', lambda _: os.getpid())
    return SimpleNamespace(pw_uid=os.getuid(), pw_name='alex-agent'), held, Path(os.readlink(f'/proc/self/fd/{held[1]}'))


@pytest.mark.parametrize('distinct_agent', [False, True], ids=['default-agent', 'different-agent-uid'])
def test_bootstrap_reads_the_broker_gateway_and_returns_that_socket(env, monkeypatch, distinct_agent):  # noqa: F811
    from acc.container_egress import Gateway, Policy
    if distinct_agent:
        import test_containers_helper
        # Reproduce root's mismatch without privileges: modeled agent != real peer.
        uid = os.getuid() + 1
        monkeypatch.setattr(test_containers_helper, 'AGENT_UID', uid)
        env.users['alex-agent'] = (uid, str(env.home))
    g = grant(env)
    entry, held, run = _held_run(env, g, monkeypatch)
    try:
        with Gateway(run / '.acc-integrations.sock', g['session_id'], 1, 'claude', {'synthetic': 'CANARY'}, Policy(),
                     control_uid=os.getuid()) as gateway:
            value, fd = env.m.bootstrap_integrations(g, held, entry)
            try:
                assert value['credentials'] == {'synthetic': 'CANARY'} and value['session_id'] == g['session_id']
                assert (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (gateway.identity.st_dev, gateway.identity.st_ino)
            finally:
                os.close(fd)
    finally:
        for fd in held: os.close(fd)


def test_bootstrap_never_mounts_a_socket_other_than_the_authenticated_broker(env, monkeypatch):  # noqa: F811
    """A same-account process swaps names around root's check; the mounted inode must be the one authenticated."""
    import subprocess
    import sys

    from acc.container_egress import Gateway, Policy
    g = grant(env)
    entry, held, run = _held_run(env, g, monkeypatch)
    name, aside_broker, aside_attacker = run / '.acc-integrations.sock', run / '.broker', run / '.attacker'
    attacker = None
    try:
        with Gateway(name, g['session_id'], 1, 'claude', {'synthetic': 'CANARY'}, Policy(), control_uid=os.getuid()):
            os.rename(name, aside_broker)
            attacker = subprocess.Popen([sys.executable, '-c', (
                'import os,socket,sys,time; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); os.chmod(sys.argv[1], 0o600);'
                ' s.listen(4); print("ready", flush=True); time.sleep(30)'), name.name], cwd=run, stdout=subprocess.PIPE, text=True)
            assert attacker.stdout.readline().strip() == 'ready'
            real_open, real_peer = env.m.os.open, env.m.peer

            def swapping_open(path, flags, *args, **kwargs):
                fd = real_open(path, flags, *args, **kwargs)
                if str(path).endswith('/.acc-integrations.sock') and flags & os.O_PATH:
                    os.rename(name, aside_attacker); os.rename(aside_broker, name)  # root connects to the broker...
                return fd

            def swapping_peer(conn):
                os.rename(name, aside_broker); os.rename(aside_attacker, name)  # ...then the attacker's is back
                return real_peer(conn)

            monkeypatch.setattr(env.m.os, 'open', swapping_open)
            monkeypatch.setattr(env.m, 'peer', swapping_peer)
            with pytest.raises(env.m.Refused):
                env.m.bootstrap_integrations(g, held, entry)
    finally:
        if attacker is not None:
            attacker.kill(); attacker.wait()
        for fd in held: os.close(fd)
