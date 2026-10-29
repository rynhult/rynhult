"""User-facing refusal formatting preserves messages and status, not wire diagnostics."""
import pytest

from acc.server.api.context import Context
from acc.server.broker_client import BrokerError, BrokerUnavailable, MachineOffline
from acc.server.supervisor_common import LaunchRefused


@pytest.mark.parametrize(('error', 'status', 'message'), [
    (BrokerError('OpError: Choose a project with a commit.'), 502, 'Choose a project with a commit.'),
    (LaunchRefused('BrokerError: OpError: Choose a project with a commit.'), 409, 'Choose a project with a commit.'),
    (BrokerError('ValueError: Invalid launch option.'), 502, 'Invalid launch option.'),
    (BrokerError('acc.broker.ops.OpError: Choose another folder.'), 502, 'Choose another folder.'),
    (MachineOffline('MachineOffline: demo-node is offline.'), 409, 'demo-node is offline.'),
    (BrokerUnavailable('BrokerError: Service not connected.'), 503,
     "The agent account's session service isn't answering: Service not connected."),
    (LaunchRefused('Cannot reach demo-node (OpError: Service not connected).'), 409,
     'Cannot reach demo-node (Service not connected).'),
    (BrokerError('GitHub: repository unavailable.'), 502, 'GitHub: repository unavailable.'),
    (BrokerError('Cannot read internal: project settings.'), 502, 'Cannot read internal: project settings.'),
    (BrokerError('A file is labelled ValueError: notes.'), 502, 'A file is labelled ValueError: notes.'),
    (BrokerError('internal: RuntimeError'), 502, "The session service couldn't complete the request. Try again."),
])
def test_refused_boundary_hides_exception_types(error, status, message):
    refusal = Context.refused(error)
    assert refusal.status_code == status
    assert refusal.detail == message
