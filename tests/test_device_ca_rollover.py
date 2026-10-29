"""Planned device-authority rollover: a new CA beside the old one, without a public maintenance window.

Compromise response stays the maintenance-window procedure (Device-Pairing runbook): this is for a scheduled key
change. The edge's leaf allow list stays the gate throughout; a second CA only ever narrows to listed leaves.
"""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from test_device_ca import helper as signer
from test_device_ca_machine import csr, recovery

from acc import remote_access_helper as rh


def pem(path):
    return (path / 'ca.pem').read_text()


def staged(tmp_path):
    """An authority with one browser leaf, a staged successor, and helper files installed in the required order."""
    ca = signer()
    state, helper_dir = tmp_path / 'authority', tmp_path / 'helper'
    ca.initialize(state)
    helper_dir.mkdir()
    old = ca.issue(state, {'operation': 'issue', 'device_id': 'a' * 32, 'days': 90})['certificate']
    old_ca = pem(state)
    ca.rollover_stage(state)
    (helper_dir / 'device-ca.pem').write_text(pem(state / 'next'))
    (helper_dir / 'device-ca-previous.pem').write_text(old_ca)
    return ca, state, helper_dir, old, old_ca


def fp(ca_pem):
    return x509.load_pem_x509_certificate(ca_pem.encode()).fingerprint(hashes.SHA256()).hex()


def events(state):
    return [json.loads(line) for line in (state / 'rollover.log').read_text().splitlines()]


def issuer_of(leaf, ca_pem):
    try:
        x509.load_pem_x509_certificate(leaf.encode()).verify_directly_issued_by(x509.load_pem_x509_certificate(ca_pem.encode()))
        return True
    except (ValueError, TypeError, Exception):
        return False


def test_stage_promote_finish_switches_issuance_and_keeps_old_leaves_verifiable(tmp_path):
    ca, state, helper_dir, old, old_ca = staged(tmp_path)
    new_ca = pem(state / 'next')
    assert new_ca != old_ca
    # A distinct subject keeps the edge's chain building unambiguous with two trusted authorities.
    assert x509.load_pem_x509_certificate(new_ca.encode()).subject != x509.load_pem_x509_certificate(old_ca.encode()).subject
    # Staging alone changes nothing: issuance still uses the current authority.
    assert issuer_of(ca.issue(state, {'operation': 'issue', 'device_id': 'b' * 32, 'days': 90})['certificate'], old_ca)
    assert ca.issue(state, {'operation': 'authority'}) == {'certificate': old_ca, 'fingerprint': fp(old_ca),
                                                           'previous': None, 'previous_key': False}
    ca.rollover_promote(state, helper_dir)
    assert pem(state) == new_ca and pem(state / 'previous') == old_ca and not (state / 'next').exists()
    # Nothing issues from the old key after promote and there is no abort path: it is deleted now, not at finish.
    assert not (state / 'previous' / 'ca.key').exists()
    assert ca.issue(state, {'operation': 'authority'}) == {'certificate': new_ca, 'fingerprint': fp(new_ca),
                                                           'previous': fp(old_ca), 'previous_key': False}
    assert [(e['event'], e['authority'], e.get('previous'), e.get('previous_key_deleted')) for e in events(state)] == [
        ('stage', fp(new_ca), None, None), ('promote', fp(new_ca), fp(old_ca), True)]
    fresh = ca.issue(state, {'operation': 'issue', 'device_id': 'c' * 32, 'days': 90})['certificate']
    machine = ca.issue(state, {'operation': 'issue_machine', 'node_id': 'd' * 32, 'csr': csr()[1], 'days': 30})['certificate']
    assert issuer_of(fresh, new_ca) and issuer_of(machine, new_ca) and issuer_of(old, old_ca)
    ca.rollover_promote(state, helper_dir)  # idempotent once done
    with pytest.raises(ValueError, match='previous authority'):
        ca.rollover_finish(state, helper_dir)  # the edge still trusts it
    (helper_dir / 'device-ca-previous.pem').unlink()
    ca.rollover_finish(state, helper_dir)
    assert not (state / 'previous').exists() and pem(state) == new_ca
    assert ca.issue(state, {'operation': 'authority'})['previous'] is None
    assert [e['event'] for e in events(state)] == ['stage', 'promote', 'finish']
    with pytest.raises(ValueError, match='nothing'):
        ca.rollover_finish(state, helper_dir)


