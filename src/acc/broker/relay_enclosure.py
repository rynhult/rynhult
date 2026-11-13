"""One native owner, one process group and Linux subreaper; no privileged service.

The supervisor alone forks the CLI. After it exits, orphaned descendants (even
setsid/double-fork children) are adopted here. Only waitpid ECHILD permits an
empty-tree receipt. Missing receipt, supervisor death or a stuck child is unknown.
"""
from __future__ import annotations

import ctypes
import hmac
import json
import os
import secrets
import select
import signal
import stat
import sys
import time
from pathlib import Path

from acc.session_relay import protocol as p

from .session_relay import private_dir


def _read(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 8192:
            raise ValueError('Unsafe enclosure record')
        return json.loads(os.read(fd, 8193))
    finally:
        os.close(fd)


def _write(directory, name, value):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        os.write(fd, p.canonical_json(value))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory)


def incarnation(pid):
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


class Enclosure:
    def __init__(self, root, spec, executable, *, lifetime=None):
        self.path = root / ('owner-' + spec.run_id)
        self.fd = private_dir(self.path)
        self.run_id, self.token = spec.run_id, secrets.token_hex(32)
        self.identity = os.fstat(self.fd)
        from .relay_receipts import ReceiptKeys
        self.receipt_keys=None
        try:
            self.receipt_keys=ReceiptKeys(self.run_id,self.token,self.fd)
            _write(self.fd, 'manifest.json', {'runId': self.run_id, 'token': self.token, 'executable': str(executable), 'receiptSocket':self.receipt_keys.address,
                **({'lifetime': lifetime} if lifetime is not None else {})})
            # Isolated interpreter, trusted package root; no project/cwd import.
            source = str(Path(__file__).resolve().parents[2])
            script = (f'#!{sys.executable} -I\nimport sys\nsys.path.insert(0, {source!r})\n'
                      f'from acc.broker.relay_enclosure import main\nraise SystemExit(main({str(self.path)!r}, sys.argv[1:]))\n')
            fd = os.open('cli', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o500, dir_fd=self.fd)
            try:
                os.write(fd, script.encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(self.fd)
        except BaseException:
            if self.receipt_keys is not None:self.receipt_keys.close()
            os.close(self.fd)
            self.fd = None
            raise
        self.cli = self.path / 'cli'

    def _verified_record(self, name, identity):
        try:
            current = self.path.stat(follow_symlinks=False)
            if ((current.st_dev, current.st_ino) != (self.identity.st_dev, self.identity.st_ino)
                    or current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) != 0o700):
                return None
            record = _read(self.fd, name)
            if not isinstance(record,dict):return None
            pid, started = identity.split(':')[:2]
            valid = (record.get('runId') == self.run_id and isinstance(record.get('token'),str) and hmac.compare_digest(record['token'],self.token)
                    and record.get('pid') == int(pid) and record.get('started') == started
                    and self.receipt_keys.verify(name,record,int(pid),started)
                    and record.get('subreaper') is True
                    and (name != 'exit.json' or record.get('empty') is True))
            return record if valid else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def proof(self, name, identity):
        return self._verified_record(name, identity) is not None

    def native_status(self, identity):
        record = self._verified_record('exit.json', identity)
        value = record.get('status') if record is not None else None
        return value if type(value) is int and -64 <= value <= 255 else None

    def close(self):
        self.receipt_keys.close()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def main(directory, args):
    fd = private_dir(Path(directory))
    manifest = _read(fd, 'manifest.json')
    if args in (['-v'], ['--version']):
        # SDK metadata subprocess is not an owner and cannot consume its slot.
        try:
            os.mkdir('metadata-home', mode=0o700, dir_fd=fd)
        except FileExistsError:
            pass
        info = os.stat('metadata-home', dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Unsafe metadata home')
        os.execve(manifest['executable'], [manifest['executable'], *args],
                  {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                   'HOME': str(Path(directory) / 'metadata-home'),
                   'CLAUDE_CONFIG_DIR': str(Path(directory) / 'metadata-home')})
    # A tmux-owned view is not a child of its broker. Pin the reviewed broker
    # before spawning so its death cannot leave that native client orphaned.
    lifetime_fd = None
    if 'lifetime' in manifest:
        lifetime = manifest['lifetime']
        lifetime_fd = os.pidfd_open(lifetime['pid'])
        if incarnation(lifetime['pid']) != lifetime['started'] or select.select([lifetime_fd], [], [], 0)[0]:
            os.close(lifetime_fd)
            raise ValueError('Owned lifetime already ended')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'Subreaper unavailable')
    identity = {'runId': manifest['runId'], 'token': manifest['token'], 'pid': os.getpid(),
                'started': incarnation(os.getpid()), 'subreaper': True}
    from .relay_receipts import provision, signature
    receipt_key=provision(manifest,fd)
    def receipt(name,value):
        _write(fd,name,{**value,'hmac':signature(receipt_key,name,value)})
    receipt('started.json', identity)  # O_EXCL: no duplicate native launch.
    stopping = [False]
    def stop(signum, frame):
        stopping[0] = True
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    gate_read, gate_write = os.pipe()
    child = os.fork()
    if child == 0:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_DFL)
        os.close(gate_write)
        os.setpgid(0, 0)
        os.read(gate_read, 1)
        os.close(gate_read)
        os.execv(manifest['executable'], [manifest['executable'], *args])
    try:
        os.setpgid(child, child)
    except ProcessLookupError:
        pass
    def interrupt(signum, frame):
        try:
            signal.pidfd_send_signal(root_fd, signum)
        except ProcessLookupError:
            pass
    os.close(gate_read)
    root_fd = os.pidfd_open(child)
    os.write(gate_write, b"1")
    os.close(gate_write)
    signal.signal(signal.SIGINT, interrupt)
    if os.isatty(0):
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        try:
            os.tcsetpgrp(0, child)
        except OSError:
            pass
    status = 1
    stopped_at = None
    try:
        while True:
            if lifetime_fd is not None and select.select([lifetime_fd], [], [], 0)[0]:
                stopping[0] = True
            try:
                pid, code = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                receipt('exit.json', {**identity, 'empty': True, 'status': status})
                return status
            if pid:
                if pid == child:
                    status = os.waitstatus_to_exitcode(code)
                    stopping[0] = True
                continue
            if stopping[0]:
                stopped_at = stopped_at or time.monotonic()
                sig = signal.SIGKILL if time.monotonic() - stopped_at > .25 else signal.SIGTERM
                # Direct children cannot be reparented away while we live. Pin
                # each PID before signalling; no global PID/name/group kill.
                children = Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()
                for value in children:
                    try:
                        owned = os.pidfd_open(int(value))
                        try:
                            signal.pidfd_send_signal(owned, sig)
                        finally:
                            os.close(owned)
                    except ProcessLookupError:
                        pass
            time.sleep(.01)
    finally:
        if lifetime_fd is not None:
            os.close(lifetime_fd)
        os.close(root_fd)
        os.close(fd)
