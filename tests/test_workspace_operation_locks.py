"""Lock ownership, cancellation and nonblocking reconcile acquisition."""
import asyncio

import pytest
from helpers import wait_for

from acc.server.workspace_operations import WorkspaceOperations


async def test_independent_paths_and_cancelled_waiter_cleanup(tmp_path):
    operations = WorkspaceOperations()
    path = str(tmp_path / 'one')
    entered, release = asyncio.Event(), asyncio.Event()
    called = []

    async def owner():
        entered.set()
        await release.wait()

    async def write():
        called.append(True)

    first = asyncio.create_task(operations.run(path, owner))
    await entered.wait()
    await operations.run(str(tmp_path / 'two'), write)
    queued = asyncio.create_task(operations.run(path, write))
    assert await wait_for(lambda: operations.pending(path) == 1)
    queued.cancel()
    # Cancellation of a waiter must not turn into a later destructive action.
    try:
        assert await wait_for(queued.done), 'a cancelled waiter must finish before its owner releases'
    finally:
        release.set()
        await first
        with pytest.raises(asyncio.CancelledError):
            await queued
    assert called == [True]
    assert not operations._entries


async def test_cancelled_owner_keeps_lock_until_operation_outcome(tmp_path):
    operations = WorkspaceOperations()
    path = str(tmp_path / 'one')
    entered, release, next_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def owner():
        entered.set()
        await release.wait()

    async def next_operation():
        assert release.is_set()
        next_started.set()

    first = asyncio.create_task(operations.run(path, owner))
    await entered.wait()
    first.cancel()
    second = asyncio.create_task(operations.run(path, next_operation))
    try:
        assert await wait_for(lambda: operations.pending(path) == 1)
        assert not next_started.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
    assert next_started.is_set() and not operations._entries


async def test_nonblocking_acquisition_skips_busy_path_and_normalizes_aliases(tmp_path):
    operations = WorkspaceOperations()
    path = str(tmp_path / 'one')
    async with operations.hold(path):
        async with operations.hold(str(tmp_path / 'child/../one'), wait=False) as held:
            assert held is False
    async with operations.hold(path, wait=False) as held:
        assert held is True
    assert not operations._entries


async def test_failed_operation_releases_lock_and_machine_keys_are_separate(tmp_path):
    operations = WorkspaceOperations()
    path = str(tmp_path / 'one')

    async def failure():
        raise ValueError('operation refused')

    with pytest.raises(ValueError, match='operation refused'):
        await operations.run(path, failure)
    assert not operations._entries
    async with operations.hold(path):
        async with operations.hold(path, 'another-machine', wait=False) as held:
            assert held is True
    assert not operations._entries


def test_keys_are_lexical_and_never_probe_the_server_filesystem(monkeypatch):
    from pathlib import Path

    def forbidden(*args, **kwargs):
        raise AssertionError('workspace keys must not inspect account-owned paths')

    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr(Path, 'stat', forbidden)
    monkeypatch.setattr(Path, 'lstat', forbidden)
    assert WorkspaceOperations.key('/home/member/child/../project') == ('local', '/home/member/project')
    assert WorkspaceOperations.key('/home/member/project', 'node') == ('node', '/home/member/project')


async def test_ancestor_checks_only_track_deletions_and_separate_machines(tmp_path):
    operations = WorkspaceOperations()
    root = str(tmp_path / 'parent')
    child = root + '/child'
    for kind in ['launch', 'resume', 'convert', 'other']:
        async with operations.hold(root, kind=kind):
            assert operations.busy(root)
            assert not operations.other_busy(child)
            assert not operations.busy_ancestor(child)
    async with operations.hold(root, kind='delete'):
        assert operations.busy_ancestor(child) and operations.other_busy(child)
        assert not operations.busy_ancestor(root + '-sibling')
        assert not operations.busy_ancestor(child, 'node')
        assert not operations.other_busy(child, 'node')
    assert not operations._entries


async def test_cancelled_queued_delete_clears_destructive_admission(tmp_path):
    operations = WorkspaceOperations()
    path = str(tmp_path / 'parent')

    async def never():
        raise AssertionError('cancelled delete must never run')

    async with operations.hold(path, kind='launch'):
        queued = asyncio.create_task(operations.run(path, never, kind='delete'))
        assert await wait_for(lambda: operations.pending(path) == 1)
        assert operations.busy_ancestor(path + '/child')
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert not operations.busy_ancestor(path + '/child')
        assert operations.busy(path)
    assert not operations._entries
