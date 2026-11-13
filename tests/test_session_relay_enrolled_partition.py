"""Real enrolled node/main/broker/native lab, with an actual offline TCP cut."""
import importlib.util
import json
import shutil
import sysconfig
from pathlib import Path

import pytest


@pytest.mark.relay_native
@pytest.mark.parametrize('mode', ['node-partition', 'leader-loss', 'node-replacement'])
def test_enrolled_native_placement_expires_without_broker_only_authority(mode, record_property):
    root = Path(__file__).resolve().parents[1]
    if not shutil.which('bwrap') or not Path('/usr/bin/codex').exists():
        pytest.skip('Installed bubblewrap and pinned Codex required; no fallback')
    if not Path(sysconfig.get_path('purelib')).resolve().is_relative_to(root / '.acc'):
        pytest.skip('Self-contained locked project test environment required; no host dependency mounts')
    path = root / 'scripts/session_relay_qualification/partition_probe.py'
    spec = importlib.util.spec_from_file_location('enrolled_partition_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.capture_enrolled_partition(mode)
    assert result['scenario'] == mode
    for key in ('oneTimeEnrollment', 'authenticatedNode', 'productionRelay', 'productionNodeClient', 'verifiedTLS', 'badCARefused', 'socketReplacementRefused', 'nativeReadBeforePartition',
                'brokerRefreshDidNotRenewAuthority', 'postPartitionAdmissionRefused', 'beforePartitionOperationVerified', 'withdrawnAccountRefused', 'changedEnrollmentKeyRefused',
                'oldKeyHandshakeRefused', 'lateOldFrameRefused', 'operationNeverReplayed', 'nativeOwnerStopped', 'runSealed'):
        assert result[key] is True, key
    assert result['account'] == 'relayagent' and result['leaderEpoch'] == 1
    assert result['terminalMediated'] is False
    if mode == 'node-partition':
        assert result['discardedBytes'] > 0
        assert 25 <= result['brokerExpiredSeconds'] <= 40
        assert result['authorityExpiredSeconds'] >= result['brokerExpiredSeconds']
    if mode == 'node-replacement':
        assert result['freshNodeCannotReviveOldEnrollment'] is True
    record_property('offline_enrolled_partition', json.dumps(result, sort_keys=True))


@pytest.mark.parametrize('closed_at', ['send', 'receive', 'accepted_result'])
async def test_refusal_probe_handles_retirement_before_send_without_hiding_success(closed_at):
    from websockets.exceptions import ConnectionClosed

    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/enrolled_partition.py'
    spec = importlib.util.spec_from_file_location('partition_refusal_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Reader:
        async def send(self, _frame):
            if closed_at == 'send':
                raise ConnectionClosed(None, None)
        async def recv(self):
            if closed_at == 'receive':
                raise ConnectionClosed(None, None)
            return '{"result":{"thread":{"id":"native"}}}'
    if closed_at == 'accepted_result':
        with pytest.raises(AssertionError):
            await module.require_refused_read(Reader(), 'native')
    else:
        await module.require_refused_read(Reader(), 'native')
