"""Abrupt subprocess crash barriers across both real durable stores; fake provider."""
import subprocess
import sys
from pathlib import Path

import pytest
from relay_helpers import setup
from sqlalchemy import select

from acc.broker.session_relay import AdmissionJournal
from acc.server.models import RelayOperation
from acc.server.session_relay.maintenance import recover

PHASES = ('before_registration', 'after_registration', 'after_main_claim', 'after_broker_claim',
          'after_provider_admission', 'after_acknowledgement', 'after_receipt_commit')


@pytest.mark.parametrize('phase', PHASES)
def test_abrupt_process_crash_at_each_admission_and_receipt_boundary(tmp_path, phase):
    case = setup(tmp_path)
    source = Path(__file__).resolve().parents[1]
    worker = source / 'tests/fixtures/session_relay/crash/worker.py'
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    result = subprocess.run([sys.executable, str(worker), str(tmp_path), phase], capture_output=True,
        timeout=20, env={'PATH': '/usr/bin:/bin', 'HOME': str(home), 'ACC_TEST': '1',
                         'PYTHONPATH': str(source / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
    try:
        assert result.returncode == 73, result.stderr.decode()
        recover(case.ctx)
        with case.db() as db:
            operations = db.scalars(select(RelayOperation)).all()
            if phase == 'before_registration':
                assert not operations
            else:
                assert len(operations) == 1
                operation = operations[0]
                expected = 'queued' if phase == 'after_registration' else 'accepted' if phase == 'after_receipt_commit' else 'uncertain'
                assert operation.receipt['state'] == expected
                if phase == 'after_registration':
                    assert not operation.confirmed
                if phase in PHASES[3:]:
                    journal = AdmissionJournal(tmp_path / 'journal', clock=lambda: 1000)
                    try:
                        before = journal.lookup(operation.id)
                        assert before is not None
                        if phase in PHASES[5:]:
                            assert before[0] == 'accepted'
                        else:
                            assert before[1] is None
                    finally:
                        journal.close()
        # Recovery does not perform a provider write, including the no-admission
        # crash immediately after the broker claim. No retry of an unknown claim.
        log = tmp_path / 'provider-admissions'
        admissions = log.read_text().splitlines() if log.exists() else []
        assert len(admissions) == int(phase in PHASES[4:])
        assert 'ANTHROPIC_API_KEY' not in result.stdout.decode()
    finally:
        case.engine.dispose()


def test_release_inventory_cannot_promote_a_green_fixture_run():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/safety_gates.py'
    spec = importlib.util.spec_from_file_location('relay_safety_gates', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    evidence = module.report({'passed': 1000, 'failed': 0, 'errors': 0, 'skipped': 0},
                             command=['fixture'], xml='fixture.xml', returncode=0)
    assert evidence['regression']['status'] == 'passed'
    assert evidence['pilot_ready'] is False
    assert len(evidence['required_proofs']) >= 14
    assert all(proof['status'] == 'incomplete' for proof in evidence['required_proofs'])
    assert all(proof['release_status'] == 'blocked' for proof in evidence['required_proofs'])


def test_r1_gate_manifest_covers_design_and_existing_test_modules():
    import json
    root = Path(__file__).resolve().parents[1]
    gates = json.loads((root / 'scripts/session_relay_qualification/gates.json').read_text())
    assert set(gates) == {'A1', 'A2', 'A3-A6-A8', 'A4-A7-A9', 'A5-A10', 'F4', 'F5-F6', 'F7',
                          'authority', 'private-placement', 'frozen-security', 'decisions',
                          'bounded-redacted-storage', 'duplicates-crashes', 'unsupported-controls',
                          'selector', 'claude-L1'}
    for gate in gates.values():
        assert gate['modules']
        for module in gate['modules']:
            assert (root / 'tests' / (module + '.py')).is_file()


def test_gate_evidence_requires_every_mapped_module_and_rejects_skips(tmp_path):
    import importlib.util
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/safety_gates.py'
    spec = importlib.util.spec_from_file_location('relay_gate_evidence', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    xml = tmp_path / 'evidence.xml'
    names = module.GATES['A4-A7-A9']['modules']
    def write(skipped=False):
        xml.write_text('<testsuite>' + ''.join(
            '<testcase classname="tests.' + name + '" name="case">' +
            ('<skipped/>' if skipped and name == names[0] else '') + '</testcase>'
            for name in names) + '</testsuite>')
    write()
    result = {gate['id']: gate for gate in module.gate_results(xml)}
    assert result['A4-A7-A9']['status'] == 'fixture_passed'
    assert result['A1']['status'] == 'incomplete'
    assert not any(gate['native_qualified'] for gate in result.values())
    write(skipped=True)
    assert {gate['id']: gate for gate in module.gate_results(xml)}['A4-A7-A9']['status'] == 'incomplete'


def test_gate_runner_timeout_discards_old_xml_and_writes_incomplete_report(tmp_path, monkeypatch):
    import importlib.util
    import json
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/safety_gates.py'
    spec = importlib.util.spec_from_file_location('relay_gate_timeout', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / 'report.json'
    output.with_suffix('.xml').write_text('<testsuite><testcase classname="old" name="pass"/></testsuite>')
    monkeypatch.setattr(sys, 'argv', ['safety_gates.py', '--output', str(output)])
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
    monkeypatch.setattr(module.subprocess, 'run', timeout)
    assert module.main() == 2
    evidence = json.loads(output.read_text())
    assert evidence['regression']['status'] == 'incomplete'
    assert evidence['regression']['counts']['errors'] == 1
    assert all(gate['status'] == 'incomplete' for gate in evidence['required_proofs'])
    assert not evidence['pilot_ready']


def test_gate_runner_selects_only_explicit_related_modules_and_stays_blocked(tmp_path, monkeypatch):
    import importlib.util
    import json
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/safety_gates.py'
    spec = importlib.util.spec_from_file_location('relay_focused_gate_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / 'focused.json'
    monkeypatch.setattr(sys, 'argv', ['safety_gates.py', '--module', 'test_session_relay_native_ingress', '--output', str(output)])
    def passed(command, **kwargs):
        assert [item for item in command if item.startswith('tests/')] == ['tests/test_session_relay_native_ingress.py']
        xml = Path(next(item.split('=', 1)[1] for item in command if item.startswith('--junitxml=')))
        xml.write_text('<testsuite><testcase classname="tests.test_session_relay_native_ingress" name="case"/></testsuite>')
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(module.subprocess, 'run', passed)
    assert module.main() == 2
    evidence = json.loads(output.read_text())
    assert evidence['selection']['modules'] == ['test_session_relay_native_ingress']
    assert evidence['regression']['status'] == 'passed' and evidence['pilot_ready'] is False
    assert all(gate['status'] == 'incomplete' and not gate['native_qualified'] for gate in evidence['required_proofs'])


def test_gate_selection_cannot_add_unrelated_test_files(tmp_path, monkeypatch):
    import importlib.util
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/safety_gates.py'
    spec = importlib.util.spec_from_file_location('relay_selected_gate_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, 'argv', ['safety_gates.py', '--gate', 'duplicates-crashes', '--output', str(tmp_path / 'gate.json')])
    def timeout(command, **kwargs):
        assert {item for item in command if item.startswith('tests/')} == {
            'tests/' + name + '.py' for name in module.GATES['duplicates-crashes']['modules']}
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(module.subprocess, 'run', timeout)
    assert module.main() == 2
