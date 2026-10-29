"""Additional equivalent API payload spellings for SR3-03; strings are never executed."""
import pytest
from test_security_review_2 import guard


@pytest.mark.parametrize('command', [
    'gh api -X PUT "$ENDPOINT"',
    'gh api graphql --input - <<\'EOF\'\n{"query":"mutation { \\u006dergePullRequest(input:{pullRequestId:\\"x\\"}){clientMutationId}}"}\nEOF',
])
def test_merge_payload_spellings(command):
    verdict, error = guard.decide({'workspace': '/srv/ws', 'rules': {'merge_pr': True}, 'custom': []},
                                 None, 'Bash', {'command': command}, '/srv/ws', '')
    assert error is None
    assert verdict and verdict[0] == 'merge_pr'


def test_command_local_alias_configuration_cannot_hide_merge(tmp_path):
    (tmp_path / 'config.yml').write_text('aliases:\n  finish-review: pr merge --squash\n')
    command = f'GH_CONFIG_DIR={tmp_path} gh finish-review 12'
    verdict, error = guard.decide({'workspace': '/srv/ws', 'rules': {'merge_pr': True}, 'custom': []},
                                 None, 'Bash', {'command': command}, '/srv/ws', '')
    assert error is None
    assert verdict and verdict[0] == 'merge_pr'
