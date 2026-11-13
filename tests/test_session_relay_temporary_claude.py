"""Native Claude TUI with generated settings only and enforced temporary writes."""
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_generated_claude_tui_never_mounts_host_settings_or_writes_outside_tmp(tmp_path, record_property):
    if not shutil.which('bwrap') or not Path('/usr/bin/claude').exists():
        pytest.skip('Installed pinned Claude and bubblewrap required; no fallback')
    before = {key:os.environ.get(key) for key in ('HOME', 'CLAUDE_CONFIG_DIR')}
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/temporary_claude_probe.py'
    spec = importlib.util.spec_from_file_location('temporary_claude_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.capture_temporary_claude_tui(tmp_path)
    assert {key:os.environ.get(key) for key in before} == before
    assert Path(result['hostWritesConfinedTo']).is_relative_to(tmp_path)
    assert result['confinement']['writableFileMounts'] == ['/tmp']
    assert result['confinement']['noHostAccountMounted'] and not result['protectedSettingsAccessed']
    assert all(item['exit'] == 0 and item['empty_tree'] for item in result['results'])
    assert result['tui']['completed'] and result['tui']['entered'] and result['tui']['empty_tree']
    assert not result['forbidden_file_exists']
    denied = [row for row in result['guard_events'] if row.get('guard', {}).get('rule') == 'custom']
    assert len(denied) == 2  # first print turn and the real native TUI turn
    assert result['generatedDisableHooksOverridden']
    record_property('temporary_claude_native', json.dumps(result, sort_keys=True))
