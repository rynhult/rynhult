"""Pinned real Claude decoder/drain component; enforced offline namespace."""
import importlib.util
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_native_claude_partial_draft_exact_decoder_and_conclusive_drain(record_property):
    if not shutil.which('bwrap') or not Path('/usr/bin/claude').exists():
        pytest.skip('Installed pinned Claude and bubblewrap required; no fallback')
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/probe.py'
    spec = importlib.util.spec_from_file_location('claude_draft_native_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.capture_claude_resume(enclosure=True, tui=True, draft=True)
    tui = result['tui']
    assert tui['completed'] and tui['empty_tree'] and tui['exit'] == 0
    assert tui['partial_draft_refused'] and tui['decoder_only_refused']
    assert tui['provider_evidence_kind'] == 'provider_admission'
    assert tui['provider_ref_contains_native_message'] is True
    assert tui['positive_native_empty'] is True
    assert tui['drained'] == {'epoch': 2, 'admitted': 2, 'processed': 2}
    assert tui['terminalMediated'] is False
    assert all(stage['empty_tree'] for stage in result['results'])
    assert all(stage['output']['session_id'] == '11111111-1111-4111-8111-111111111111' for stage in result['results'])
    record_property('native_draft_scope', tui['receipt_scope'])
    record_property('transcript_flush_waits', tui['transcript_flush_waits'])
    record_property('transcript_flush_snapshots', tui['transcript_flush_snapshots'])
