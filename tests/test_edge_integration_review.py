"""Edge integration review (docs/security/2026-10-27-edge-integration-review.md): one test per fixed finding."""
import importlib.util
import os
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[1]


def signer():
    spec = importlib.util.spec_from_file_location('acc_device_ca_ei', ROOT / 'deploy/device-ca/acc-device-ca.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def staged_rollover(tmp_path):
    ca = signer()
    os.chmod(tmp_path, 0o700)
    state, helper = tmp_path / 'state', tmp_path / 'etc'
    ca.initialize(state)
    helper.mkdir()
    old_pem = (state / 'ca.pem').read_bytes()
    time.sleep(1.1)  # a distinct staged authority
    ca.rollover_stage(state)
    new_pem = (state / 'next' / 'ca.pem').read_bytes()
    (helper / ca.EDGE_CURRENT).write_bytes(new_pem)
    (helper / ca.EDGE_PREVIOUS).write_bytes(old_pem)
    return ca, state, helper, old_pem, new_pem


def machine_request():
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(key, hashes.SHA256())
           .public_bytes(serialization.Encoding.PEM).decode())
    return {'operation': 'issue_machine', 'node_id': '0123456789abcdef', 'csr': csr, 'days': 30}


# EI-1: a signer request racing `rollover promote` read the old key and then the new certificate, and signed a leaf
# that neither authority verifies (every later apply of the edge's leaf list then failed). EI-5's lock now keeps
# promote out of an issuance; this checks the signer's own refusal underneath it, without the lock.
def test_ei_1_a_promote_between_the_key_and_certificate_reads_is_refused(tmp_path, monkeypatch):
    ca, state, helper, _, _ = staged_rollover(tmp_path)
    original, fired = ca.private_read, []

    def promote_after_the_key_is_read(directory, name):
        data = original(directory, name)
        if name == 'ca.key' and Path(directory) == state and not fired:
            fired.append(True)
            ca.rollover_promote.__wrapped__(state, helper)  # root, concurrently
        return data
    monkeypatch.setattr(ca, 'private_read', promote_after_the_key_is_read)
    csr = machine_request()['csr']
    with pytest.raises(ValueError, match='changing'):
        ca.issue.__wrapped__(state, machine_request())
    assert fired
    # The next request signs under the promoted authority, and the leaf verifies against it.
    monkeypatch.setattr(ca, 'private_read', original)
    leaf = x509.load_pem_x509_certificate(
        ca.issue(state, {**machine_request(), 'csr': csr})['certificate'].encode())
    leaf.verify_directly_issued_by(x509.load_pem_x509_certificate((state / 'ca.pem').read_bytes()))


# EI-5: rollover promote takes the issuance lock, so a promote started during an issuance waits for it to finish.
def test_ei_5_promote_waits_for_an_issuance_in_progress(tmp_path, monkeypatch):
    import threading
    ca, state, helper, old_pem, new_pem = staged_rollover(tmp_path)
    original, promote = ca.private_read, []

    def start_promote_after_the_key_is_read(directory, name):
        data = original(directory, name)
        if name == 'ca.key' and Path(directory) == state and not promote:
            promote.append(threading.Thread(target=ca.rollover_promote, args=(state, helper)))
            promote[0].start()
            promote[0].join(1.0)  # unlocked, promote finishes here, between this issuance's two reads
        return data
    monkeypatch.setattr(ca, 'private_read', start_promote_after_the_key_is_read)
    leaf = x509.load_pem_x509_certificate(ca.issue(state, machine_request())['certificate'].encode())
    promote[0].join(10)
    assert not promote[0].is_alive()
    leaf.verify_directly_issued_by(x509.load_pem_x509_certificate(old_pem))  # the whole issuance was the old one
    assert (state / 'ca.pem').read_bytes() == new_pem  # and the promote then ran


# EI-5: a linked trust file is refused by promote and finish, as the edge helper refuses it, never read as absent.
@pytest.mark.parametrize('step', ['promote', 'finish'])
def test_ei_5_a_linked_edge_trust_file_is_refused(tmp_path, step):
    ca, state, helper, old_pem, _ = staged_rollover(tmp_path)
    if step == 'finish':
        ca.rollover_promote(state, helper)
    (helper / ca.EDGE_PREVIOUS).unlink()
    (tmp_path / 'elsewhere.pem').write_bytes(old_pem)
    (helper / ca.EDGE_PREVIOUS).symlink_to(tmp_path / 'elsewhere.pem')
    with pytest.raises(ValueError, match='unsafe'):
        getattr(ca, 'rollover_' + step)(state, helper)
    assert (state / ('next' if step == 'promote' else 'previous')).exists()  # nothing moved


# EI-3: turning a project's backups off ended its open public pushes only at the next 15-second sweep.
from test_public_git_backup_policy import FRESH, LEAF, prepare  # noqa: E402,F401


async def test_ei_3_turning_project_backups_off_closes_its_public_pushes_at_once(api, project):
    from helpers import write_db

    from acc.server.edge import Connection
    from acc.server.models import Node
    prepare(api, project)
    with write_db(api.app.state.db) as db:
        db.get(Node, FRESH).public_git_backup = True
        db.commit()
    edge = api.app.state.ctx.edge
    push = Connection(LEAF, f"/api/git/{project['id']}.git/git-receive-pack")
    control = Connection(LEAF, '/api/nodes/connect')
    edge.open.update([push, control])
    try:
        await edge.sweep()
        assert not push.closed.is_set()  # admitted while the project backs up
        response = await api.post(f"/api/projects/{project['id']}/backup", json={'enabled': False})
        assert response.status_code == 200, response.text
        assert push.closed.is_set() and not control.closed.is_set()
    finally:
        edge.open.difference_update([push, control])