@pytest.mark.parametrize('installed', ['missing', 'swapped', 'stale'])
def test_promote_requires_the_edge_to_trust_both_authorities_first(tmp_path, installed):
    ca, state, helper_dir, _, old_ca = staged(tmp_path)
    if installed == 'missing':
        (helper_dir / 'device-ca-previous.pem').unlink()
    elif installed == 'swapped':
        (helper_dir / 'device-ca.pem').write_text(old_ca)
        (helper_dir / 'device-ca-previous.pem').write_text(pem(state / 'next'))
    else:
        (helper_dir / 'device-ca.pem').write_text(old_ca)
    before = sorted(p.name for p in state.rglob('*'))
    with pytest.raises(ValueError, match='edge'):
        ca.rollover_promote(state, helper_dir)
    assert sorted(p.name for p in state.rglob('*')) == before and pem(state) == old_ca


def test_an_interrupted_promote_fails_closed_and_resumes(tmp_path):
    ca, state, helper_dir, _, old_ca = staged(tmp_path)
    new_ca = pem(state / 'next')
    # Death after the current authority moved aside, before the staged one moved in.
    (state / 'previous').mkdir(mode=0o700)
    for name in ('ca.key', 'ca.pem'):
        os.rename(state / name, state / 'previous' / name)
    with pytest.raises(ValueError):
        ca.issue(state, {'operation': 'issue', 'device_id': 'e' * 32, 'days': 90})  # no authority: refuse, never guess
    ca.rollover_promote(state, helper_dir)
    assert pem(state) == new_ca and pem(state / 'previous') == old_ca and not (state / 'next').exists()
    assert not (state / 'previous' / 'ca.key').exists()


def test_a_promote_interrupted_before_the_old_key_was_deleted_deletes_it_when_run_again(tmp_path):
    ca, state, helper_dir, _, old_ca = staged(tmp_path)
    # Death after the swap completed, before the old key was deleted.
    (state / 'previous').mkdir(mode=0o700)
    for name in ('ca.key', 'ca.pem'):
        os.rename(state / name, state / 'previous' / name)
        os.rename(state / 'next' / name, state / name)
    os.rmdir(state / 'next')
    assert ca.issue(state, {'operation': 'authority'})['previous_key'] is True
    ca.rollover_promote(state, helper_dir)
    assert not (state / 'previous' / 'ca.key').exists() and pem(state / 'previous') == old_ca
    assert ca.issue(state, {'operation': 'authority'})['previous_key'] is False
    assert events(state)[-1]['event'] == 'promote' and events(state)[-1]['previous_key_deleted'] is True


def test_rollover_refuses_unsafe_or_out_of_order_states(tmp_path):
    ca, state, helper_dir, _, _ = staged(tmp_path)
    with pytest.raises(ValueError, match='staged'):
        ca.rollover_stage(state)  # one staged authority at a time
    with pytest.raises(ValueError, match='nothing'):
        ca.rollover_finish(state, helper_dir)
    os.chmod(state / 'next', 0o755)
    with pytest.raises(ValueError):
        ca.rollover_promote(state, helper_dir)
    os.chmod(state / 'next', 0o700)
    ca.rollover_promote(state, helper_dir)
    with pytest.raises(ValueError, match='previous'):
        ca.rollover_stage(state)  # finish the last rollover first
    with pytest.raises(ValueError, match='invalid signing request'):
        ca.issue(state, {'operation': 'authority', 'extra': 1})


def test_recovery_tool_runs_the_rollover_steps(tmp_path, monkeypatch):
    tool = recovery()
    ca, state, helper_dir, _, _ = staged(tmp_path)
    shutil.rmtree(state / 'next')
    for name in ('device-ca.pem', 'device-ca-previous.pem'):
        (helper_dir / name).unlink()
    fingerprint = tool.run(['rollover', 'stage'], state=state, helper=helper_dir)
    assert len(fingerprint) == 64
    with pytest.raises(ValueError, match='edge'):
        tool.run(['rollover', 'promote'], state=state, helper=helper_dir)
    (helper_dir / 'device-ca.pem').write_text(pem(state / 'next'))
    (helper_dir / 'device-ca-previous.pem').write_text(pem(state))
    assert tool.run(['rollover', 'promote'], state=state, helper=helper_dir) == fingerprint
    (helper_dir / 'device-ca-previous.pem').unlink()
    assert tool.run(['rollover', 'finish'], state=state, helper=helper_dir) == fingerprint
    with pytest.raises(ValueError, match='usage'):
        tool.run(['rollover', 'other'], state=state, helper=helper_dir)


