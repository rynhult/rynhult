"""Rename audit for the RC (docs/handoffs/2026-10-27-59d81fe838ef-rename-rc-audit.md): owner decisions 3 and 5."""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def setup_dry_run(tmp_path, accd):
    env = {**os.environ, 'ACC_SETUP_TEST_ACCD': str(accd), 'ACC_OWNER': 'nobody', 'ACC_ZONE': 'example.com',
           'CF_API_TOKEN': 'EXAMPLE', 'ACC_AGENT_CLIS': 'codex', 'ACC_OLLAMA': 'no', 'ACC_DOCKER': 'no', 'SUDO_USER': ''}
    env.pop('ACC_HOSTNAME', None)
    result = subprocess.run(['bash', str(REPO / 'deploy/setup.sh'), '--yes', '--dry-run'], capture_output=True,
                            text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


# Decision 3: a new install with a zone defaults to rynhult.<zone>.
def test_a_new_install_defaults_to_rynhult_in_its_zone(tmp_path):
    out = setup_dry_run(tmp_path, tmp_path / 'none')
    assert 'ACC_ALLOWED_ORIGINS=https://rynhult.example.com' in out and 'https://acc.example.com' not in out


# ...and an existing install keeps the name it has.
def test_an_existing_install_keeps_its_name(tmp_path):
    accd = tmp_path / 'accd'
    accd.write_text('ACC_ALLOWED_ORIGINS=https://acc.example.com\nACC_WILDCARD_ZONE=example.com\n'
                    'ACC_SERVICES_ACCOUNT=nobody\n')
    out = setup_dry_run(tmp_path, accd)
    assert 'keeping' in out and 'https://rynhult.example.com' not in out


def test_the_wizard_offers_the_same_default():
    assert '"${ACC_ZONE:+rynhult.$ACC_ZONE}"' in (REPO / 'deploy/acc-install').read_text()


# Decision 2: new device authorities are named Rynhult device CA; an existing ACC device CA keeps working untouched.
def signer():
    import importlib.util
    spec = importlib.util.spec_from_file_location('acc_device_ca_rename', REPO / 'deploy/device-ca/acc-device-ca.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def common_name(pem):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    cert = x509.load_pem_x509_certificate(pem if isinstance(pem, bytes) else pem.encode())
    return cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value


def issuer_name(pem):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    return x509.load_pem_x509_certificate(pem.encode()).issuer.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value


def test_a_new_device_authority_and_a_staged_successor_are_named_rynhult(tmp_path):
    import time
    ca = signer()
    os.chmod(tmp_path, 0o700)
    state = tmp_path / 'state'
    ca.initialize(state)
    assert common_name((state / 'ca.pem').read_bytes()) == 'Rynhult device CA'
    time.sleep(1.1)
    ca.rollover_stage(state)
    assert common_name((state / 'next' / 'ca.pem').read_bytes()).startswith('Rynhult device CA ')


def test_an_existing_acc_device_ca_keeps_issuing_and_its_devices_keep_verifying_across_a_rollover(tmp_path):
    import time

    from cryptography import x509
    ca = signer()
    os.chmod(tmp_path, 0o700)
    state, helper = tmp_path / 'state', tmp_path / 'etc'
    ca.initialize(state, 'ACC device CA')  # an authority provisioned before the rename
    old_pem = (state / 'ca.pem').read_bytes()
    before = ca.issue(state, {'operation': 'issue', 'device_id': 'a' * 32, 'days': 90})['certificate']
    assert common_name(old_pem) == 'ACC device CA' and issuer_name(before) == 'ACC device CA'
    x509.load_pem_x509_certificate(before.encode()).verify_directly_issued_by(x509.load_pem_x509_certificate(old_pem))
    # The rename never rewrites it: it still issues under its own name.
    again = ca.issue(state, {'operation': 'issue', 'device_id': 'b' * 32, 'days': 90})['certificate']
    assert issuer_name(again) == 'ACC device CA' and (state / 'ca.pem').read_bytes() == old_pem
    # A planned rollover gets a Rynhult-named successor, and the earlier devices still verify against the old root.
    time.sleep(1.1)
    ca.rollover_stage(state)
    helper.mkdir()
    (helper / ca.EDGE_CURRENT).write_bytes((state / 'next' / 'ca.pem').read_bytes())
    (helper / ca.EDGE_PREVIOUS).write_bytes(old_pem)
    ca.rollover_promote(state, helper)
    after = ca.issue(state, {'operation': 'issue', 'device_id': 'c' * 32, 'days': 90})['certificate']
    assert issuer_name(after).startswith('Rynhult device CA ')
    assert (state / 'previous' / 'ca.pem').read_bytes() == old_pem
    x509.load_pem_x509_certificate(before.encode()).verify_directly_issued_by(x509.load_pem_x509_certificate(old_pem))
