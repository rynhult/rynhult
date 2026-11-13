"""B3 first test: the agent's own Bash must not read broker private state."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_agent_own_bash_read_characterization_and_relay_write_exclusion(record_property):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('private_read',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='codex_private_read')
    record_property('private_read',json.dumps(proof,sort_keys=True))
    assert proof['agentOwnBashReadRelay'],proof  # read access is real; disk tokens cannot be secrets
    assert not proof['agentOwnBashWroteRelay'] and proof['writableRootsExcludeRelay'],proof
    assert proof['effectiveSandbox']['type']=='workspaceWrite',proof
    assert proof['effectiveSandbox']['excludeSlashTmp'] and proof['effectiveSandbox']['excludeTmpdirEnvVar']
