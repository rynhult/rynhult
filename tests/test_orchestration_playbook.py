"""The orchestration playbook quotes the v2 lead's default instructions; the code (planning.LEAD_INSTRUCTIONS) is the
source, and the copy must not drift from it."""
import re
from pathlib import Path

from acc.server.orchestrator.planning import LEAD_INSTRUCTIONS

PLAYBOOK = Path(__file__).resolve().parents[1] / 'docs/ORCHESTRATION-PLAYBOOK.md'


def test_the_playbooks_default_lead_instructions_are_the_codes():
    section = PLAYBOOK.read_text().split('## Default lead instructions (for Orchestrator v2)', 1)[1]
    quoted = re.search(r'```text\n(.*?)\n```', section, re.S)
    assert quoted, 'the playbook quotes the instructions in a text block'
    assert quoted.group(1) == LEAD_INSTRUCTIONS
