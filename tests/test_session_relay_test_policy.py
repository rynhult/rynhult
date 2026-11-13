"""Default tests stay portable; explicit qualification keeps every native assertion."""
import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def marked(node):
    return any(ast.unparse(item) == 'pytest.mark.relay_native' for item in node.decorator_list)


def test_native_inventory_has_explicit_module_or_function_markers():
    inventory = json.loads((ROOT / 'tests/relay_native_inventory.json').read_text())['files']
    for filename, entry in inventory.items():
        tree = ast.parse((ROOT / filename).read_text())
        if entry['targets'] == ['*']:
            assert any(isinstance(node, ast.Assign) and ast.unparse(node.value) == 'pytest.mark.relay_native'
                       and any(isinstance(target, ast.Name) and target.id == 'pytestmark' for target in node.targets)
                       for node in tree.body), filename
        else:
            tests = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assert all(marked(tests[name]) for name in entry['targets']), filename


def collect(selector=None):
    # Exercise all marker forms and mixed fixture boundaries without recursively
    # recollecting the full relay suite three times during every default run.
    files = ['tests/test_session_relay_native_isolation.py', 'tests/test_session_relay_metadata_security.py',
             'tests/test_session_relay_installed_security.py', 'tests/test_session_relay_native_socket.py',
             'tests/test_session_relay_native_turn.py', 'tests/test_session_relay_runtime.py']
    command = [sys.executable, '-m', 'pytest', '--collect-only', '-q', *files]
    if selector is not None:
        command += ['-m', selector]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return {line.strip() for line in result.stdout.splitlines() if line.startswith('tests/') and '::' in line}


def test_default_native_and_explicit_gate_collection_are_disjoint_and_complete():
    ordinary = collect()
    native = collect('relay_native')
    explicit = collect('relay_native or not relay_native')
    assert ordinary and native and not ordinary.intersection(native)
    assert ordinary | native == explicit
    assert any('test_native_metadata_sandbox_has_no_egress' in node for node in ordinary)
    assert any('test_weaker_metadata_policy_refuses_before_native_launch' in node for node in ordinary)
    assert any('test_exclusive_stop_eof_and_cancelled_waiter' in node for node in ordinary)
    assert any('test_pinned_native_tui_uses_private_unix_websocket' in node for node in native)
    assert any('test_captured_stock_native_turn_has_no_actor_grant' in node for node in native)
    assert any('test_stock_envelope_projects_only_text' in node for node in native)
    assert any('test_security_a4_feature_off' in node for node in ordinary)


def test_explicit_safety_runner_selects_native_and_unit_tests_without_false_qualification(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('relay_safety_runner', ROOT / 'scripts/session_relay_qualification/safety_gates.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)  # no XML/evidence: must remain incomplete
    monkeypatch.setattr(runner.subprocess, 'run', run)
    output = tmp_path / 'qualification.json'
    monkeypatch.setattr(sys, 'argv', ['safety_gates.py', '--module', 'test_session_relay_installed_security', '--output', str(output)])
    assert runner.main() == 2
    assert commands[0][commands[0].index('-m', 3) + 1] == 'relay_native or not relay_native'
    assert 'tests/test_session_relay_installed_security.py' in commands[0]
    evidence = json.loads(output.read_text())
    assert evidence['regression']['status'] == 'incomplete'
    assert all(gate['native_qualified'] is False for gate in evidence['required_proofs'])
