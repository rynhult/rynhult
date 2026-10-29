"""Role defaults accept the WSL generation metadata already sent by nodes."""
import pytest

from acc.server.machine_presence import default_role


@pytest.mark.parametrize('generation', ['1', '2'])
def test_existing_wsl_metadata_defaults_to_workstation(generation):
    assert default_role({'os': 'linux', 'wsl': generation}) == 'workstation'
