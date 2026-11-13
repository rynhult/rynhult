"""Native broker + real OS-main crash/restart, simulated nodes and real HA code."""
import asyncio
import json
from pathlib import Path

import pytest
from test_session_relay_node_runtime import HOST, NEXT, SID, node_runtime  # noqa: F401
from test_session_relay_process_restart import process_node  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelaySession, SessionRun

pytestmark = pytest.mark.relay_native


async def empty(c):
    for _ in range(500):
        value = await c.inspect('clean')
        if value['clean'] and not value['active']:
            return value
        await asyncio.sleep(.02)
    raise AssertionError(value)


@pytest.mark.parametrize('profile', ['empty', 'retained'])
@pytest.mark.parametrize('loss', ['leader-loss', 'node-loss'])
async def test_os_main_restart_requires_fencing_and_ha_promotion_before_native_recovery(process_node, loss, record_property, profile):  # noqa: F811
    c = process_node
    @write_intent
    def prepare():
        with c.db() as db:
            db.delete(db.get(RelaySession, SID))
            db.delete(db.get(SessionRun, c.launch.run_id))
            db.commit()
    prepare()
    # Parent is only the test observer. The main authority/runtime/typed node
    # channels below live in a different OS process inside the offline namespace.
    await c.inspect('main_start')
    first = await c.inspect('main:state')
    assert first['authority']['role'] == 'standby' and not first['runs']
    assert (await c.inspect('main:sql_write'))['fenced']
    assert (await c.inspect('main:launch_first'))['fenced']
    assert (await c.inspect('state'))['active'] == 0
    assert (await c.inspect('main:promote'))['authority']['epoch'] == 1
    launched = await c.inspect('main:launch_first')
    assert launched['fence']['hub_incarnation'] == first['main']
    assert launched['fence']['authority_epoch'] == 1
    old_native = await c.inspect('state')
    assert old_native['active'] == 1 and old_native['requests'] == 0
    retained = None
    if profile == 'retained':
        from relay_crash_retained import Retained
        await c.inspect('qualify')
        completed = await c.inspect('main:complete_turn')
        async with asyncio.timeout(20):
            while not (await c.inspect('idle'))['idle']:
                await asyncio.sleep(.02)
        retained = await Retained.observe(c, completed['run'], completed['operation'])
    operation = (await c.inspect('main:stage'))['operation']
    await c.inspect('track')
    killed = await c.inspect('main_kill')
    assert killed == {'killed': first['pid'], 'returncode': -9}
    await empty(c)
    # Persisted main state survives an actual SIGKILL; nobody inferred clean
    # rollback or replay from the broker's transport EOF.
    with c.db() as db:
        assert db.get(RelayOperation, operation).receipt['state'] == 'submitting'
    await c.inspect('main_start')
    restarted = await c.inspect('main:state')
    assert restarted['pid'] != first['pid'] and restarted['main'] != first['main']
    assert restarted['authority']['role'] == 'standby' and restarted['authority']['epoch'] == 1
    assert (await c.inspect('main:promote'))['fenced']  # no implicit old-main fencing
    assert (await c.inspect('main:sql_write'))['fenced']
    assert (await c.inspect('main:launch_fresh'))['fenced']
    assert (await c.inspect('state'))['active'] == 0
    promoted = await c.inspect('main:promote_fenced')
    assert promoted['authority']['epoch'] == 2 and promoted['authority']['role'] == 'active'
    assert (await c.inspect('main:stale_epoch_sql'))['fenced']
    assert (await c.inspect('main:state'))['authority']['role'] == 'active'
    await c.inspect('main:recover')
    with c.db() as db:
        assert db.get(RelaySession, SID).sealed
        assert db.get(RelayOperation, operation).receipt['state'] == 'uncertain'
    fresh = await c.inspect('main:launch_fresh')
    if retained is not None:
        from types import SimpleNamespace
        await retained.after_restart(SimpleNamespace(run_id=fresh['run']))
        retained.proof['scope'] = 'Actual OS-main SIGKILL/restart and pinned Codex; production HA epoch1->2 after independent fencing; simulated node; explicit fixture-reviewed fresh session; no physical-node/PostgreSQL/browser Launch claim'
        record_property('crash_retained', json.dumps(retained.proof))
    assert fresh['main'] == restarted['main']
    # Relay authority epoch is per enrollment; HA epoch is independently pinned
    # in the actual production placement identity, not fabricated as a run epoch.
    assert (await c.inspect('main:old_grant'))['refused']
    current = await c.inspect('state')
    assert current['runs'][0]['native'] != old_native['runs'][0]['native']
    await c.inspect('track')
    if loss == 'node-loss':
        assert (await c.inspect('main:node_loss'))['node_closed']
        await empty(c)
        assert (await c.inspect('main:launch_replacement'))['refused']
    demoted = await c.inspect('main:demote')
    assert demoted['authority']['role'] == 'standby' and demoted['authority']['epoch'] == 3
    if loss == 'leader-loss':
        await empty(c)
    assert (await c.inspect('main:sql_write'))['fenced']
    final = await c.inspect('state')
    assert final['active'] == 0 and final['requests'] == int(retained is not None)
    with c.db() as db:
        assert db.get(RelayOperation, operation).receipt['state'] == 'uncertain'
    evidence = {'loss': loss, 'old_pid': first['pid'], 'new_pid': restarted['pid'],
        'old_main': first['main'], 'new_main': restarted['main'], 'old_ha_epoch': 1, 'new_ha_epoch': 2,
        'final_ha_epoch': 3, 'requests': int(retained is not None), 'uncertain_retained': True,
        'scope': 'OS-main SIGKILL/wait + native broker/owner/TUI + production Authority/write guard/HA stop; simulated node EOF; SQLite HA oracle, no PostgreSQL/physical-node claim'}
    Path(f'/tmp/relay-g56-os-main-ha-{loss}.json').write_text(json.dumps(evidence, indent=2) + '\n')
    record_property('os_main_ha', json.dumps(evidence))
