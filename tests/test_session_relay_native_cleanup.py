"""Reproduce the observer's exit-before-child-watcher teardown race."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def cleanup():
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/process_cleanup.py'
    spec = importlib.util.spec_from_file_location('relay_fixture_cleanup', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_kernel_exit_before_asyncio_returncode_still_reaps(monkeypatch):
    module = cleanup()
    process = SimpleNamespace(pid=123, returncode=None, wait=AsyncMock(return_value=0))
    def already_gone(pid, signum):
        assert pid == process.pid
        raise ProcessLookupError()
    monkeypatch.setattr(module.os, 'killpg', already_gone)
    await module.stop_group(process)
    process.wait.assert_awaited_once()


async def test_reaped_observer_is_not_signalled_again(monkeypatch):
    module = cleanup()
    process = SimpleNamespace(pid=123, returncode=0, wait=AsyncMock(return_value=0))
    def unexpected(*args):
        raise AssertionError('An observed exit must not be signalled again')
    monkeypatch.setattr(module.os, 'killpg', unexpected)
    await module.stop_group(process)
    process.wait.assert_awaited_once()


async def test_cleanup_does_not_hide_other_signal_failures(monkeypatch):
    module = cleanup()
    process = SimpleNamespace(pid=123, returncode=None, wait=AsyncMock())
    def refused(*args):
        raise PermissionError()
    monkeypatch.setattr(module.os, 'killpg', refused)
    with pytest.raises(PermissionError):
        await module.stop_group(process)
