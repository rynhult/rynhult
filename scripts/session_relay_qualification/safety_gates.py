#!/usr/bin/env python3
"""Bounded relay regression runner and explicit R1 release-proof inventory.

A green fixture run cannot enable the pilot. Missing native/placement proofs are
reported as blocked and make this command fail, without an override flag.
"""
import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATES = json.loads(Path(__file__).with_name('gates.json').read_text())
BLOCKED = {key: gate['missing'] for key, gate in GATES.items() if gate['missing']}


def gate_results(xml):
    """Only tests present in this exact run count; skips never qualify a gate."""
    cases = list(ET.parse(xml).getroot().iter('testcase')) if Path(xml).exists() else []
    results = []
    for key, gate in GATES.items():
        modules = {}
        for module in gate['modules']:
            matching = [case for case in cases if case.get('classname', '').split('.')[-1] == module]
            modules[module] = 'passed' if matching and all(
                not any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped'))
                for case in matching) else 'incomplete'
        covered = all(status == 'passed' for status in modules.values())
        results.append({'id': key, 'status': 'fixture_passed' if covered else 'incomplete',
                        'modules': modules, 'release_status': 'blocked' if gate['missing'] or not covered else 'fixture_only',
                        'reason': gate['missing'], 'native_qualified': False})
    return results


def summarize(xml):
    root = ET.parse(xml).getroot()
    counts = {'passed': 0, 'failed': 0, 'errors': 0, 'skipped': 0}
    for case in root.iter('testcase'):
        outcome = ('failed' if case.find('failure') is not None else 'errors' if case.find('error') is not None
                   else 'skipped' if case.find('skipped') is not None else 'passed')
        counts[outcome] += 1
    return counts


def report(counts, *, command, xml, returncode):
    regression = 'passed' if returncode == 0 and counts['passed'] and not any(
        counts[key] for key in ('failed', 'errors', 'skipped')) else 'incomplete'
    return {'schema': 2, 'pilot_ready': False, 'full_suite_run': False,
            'regression': {'status': regression, 'counts': counts, 'command': command, 'xml': str(xml)},
            'proof_scope': 'Focused fixture regression and separately scoped isolated native component evidence. '
                           'Neither is automatic production native R1 qualification.',
            'required_proofs': gate_results(xml)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gate', choices=sorted(GATES), action='append', default=[])
    available = sorted({module for gate in GATES.values() for module in gate['modules']})
    parser.add_argument('--module', choices=available, action='append', default=[])
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    xml = output.with_suffix('.xml')
    log = output.with_suffix('.log')
    # Explicit related modules, never an unmanaged full test suite.
    selected = set(args.module) | {module for name in args.gate for module in GATES[name]['modules']}
    if not selected:
        selected = set(available)
    files = sorted('tests/' + module + '.py' for module in selected)
    command = [sys.executable, '-m', 'pytest', '-q', '-m', 'relay_native or not relay_native',
               *files, '--junitxml=' + str(xml)]
    xml.unlink(missing_ok=True)  # Never qualify a previous run after timeout or collection failure.
    with log.open('w') as stream:
        try:
            returncode = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, timeout=900).returncode
        except subprocess.TimeoutExpired:
            returncode = 124
    counts = summarize(xml) if xml.exists() else {'passed': 0, 'failed': 0, 'errors': 1, 'skipped': 0}
    evidence = report(counts, command=command, xml=xml, returncode=returncode)
    evidence['selection'] = {'gates': args.gate, 'modules': sorted(selected)}
    output.write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps({'pilot_ready': False, 'regression': evidence['regression']['status'],
                      'counts': counts, 'blocked_proofs': len(BLOCKED), 'report': str(output)}))
    return 2  # Missing native proofs remain a release failure even with green regressions.


if __name__ == '__main__':
    raise SystemExit(main())