# The edge helper: two trusted authorities, only ever narrowed to listed leaves.

def edge_helper(directory, cas):
    directory.mkdir(parents=True, mode=0o700)
    base = {'admin': {'listen': 'unix//run/caddy/admin.sock'}, 'apps': {'http': {'servers': {'main': {
        'listen': [':443'], 'tls_connection_policies': [{'match': {'sni': ['acc.example.lan']}}], 'routes': []}}}}}
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    return rh.RemoteAccessHelper(fd, {'qualified': True, 'hostnames': ['remote.example.com'], 'server': 'main'},
                                 base, cas, lambda action, config: True, owner=os.getuid()), fd


def trusted(config):
    return config['apps']['http']['servers']['main']['tls_connection_policies'][0]['client_authentication']['trusted_ca_certs']


def test_edge_trusts_only_authorities_that_issued_a_listed_leaf(tmp_path):
    ca, state, helper_dir, old, old_ca = staged(tmp_path)
    ca.rollover_promote(state, helper_dir)
    new_ca = pem(state)
    fresh = ca.issue(state, {'operation': 'issue', 'device_id': 'c' * 32, 'days': 90})['certificate']
    stranger, other = signer(), tmp_path / 'other'
    stranger.initialize(other)
    foreign = stranger.issue(other, {'operation': 'issue', 'device_id': 'f' * 32, 'days': 90})['certificate']
    subject, fd = edge_helper(tmp_path / 'edge', [new_ca, old_ca])
    single, single_fd = edge_helper(tmp_path / 'single', new_ca)
    listed = single.render('remote.example.com', [fresh])
    try:
        assert len(trusted(subject.render('remote.example.com', [old, fresh]))) == 2
        # Once no listed leaf comes from the old authority, it drops out of the trust pool on the next apply.
        assert rh.revision(subject.render('remote.example.com', [fresh])) == rh.revision(listed)
        assert len(trusted(subject.render('remote.example.com', []))) == 1  # no leaves: the current one only
        with pytest.raises(Exception):
            subject.render('remote.example.com', [fresh, foreign])
        with pytest.raises(Exception):
            single.render('remote.example.com', [old])  # without the previous file, old leaves are refused
    finally:
        os.close(fd)
        os.close(single_fd)


def test_edge_refuses_more_than_two_authorities_or_a_duplicate(tmp_path):
    ca = signer()
    pems = []
    for index in range(3):
        ca.initialize(tmp_path / f'ca{index}')
        pems.append(pem(tmp_path / f'ca{index}'))
    for name, cas in (('three', pems), ('duplicate', [pems[0], pems[0]])):
        subject, fd = edge_helper(tmp_path / name, cas)
        try:
            with pytest.raises(rh.Refused, match='authorities'):
                subject.render('remote.example.com', [])
        finally:
            os.close(fd)


