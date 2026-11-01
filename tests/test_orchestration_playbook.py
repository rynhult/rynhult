"""The orchestration playbook quotes the v2 lead's default instructions; the code (planning.LEAD_INSTRUCTIONS) is the
source, and the copy must not drift from it."""
import re
from pathlib import Path

import pytest

from acc.server.orchestrator.planning import LEAD_INSTRUCTIONS

PLAYBOOK = Path(__file__).resolve().parents[1] / 'docs/ORCHESTRATION-PLAYBOOK.md'


def quoted_lead_instructions():
    section = PLAYBOOK.read_text().split('## Default lead instructions (for Orchestrator v2)', 1)[1]
    quoted = re.search(r'```text\n(.*?)\n```', section, re.S)
    assert quoted, 'the playbook quotes the instructions in a text block'
    return quoted.group(1)


def test_the_playbooks_default_lead_instructions_are_the_codes():
    assert quoted_lead_instructions() == LEAD_INSTRUCTIONS


@pytest.mark.parametrize('guard', [
    'offer Check image access, upload a usable reference, or an explicit skip',
    'never discard them',
    'otherwise offer a local merge/handoff',
    'Never invent/add a remote or merge main',
])
def test_playbook_retains_owner_handoff_and_reference_guards(guard):
    assert guard in quoted_lead_instructions()
    assert guard in LEAD_INSTRUCTIONS
