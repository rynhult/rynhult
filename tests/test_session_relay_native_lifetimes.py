"""G6 native owner/view/bridge churn through production main/node enrollment."""
import asyncio
import json
import time
from pathlib import Path

import pytest
from test_session_relay_node_runtime import HOST, SID, node_runtime  # noqa: F401
from test_session_relay_process_restart import process_node  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import Session

pytestmark = pytest.mark.relay_native


@write_intent
def reserve(c, index):
    identifier = f'{index:012x}'
    launch = c.manager.new_launch(c.resolve)
    with c.db() as db:
        template = db.get(Session, SID)
        db.add(Session(id=identifier, provider='codex', name='Native lifetime fixture', tmux_name='acc-' + identifier,
            launch_spec_id=template.launch_spec_id, run_as=template.run_as, owner=template.owner,
            team_host=HOST, workdir=template.workdir, run_dir=str(c.factory.config.run_dir / identifier)))
        db.flush()
        launch.reserve(db, db.get(Session, identifier))
        db.commit()
    return launch


async def cleaned(c):
    for _ in range(500):
        value = await c.inspect('clean')
        if value['clean']:
            return value
        await asyncio.sleep(.02)
    raise AssertionError(value)


@pytest.mark.parametrize('lifetimes', [3, pytest.param(2049, marks=pytest.mark.relay_long_native)],
                         ids=['smoke', '2049'])
async def test_native_lifetimes_reclaim_production_resources(process_node, lifetimes, record_property):  # noqa: F811
    c = process_node
    samples = []
    # Sequential bounded batches, at most eight separate session writers/owners.
    # One retained production factory/process spans the entire 2049 run count.
    for first in range(1, lifetimes + 1, 8):
        batch = []
        started = time.monotonic()
        try:
            for offset in range(first, min(first + 8, lifetimes + 1), 4):
                launches = [reserve(c, index) for index in range(offset, min(offset + 4, first + 8, lifetimes + 1))]
                batch.extend(launches)
                await asyncio.gather(*(launch.launch(launch.session_id) for launch in launches))
        except Exception:
            print(await c.inspect('debug'))
            raise
        launched_at = time.monotonic()
        active = await c.inspect('state')
        assert active['active'] == len(batch) and active['requests'] == 0
        assert len({run['owner'] for run in active['runs']}) == len(batch)
        assert len({run['view'] for run in active['runs']}) == len(batch)
        assert not c.owners  # prove fixture Owner was never used
        assert (await c.inspect('track'))['tracked'] == len(batch)
        tracked_at = time.monotonic()
        await asyncio.gather(*(launch.peer.close() for launch in batch))
        sample = await cleaned(c)
        assert sample['count'] == len(batch) and sample['active'] == 0
        await asyncio.gather(*tuple(c.manager.background))
        assert not c.manager.runs and not c.wire.link._channels and not c.wire.streams
        samples.append({key: sample[key] for key in ('fds', 'tasks', 'active', 'rss_kib', 'threads', 'cpus')})
        Path(f'/tmp/relay-g56-churn-progress-{lifetimes}.json').write_text(json.dumps(
            {'completed': first + len(batch) - 1, 'batch_seconds': round(time.monotonic() - started, 2),
                'launch_seconds': round(launched_at - started, 2), 'track_seconds': round(tracked_at - launched_at, 2),
                'samples': samples, 'latest': samples[-1]}))
    # Warm-up permits worker executor/asyncio infrastructure, not per-lifetime
    # growth. Bounds are independent of the selected lifetime count.
    baseline = samples[0]
    assert max(sample['fds'] for sample in samples) <= baseline['fds'] + 4
    assert max(sample['tasks'] for sample in samples) <= baseline['tasks'] + 2
    assert max(sample['rss_kib'] for sample in samples) <= baseline['rss_kib'] + 64 * 1024
    assert max(sample['threads'] for sample in samples) <= 33  # main + bounded default executor
    evidence = {'lifetimes': lifetimes, 'max_concurrent': 8, 'max_concurrent_launches': 4, 'samples': samples,
        'requests': (await c.inspect('state'))['requests'],
        'scope': 'pinned Codex owner/TUI, owned bridge, actual main and typed simulated node; no Claude/HA claim'}
    path = Path('/tmp') / f'relay-g56-native-lifetimes-{lifetimes}.json'
    path.write_text(json.dumps(evidence, indent=2) + '\n')
    record_property('native_lifetimes', json.dumps(evidence))
