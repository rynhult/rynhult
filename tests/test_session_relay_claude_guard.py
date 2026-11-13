"""Installed guard execution is pinned, secret-minimal and fail-closed."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from acc.broker.claude_guard import InstalledGuard


@pytest.mark.parametrize('fault', ['none', 'deny', 'stdout', 'missing', 'writable', 'changed', 'unsafe_mount'])
async def test_installed_guard_contract(tmp_path, monkeypatch, fault):
    # Synthetic filesystem attestation; actual read-only bind is proven by native harness.
    if fault != 'unsafe_mount':
        monkeypatch.setattr('acc.broker.claude_guard.os.statvfs', lambda _: SimpleNamespace(f_flag=os.ST_RDONLY))
    path = tmp_path / 'acc-guard'
    path.write_text('#!/usr/bin/python3 -I\nimport os\nassert "ANTHROPIC_API_KEY" not in os.environ\n' +
        ('raise SystemExit(2)\n' if fault == 'deny' else 'print("unrecognized")\n' if fault == 'stdout' else ''))
    path.chmod(0o500)
    (tmp_path / 'acc_env.py').write_text('# synthetic settings reader\n')
    (tmp_path / 'acc_env.py').chmod(0o400)
    if fault == 'unsafe_mount':
        # Model account-owned code even when a VM runner itself is root. A
        # root-owned deployment is deliberately trusted on a writable mount.
        original_stat = Path.stat
        def account_owned(candidate, *args, **kwargs):
            value = original_stat(candidate, *args, **kwargs)
            return SimpleNamespace(st_uid=1001, st_mode=value.st_mode) if candidate == path else value
        monkeypatch.setattr(Path, 'stat', account_owned)
    if fault == 'missing':
        path.unlink()
    elif fault == 'writable':
        path.chmod(0o777)
    guard = InstalledGuard(tmp_path / 'relay-hook')
    if fault == 'changed':
        path.chmod(0o600)
        path.write_text('#!/usr/bin/python3 -I\n')
        path.chmod(0o500)
    assert await guard.check({'tool_name': 'Bash', 'tool_input': {'command': 'echo artificial'}},
        {'PATH': '/usr/bin:/bin', 'ANTHROPIC_API_KEY': 'synthetic-not-a-key'}, str(tmp_path)) is (fault == 'none')


async def test_guard_dependency_change_during_execution_refuses(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr('acc.broker.claude_guard.os.statvfs', lambda _: SimpleNamespace(f_flag=os.ST_RDONLY))
    path = tmp_path / 'acc-guard'
    path.write_text('#!/usr/bin/python3 -I\n')
    path.chmod(0o500)
    reader = tmp_path / 'acc_env.py'
    reader.write_text('# original settings reader\n')
    reader.chmod(0o400)
    guard = InstalledGuard(tmp_path / 'relay-hook')
    original = asyncio.create_subprocess_exec
    async def changed(*args, **kwargs):
        process = await original(*args, **kwargs)
        reader.chmod(0o600)
        reader.write_text('# replacement reader\n')
        reader.chmod(0o400)
        return process
    monkeypatch.setattr('acc.broker.claude_guard.asyncio.create_subprocess_exec', changed)
    assert not await guard.check({'tool_name': 'Bash', 'tool_input': {'command': 'echo artificial'}}, {}, str(tmp_path))


async def test_cancelled_guard_is_reaped_before_cancellation_returns(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr('acc.broker.claude_guard.os.statvfs', lambda _: SimpleNamespace(f_flag=os.ST_RDONLY))
    path = tmp_path / 'acc-guard'
    path.write_text('#!/usr/bin/python3 -I\nimport time\ntime.sleep(30)\n')
    path.chmod(0o500)
    (tmp_path / 'acc_env.py').write_text('# synthetic settings reader\n')
    (tmp_path / 'acc_env.py').chmod(0o400)
    guard = InstalledGuard(tmp_path / 'relay-hook')
    original = asyncio.create_subprocess_exec
    created = asyncio.Event()
    processes = []
    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        created.set()
        return process
    monkeypatch.setattr('acc.broker.claude_guard.asyncio.create_subprocess_exec', capture)
    task = asyncio.create_task(guard.check({'tool_name': 'Bash'}, {}, str(tmp_path)))
    await asyncio.wait_for(created.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(processes) == 1 and processes[0].returncode is not None
