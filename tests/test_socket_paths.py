"""A Unix socket path longer than the kernel allows (107 bytes) fails at once with a message that names it, in the
broker, its client and the browser-test harness (which used to wait forever for a socket that could never appear)."""
import asyncio
import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

from acc.broker.server import MAX_SOCKET_PATH, serve
from acc.server.broker_client import BrokerClient, BrokerUnavailable

REPO = Path(__file__).resolve().parents[1]


def too_long(folder: Path) -> Path:
    path = folder / ("d" * max(1, MAX_SOCKET_PATH - len(os.fsencode(folder)))) / "broker.sock"
    assert len(os.fsencode(path)) > MAX_SOCKET_PATH
    return path


async def test_the_broker_refuses_an_over_long_socket_path_by_name(env, tmp_path):
    path = too_long(tmp_path)
    path.parent.mkdir()
    config = dataclasses.replace(env["broker"], socket_path=path)
    with pytest.raises(ValueError) as error:
        await asyncio.wait_for(serve(config), timeout=10)
    message = str(error.value)
    assert str(path) in message and f"at most {MAX_SOCKET_PATH} bytes" in message


async def test_the_client_names_an_over_long_socket_path_at_once(tmp_path):
    path = too_long(tmp_path)
    client = BrokerClient(path)
    with pytest.raises(BrokerUnavailable) as error:
        await asyncio.wait_for(client.call("ping"), timeout=10)
    assert str(path) in str(error.value) and "too long" in str(error.value)


def test_the_browser_harness_fails_fast_on_an_over_long_base(tmp_path):
    base = tmp_path / ("b" * 80)  # its broker socket is <base>/run-xxxxxxxx/broker.sock
    result = subprocess.run([sys.executable, str(REPO / "tests/e2e_server.py"), str(base), "18981"],
                            cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO / "src")},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert "ACC_E2E_BASE" in result.stderr and f"at most {MAX_SOCKET_PATH} bytes" in result.stderr, result.stderr
    assert not base.exists()  # refused before creating anything
