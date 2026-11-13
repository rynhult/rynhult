"""Fixture-only hung-supervisor fault; never remove production broker fencing."""
import asyncio
import os
import select
from pathlib import Path


def process_identity(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return fields[19], fields[0]


class SurvivingEnclosure:
    def __init__(self, pid, started, executable, directory):
        self.pid, self.started = pid, started
        self.directory = Path(directory)
        self.gate = self.directory / "qualification-hold"
        self.held = self.directory / "qualification-held"
        self.fd = os.pidfd_open(pid)
        self.child_fd = None
        self.paused = False
        try:
            assert process_identity(pid)[0] == started, 'Supervisor incarnation changed'
            children = Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
            matches = [int(child) for child in children
                if Path(f'/proc/{child}/exe').resolve(strict=True) == Path(executable).resolve(strict=True)]
            assert len(matches) == 1, 'One exact native child required'
            self.child = matches[0]
            self.child_fd = os.pidfd_open(self.child)
            self.child_started = process_identity(self.child)[0]
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        assert not select.select([self.fd, self.child_fd], [], [], 0)[0], 'Owned tree already exited'
        assert process_identity(self.pid)[0] == self.started
        assert process_identity(self.child)[0] == self.child_started
        assert process_identity(self.child)[1] not in ('T', 't', 'Z'), 'Native child must remain live'
        return {'supervisorPid': self.pid, 'nativePid': self.child, 'nativeStarted': self.child_started}

    async def pause(self):
        self.check()
        self.gate.write_text(str(self.pid)+':'+self.started)
        self.paused = True
        async with asyncio.timeout(3):
            while not self.held.exists():
                await asyncio.sleep(.01)
        assert self.held.read_text() == str(self.pid)+':'+self.started
        return self.check()

    def resume(self):
        if self.paused:
            self.gate.unlink(missing_ok=True)
            self.paused = False

    async def exited(self):
        async with asyncio.timeout(5):
            while not (select.select([self.fd], [], [], 0)[0]
                    and select.select([self.child_fd], [], [], 0)[0]):
                await asyncio.sleep(.01)

    def close(self):
        self.resume()
        if self.child_fd is not None:
            os.close(self.child_fd)
            self.child_fd = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def install_cleanup_delay(directory):
    # Embedded only in a disposable qualification launcher, before main().
    import os
    from pathlib import Path

    from acc.broker import relay_enclosure
    clock = relay_enclosure.time
    root = Path(directory)
    identity = str(os.getpid()) + ':' + relay_enclosure.incarnation(os.getpid())
    class DelayedClock:
        def __getattr__(self, name):
            return getattr(clock, name)
        def sleep(self, seconds):
            gate = root / 'qualification-hold'
            if gate.exists() and gate.read_text() == identity:
                temporary = root / 'qualification-held.tmp'
                temporary.write_text(identity)
                temporary.replace(root / 'qualification-held')
                while gate.exists():
                    clock.sleep(.01)
            clock.sleep(seconds)
    relay_enclosure.time = DelayedClock()


def inject_cleanup_delay(enclosure):
    import inspect
    script = enclosure.cli.read_text()
    needle = 'from acc.broker.relay_enclosure import main'
    assert script.count(needle) == 1
    source = inspect.getsource(install_cleanup_delay)
    script = script.replace(needle, source + '\ninstall_cleanup_delay(' + repr(str(enclosure.path)) + ')\n' + needle)
    enclosure.cli.chmod(0o700)
    try:
        enclosure.cli.write_text(script)
    finally:
        enclosure.cli.chmod(0o500)
