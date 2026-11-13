"""Reap a fixture-owned process group inside the offline namespace only."""
import asyncio
import os
import signal


async def stop_group(process):
    if process is None:
        return
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            # Kernel exit may precede asyncio's child-watcher notification.
            # Still wait: absence of the group is not a reaping acknowledgement.
            pass
    await asyncio.wait_for(process.wait(), 5)