def test_two_authorities_in_disposable_caddy_admit_only_listed_leaves_and_retire_cleanly(tmp_path):
    """Old and new identities both work during the rollover; neither authority admits an unlisted leaf."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    from remote_access_qualification.harness import CADDY, Lab
    from test_remote_access_machines import apply
    from test_remote_access_readiness import disposable_edge, issue_client
    if not Path(CADDY).exists():
        pytest.skip('needs /usr/bin/caddy')
    ca, state, helper_dir, _, _ = staged(tmp_path)
    with Lab() as lab:
        old, old_ca = issue_client(ca, state, lab, 'old', 'b' * 32)
        ca.rollover_promote(state, helper_dir)
        new, new_ca = issue_client(ca, state, lab, 'new', 'c' * 32)
        unlisted, _ = issue_client(ca, state, lab, 'unlisted', 'd' * 32)
        assert new_ca != old_ca
        with disposable_edge(lab, tmp_path / 'edge', [new_ca, old_ca]) as edge:
            apply(edge, [old, new])
            assert lab.request('old') == 200 and lab.request('new') == 200
            before = lab.count
            assert lab.request('unlisted') is None  # chains to a trusted authority, but isn't listed
            assert lab.count == before
            # The old device renewed: its leaf leaves the list and the old authority leaves the pool.
            apply(edge, [new])
            for tls in ('TLSv1_2', 'TLSv1_3'):
                assert lab.request('old', tls=tls) is None
            assert lab.count == before
            assert lab.request('new') == 200


# accd: which identities still come from the previous authority, and machines rotate off it on their own.

async def test_accd_rotates_machines_and_flags_devices_left_on_the_previous_authority(api, env, monkeypatch, tmp_path):
    from datetime import timedelta

    from helpers import write_db
    from test_passkeys import setup

    from acc.server import auth, devices, edge
    from acc.server.models import Device, Node, Setting, User
    await setup(api, env, monkeypatch)
    ca, state, helper_dir, old, _ = staged(tmp_path)
    monkeypatch.setattr(devices, 'sign', lambda body: ca.issue(state, body))
    old_machine = ca.issue(state, {'operation': 'issue_machine', 'node_id': 'e' * 32, 'csr': csr()[1], 'days': 30})['certificate']

    def row(pem, **fields):
        cert = x509.load_pem_x509_certificate(pem.encode())
        return Device(id=devices.fingerprint(cert)[:32], label='Example', status='active', certificate_pem=pem,
                      fingerprint=devices.fingerprint(cert), serial=str(cert.serial_number),
                      expires_at=auth.now() + timedelta(days=20), **fields)
    with write_db(api.app.state.db) as s:
        person = s.query(User).filter_by(role='owner').first().username
        s.add(Node(id='e' * 32, name='pc', status='active'))
        s.add(row(old, kind='browser', person=person))
        s.add(row(old_machine, kind='node', person='', node_id='e' * 32))
        s.merge(Setting(key='remote_access', value={'machine_identities': True}))
        s.commit()
    ctx = api.app.state.ctx
    presented = devices.fingerprint(x509.load_pem_x509_certificate(old_machine.encode()))

    async def snapshot():
        listed = (await api.get('/api/devices')).json()['devices']
        status = (await api.get('/api/remote-access')).json()['rollover']
        machine = next(n for n in (await api.get('/api/nodes')).json() if n['id'] == 'e' * 32)['machine_identity']
        with api.app.state.db() as s:
            due = edge.needs_identity(s, 'e' * 32, presented, ctx.edge.authority())
        return [d['previous_authority'] for d in listed], status, machine['previous_authority'], due
    # Before promote: nothing is on a previous authority; a 20-day machine leaf isn't due.
    assert await snapshot() == ([False], {'authority': ca.issue(state, {'operation': 'authority'})['fingerprint'],
                                          'previous_identities': 0}, False, False)
    ca.rollover_promote(state, helper_dir)
    ctx.edge.forget_authority()  # the 60-second cache, as after a minute
    flags, status, machine, due = await snapshot()
    assert flags == [True] and machine is True and due is True  # the machine asks for a successor now
    assert status == {'authority': ca.issue(state, {'operation': 'authority'})['fingerprint'], 'previous_identities': 2}
    # A signer that can't answer: no rollover action and no false alarms.
    monkeypatch.setattr(devices, 'sign', lambda body: (_ for _ in ()).throw(OSError('signer down')))
    ctx.edge.forget_authority()
    flags, status, machine, due = await snapshot()
    assert flags == [False] and machine is False and due is False and status == {'authority': None, 'previous_identities': 0}


async def accd_on_staged_rollover(api, env, monkeypatch, tmp_path):
    from test_passkeys import setup

    from acc.server import devices
    await setup(api, env, monkeypatch)
    ca, state, helper_dir, old, old_ca = staged(tmp_path)
    monkeypatch.setattr(devices, 'sign', lambda body: ca.issue(state, body))
    return api.app.state.ctx, ca, state, helper_dir, old_ca


def audit(api, prefix):
    from acc.server.models import AuditLog
    with api.app.state.db() as s:
        return [(r.action, r.detail) for r in s.query(AuditLog).order_by(AuditLog.id) if r.action.startswith(prefix)]


async def test_accd_audits_an_authority_change_and_the_deleted_previous_key_once(api, env, monkeypatch, tmp_path):
    from acc.server import devices, edge
    ctx, ca, state, helper_dir, old_ca = await accd_on_staged_rollover(api, env, monkeypatch, tmp_path)
    new_ca = pem(state / 'next')
    ctx.edge.authority()
    assert audit(api, 'device_ca.') == []  # the first answer is the baseline
    ca.rollover_promote(state, helper_dir)
    ctx.edge.forget_authority()
    ctx.edge.authority()
    changed = [('device_ca.authority_changed', {'from': fp(old_ca), 'to': fp(new_ca), 'planned': True}),
               ('device_ca.previous_key_deleted', {'previous': fp(old_ca)})]
    assert audit(api, 'device_ca.') == changed
    ctx.edge.forget_authority()
    ctx.edge.authority()
    edge.Edge(ctx).authority()  # a restarted accd: the last answer is kept in the database
    assert audit(api, 'device_ca.') == changed
    (helper_dir / 'device-ca-previous.pem').unlink()
    ca.rollover_finish(state, helper_dir)
    ctx.edge.forget_authority()
    ctx.edge.authority()
    assert audit(api, 'device_ca.')[2:] == [('device_ca.previous_retired', {'previous': fp(old_ca)})]
    # An authority that didn't come from a planned rollover (a restore, a replaced directory) is flagged as such.
    other = tmp_path / 'other'
    ca.initialize(other)
    monkeypatch.setattr(devices, 'sign', lambda body: ca.issue(other, body))
    ctx.edge.forget_authority()
    ctx.edge.authority()
    assert audit(api, 'device_ca.')[3:] == [('device_ca.authority_changed', {'from': fp(new_ca), 'to': fp(pem(other)),
                                                                              'planned': False})]
    assert not any('PRIVATE' in str(detail) for _, detail in audit(api, ''))


async def test_rollover_reissues_are_capped_below_the_signer_budget(api, env, monkeypatch, tmp_path):
    import asyncio
    from datetime import timedelta

    from helpers import write_db

    from acc.server import auth, devices, edge
    from acc.server.models import Device, Node, Setting
    ctx, ca, state, helper_dir, _ = await accd_on_staged_rollover(api, env, monkeypatch, tmp_path)
    nodes, presented = [format(i + 1, '032x') for i in range(edge.ROLLOVER_PER_HOUR + 2)], {}
    with write_db(api.app.state.db) as s:
        for node_id in nodes:
            cert_pem = ca.issue(state, {'operation': 'issue_machine', 'node_id': node_id, 'csr': csr()[1], 'days': 30})['certificate']
            cert = x509.load_pem_x509_certificate(cert_pem.encode())
            presented[node_id] = devices.fingerprint(cert)
            s.add(Node(id=node_id, name='pc' + node_id[-2:], status='active'))
            s.add(Device(id=presented[node_id][:32], label='pc', kind='node', person='', node_id=node_id, status='active',
                         certificate_pem=cert_pem, fingerprint=presented[node_id], serial=str(cert.serial_number),
                         expires_at=auth.now() + timedelta(days=20)))
        s.merge(Setting(key='remote_access', value={'machine_identities': True}))
        s.commit()
    ca.rollover_promote(state, helper_dir)
    ctx.edge.forget_authority()
    assert edge.ROLLOVER_PER_HOUR <= ca.ISSUE_LIMIT // 2  # people keep at least half the hour for pairing and renewal

    async def step(node_id):
        return (await asyncio.to_thread(ctx.edge.step, node_id, presented[node_id]))[0]
    assert [await step(n) for n in nodes] == ['request'] * edge.ROLLOVER_PER_HOUR + ['wait'] * 2
    assert await step(nodes[0]) == 'request'  # asking the same machine again reuses its share
    assert len(audit(api, 'machine_identity.rollover_request')) == edge.ROLLOVER_PER_HOUR
    # A machine that was asked gets its successor; one that wasn't (an unsolicited CSR) doesn't spend the budget.
    await asyncio.to_thread(edge.issue, ctx, nodes[0], csr()[1], presented[nodes[0]])
    with pytest.raises(ValueError):
        await asyncio.to_thread(edge.issue, ctx, nodes[-1], csr()[1], presented[nodes[-1]])
    # Expiry is never held back by the rollover share.
    with write_db(api.app.state.db) as s:
        s.get(Device, presented[nodes[-1]][:32]).expires_at = auth.now() + timedelta(days=5)
        s.commit()
    assert await step(nodes[-1]) == 'request'
    await asyncio.to_thread(edge.issue, ctx, nodes[-1], csr()[1], presented[nodes[-1]])
    assert [d['reason'] for a, d in audit(api, 'machine_identity.issue')] == ['rollover', 'expiry']


@pytest.mark.parametrize('path', ['/api/devices', '/api/devices?mine=true', '/api/remote-access', '/api/nodes'])
async def test_listing_after_authority_cache_expiry_persists_rollover_audit(api, env, monkeypatch, tmp_path, path):
    """GETs must not hold a read snapshot while the authority refresh audits a rollover."""
    import asyncio

    from helpers import pause_supervisor, write_db

    from acc.server import devices
    from acc.server.db import _transaction_gate
    from acc.server.models import Device, Setting, User

    ctx, ca, state, helper_dir, _ = await accd_on_staged_rollover(api, env, monkeypatch, tmp_path)
    await pause_supervisor(api)
    # The merged native schema/registry must not move refresh back inside the
    # listing snapshot. Include a real previous-authority native leaf in every GET.
    certificate = ca.issue(state, {'operation': 'issue_native', 'device_id': 'f' * 32,
                                   'csr': csr()[1], 'days': 90})['certificate']
    cert = x509.load_pem_x509_certificate(certificate.encode())
    with write_db(api.app.state.db) as s:
        s.add(Device(id='f' * 32, kind='native', person=s.query(User).first().username,
                     label='Phone', status='active', certificate_pem=certificate,
                     fingerprint=devices.fingerprint(cert), serial=str(cert.serial_number),
                     expires_at=cert.not_valid_after_utc, installation_id='f' * 32,
                     platform='android', key_hash='a' * 64))
        s.commit()
    # Prime the old authority, then make the next GET refresh after a real temporary-CA rollover.
    await asyncio.to_thread(ctx.edge.authority)
    ca.rollover_promote(state, helper_dir)
    expected = ca.issue(state, {'operation': 'authority'})['fingerprint']
    ctx.edge.forget_authority()
    # Keep the real SQLite admission gate, but fail a deadlock quickly rather than sleeping 15 seconds.
    gate = _transaction_gate(api.app.state.db.kw['bind'])
    enter = gate.enter
    monkeypatch.setattr(gate, 'enter', lambda worker, timeout: enter(worker, min(timeout, .1)))
    response = await api.get(path)
    assert response.status_code == 200, response.text
    with api.app.state.db() as s:
        assert s.get(Setting, 'device_ca_authority').value['fingerprint'] == expected
    changes = audit(api, 'device_ca.authority_changed')
    assert len(changes) == 1 and changes[0][1]['to'] == expected
    # Another refresh observes the same authority without duplicating the durable transition.
    ctx.edge.forget_authority()
    assert (await api.get(path)).status_code == 200
    assert audit(api, 'device_ca.authority_changed') == changes



def test_native_leaves_keep_proxy_assertion_and_dual_authority_validation(tmp_path):
    ca, state, helper_dir, _, old_ca = staged(tmp_path)
    old = ca.issue(state, {'operation': 'issue_native', 'device_id': 'a' * 32,
                           'csr': csr()[1], 'days': 90})['certificate']
    ca.rollover_promote(state, helper_dir)
    fresh = ca.issue(state, {'operation': 'issue_native', 'device_id': 'b' * 32,
                             'csr': csr()[1], 'days': 90})['certificate']
    subject, fd = edge_helper(tmp_path / 'edge-native', [pem(state), old_ca])
    try:
        with pytest.raises(rh.Refused, match='native_edge_not_provisioned'):
            subject.render('remote.example.com', [old, fresh])
        subject.policy['native_edge_key'] = 'ab' * 32
        config = subject.render('remote.example.com', [old, fresh])
        assert len(trusted(config)) == 2
        rendered = json.dumps(config)
        assert 'X-Acc-Native-Edge' in rendered and 'ab' * 32 in rendered
        assert '/api/mobile/v1/claim*' in rendered
        assert len(trusted(subject.render('remote.example.com', [fresh]))) == 1
    finally:
        os.close(fd)
