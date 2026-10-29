"""Local-only code generation; authenticated broker verification never discloses a code."""
import json

import pytest

from acc.node import group_proof


def test_code_is_single_use_bound_and_not_stored_in_cleartext(tmp_path):
    code = group_proof.issue(tmp_path, 'a' * 32, 'agent', 'b' * 32, now=100)
    state = (tmp_path / '.group-proof.json').read_text()
    assert code not in state and len(code) == 10
    assert (tmp_path / '.group-proof.json').stat().st_mode & 0o777 == 0o600
    assert group_proof.verify(tmp_path, 'a' * 32, 'agent', 'b' * 32, code, now=101)
    assert not group_proof.verify(tmp_path, 'a' * 32, 'agent', 'b' * 32, code, now=102)
    assert json.loads((tmp_path / '.group-proof.json').read_text())['used']


@pytest.mark.parametrize('node,account,request_id', [
    ('c' * 32, 'agent', 'b' * 32),
    ('a' * 32, 'other-agent', 'b' * 32),
    ('a' * 32, 'agent', 'd' * 32),
])
def test_wrong_machine_account_and_request_are_refused(tmp_path, node, account, request_id):
    code = group_proof.issue(tmp_path, 'a' * 32, 'agent', 'b' * 32, now=100)
    assert not group_proof.verify(tmp_path, node, account, request_id, code, now=101)


def test_codes_expire_and_attempts_and_issuance_are_persistent(tmp_path):
    code = group_proof.issue(tmp_path, 'a' * 32, 'agent', 'b' * 32, now=100)
    assert not group_proof.verify(tmp_path, 'a' * 32, 'agent', 'b' * 32, code, now=401)
    with pytest.raises(ValueError, match='minute'):
        group_proof.issue(tmp_path, 'a' * 32, 'agent', 'b' * 32, now=110)
    code = group_proof.issue(tmp_path, 'a' * 32, 'agent', 'b' * 32, now=500)
    for _ in range(5):
        assert not group_proof.verify(tmp_path, 'a' * 32, 'agent', 'b' * 32, 'WRONGCODE1', now=501)
    assert not group_proof.verify(tmp_path, 'a' * 32, 'agent', 'b' * 32, code, now=502)


def test_proof_state_never_follows_links_or_uses_shared_directories(tmp_path):
    target = tmp_path / 'private'
    target.write_text('untouched')
    (tmp_path / '.group-proof.json').symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        group_proof.issue(tmp_path, 'a' * 32, 'agent', 'b' * 32)
    assert target.read_text() == 'untouched'
    (tmp_path / '.group-proof.json').unlink()
    tmp_path.chmod(0o777)
    with pytest.raises(ValueError, match='private'):
        group_proof.issue(tmp_path, 'a' * 32, 'agent', 'b' * 32)


def test_concurrent_checks_consume_only_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    code = group_proof.issue(tmp_path, 'a' * 32, 'agent', 'b' * 32)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: group_proof.verify(tmp_path, 'a' * 32, 'agent', 'b' * 32, code), range(4)))
    assert results.count(True) == 1


def test_local_command_uses_actual_account_and_rejects_root(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    args = SimpleNamespace(run_dir=tmp_path, node_id='a' * 32, request='b' * 32)
    monkeypatch.setattr(group_proof.pwd, 'getpwuid', lambda _: SimpleNamespace(pw_name='root'))
    with pytest.raises(ValueError, match='ordinary agent account'):
        group_proof.show(args)
    monkeypatch.setattr(group_proof.pwd, 'getpwuid', lambda _: SimpleNamespace(pw_name='alex-agent'))
    assert group_proof.show(args) == 0
    code = capsys.readouterr().out.split('Confirmation code: ')[1].splitlines()[0]
    assert group_proof.verify(tmp_path, 'a' * 32, 'alex-agent', 'b' * 32, code)


async def test_broker_only_exposes_consuming_verification(env):
    from acc.broker.server import Broker
    broker = Broker(env['broker'])
    assert 'group_machine_verify' in broker.methods
    assert not any(name in broker.methods for name in ('group_machine_issue', 'group_machine_code', 'group_proof'))
    code = group_proof.issue(env['broker'].run_dir, 'a' * 32, 'agent', 'b' * 32)
    response = await broker._dispatch({'id': 1, 'method': 'group_machine_verify', 'params': {
        'node_id': 'a' * 32, 'proof_account': 'agent', 'request_id': 'b' * 32, 'code': code}})
    assert response == {'id': 1, 'ok': True, 'result': {'verified': True}}
    restarted = Broker(env['broker'])
    replay = await restarted._dispatch({'id': 2, 'method': 'group_machine_verify', 'params': {
        'node_id': 'a' * 32, 'proof_account': 'agent', 'request_id': 'b' * 32, 'code': code}})
    assert replay['result'] == {'verified': False}


def test_an_unrelated_request_cannot_exhaust_the_current_challenge(tmp_path):
    code = group_proof.issue(tmp_path, 'a' * 32, 'agent', 'b' * 32)
    for _ in range(10):
        assert not group_proof.verify(tmp_path, 'a' * 32, 'agent', 'c' * 32, code)
    assert group_proof.verify(tmp_path, 'a' * 32, 'agent', 'b' * 32, code)


def test_public_command_does_not_read_enrollment_keys_or_configuration(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from acc.node import cli
    def forbidden(*args, **kwargs):
        raise AssertionError('local proof must not read node credentials')
    monkeypatch.setattr(cli, 'load_key', forbidden)
    monkeypatch.setattr(cli.NodeConfig, 'load', forbidden)
    monkeypatch.setattr(group_proof.pwd, 'getpwuid', lambda _: SimpleNamespace(pw_name='alex-agent'))
    assert cli.main(['group-proof', '--request', 'b' * 32, '--node-id', 'a' * 32, '--run-dir', str(tmp_path)]) == 0
    assert 'Confirmation code:' in capsys.readouterr().out


def test_special_files_fail_closed_without_blocking(tmp_path):
    import os
    os.mkfifo(tmp_path / '.group-proof.json', 0o600)
    with pytest.raises(ValueError, match='unsafe proof state'):
        group_proof.issue(tmp_path, 'a' * 32, 'agent', 'b' * 32)
