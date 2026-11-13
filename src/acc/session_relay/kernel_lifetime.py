"""Pinned Linux process lifetime, independent of inherited stdio pipe EOF."""
import asyncio
import os
import select
from pathlib import Path


class KernelLifetime:
    def __init__(self,pid):
        self.fd=os.pidfd_open(pid)
        try:self.started=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
        except BaseException:
            os.close(self.fd);self.fd=None;raise
        self.future=None;self.loop=None

    def alive(self):
        return self.fd is not None and not select.select([self.fd],[],[],0)[0]

    async def wait(self):
        if self.fd is None:raise ConnectionError('Process lifetime closed')
        if not self.alive():return
        if self.future is None:
            self.loop=asyncio.get_running_loop();self.future=self.loop.create_future()
            def ended():
                if self.fd is not None:self.loop.remove_reader(self.fd)
                if not self.future.done():self.future.set_result(None)
            self.loop.add_reader(self.fd,ended)
        await asyncio.shield(self.future)

    def close(self):
        if self.fd is None:return
        if self.loop is not None:self.loop.remove_reader(self.fd)
        os.close(self.fd);self.fd=None
        if self.future is not None and not self.future.done():self.future.cancel()


async def wait_root_exit(runtime):
    lifetime=getattr(runtime,'kernel_lifetime',None)
    if lifetime is None:return await runtime.process.wait()  # synthetic adapter owners
    await lifetime.wait()
    return runtime.process.returncode  # may still be None; never wait for pipe EOF
