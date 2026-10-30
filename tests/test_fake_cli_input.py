"""Real fake-provider subprocesses: keep stdin open as a terminal attachment does."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import wait_for

FAKE = Path(__file__).with_name('fake_cli.py')


@pytest.fixture
def fake_process(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    run = tmp_path / 'run'
    run.mkdir()
    with subprocess.Popen([sys.executable, str(FAKE), '--session-id', 'input-fixture'],
                          cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          env={**os.environ, 'HOME': str(home), 'ACC_RUN_DIR': str(run), 'FAKE_HOOK': '/usr/bin/true'}) as process:
        try:
            yield process, run
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


async def test_coalesced_lines_are_all_received_without_another_write_or_eof(fake_process):
    process, run = fake_process
    payload = 'first notice\nsecond notice\nreview é then 日本 done\n'.encode()
    process.stdin.write(payload)
    process.stdin.flush()
    # Closing stdin here would hide TextIOWrapper read-ahead behind EOF readiness.
    await wait_for(lambda: (run / 'fake.received').exists()
                   and (run / 'fake.received').read_bytes() == payload, timeout=5)
    assert process.poll() is None


async def test_a_partial_line_never_blocks_provider_control(fake_process):
    process, run = fake_process
    process.stdin.write(b'not submitted')
    process.stdin.flush()
    await wait_for(lambda: (run / 'fake.argv.jsonl').exists())
    (run / 'fake.ctl').write_text('exit')
    await wait_for(lambda: process.poll() is not None, timeout=5)
    assert process.returncode == 0
    assert not (run / 'fake.received').exists()
