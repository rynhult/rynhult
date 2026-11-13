"""Bounded OSC 7501 observer. Only allowlisted states leave this process."""
from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import time

STATES = {b'working', b'done', b'blocked', b'error'}
MAX_SEQUENCE = 256
MAX_PER_SECOND = 32


PREFIX = b'\x1b]7501;'
TERMINATOR = re.compile(rb'\x07|\x1b\\')
PRINTABLE = re.compile(rb'[ -~]*')


class StatusParser:
    def __init__(self):
        self.buffer = b''
        self.discard = False
        self.window = -1
        self.count = 0

    def feed(self, data: bytes, at: float) -> str | None:
        """C-speed chunk searches; bounded tail and at most 32 candidate parses/second."""
        window = int(at)
        if window != self.window:
            self.window, self.count = window, 0
        if self.count >= MAX_PER_SECOND:
            # Drain without scanning until the next window. No backlog in tmux's pipe.
            self.buffer, self.discard = b'', False
            return None
        data = self.buffer + data
        self.buffer = b''
        pos = 0
        latest = None
        if self.discard:
            end = TERMINATOR.search(data)
            if end is None:
                self.buffer = b'\x1b' if data.endswith(b'\x1b') else b''
                return None
            pos, self.discard = end.end(), False
        while self.count < MAX_PER_SECOND:
            start = data.find(PREFIX, pos)
            # Ignore whole unrelated OSCs in bulk, including thousands of OSC 8 links.
            # A 7501 prefix inside another OSC is not a new control sequence.
            boundary = max(data.rfind(b'\x07', pos, start if start >= 0 else len(data)),
                           data.rfind(b'\x1b\\', pos, start if start >= 0 else len(data)))
            opening = data.find(b'\x1b]', max(pos, boundary + 1))
            if start < 0:
                if opening >= 0:
                    if len(data) - opening < len(PREFIX) and PREFIX.startswith(data[opening:]):
                        self.buffer = data[opening:]
                    else:
                        self.discard = True
                        self.buffer = b'\x1b' if data.endswith(b'\x1b') else b''
                else:
                    self.buffer = b'\x1b' if data.endswith(b'\x1b') else b''
                return latest
            end = TERMINATOR.search(data, start + len(PREFIX))
            if opening != start:  # nested in an unrelated/unclosed OSC
                self.count += 1  # bound malformed 7501 candidates as well
                if end is None:
                    self.discard = True
                    self.buffer = b'\x1b' if data.endswith(b'\x1b') else b''
                    return latest
                pos = end.end()
                continue
            if end is None:
                if len(data) - start <= MAX_SEQUENCE:
                    self.buffer = data[start:]
                else:
                    self.count += 1
                    self.discard = True
                    self.buffer = b'\x1b' if data.endswith(b'\x1b') else b''
                return latest
            self.count += 1  # only 7501 candidates consume the rate budget
            if end.end() - start <= MAX_SEQUENCE:
                value = self._parse(data[start + len(PREFIX):end.start()])
                if value:
                    latest = value
            pos = end.end()
        self.buffer, self.discard = b'', False
        return latest

    @staticmethod
    def _parse(payload: bytes) -> str | None:
        if PRINTABLE.fullmatch(payload) is None:
            return None
        values = []
        for field in payload.split(b':'):
            key, sep, value = field.strip().partition(b'=')
            if key == b'state' and sep:
                values.append(value.strip())
        if len(values) == 1 and values[0] in STATES:
            return values[0].decode('ascii')
        return None


def wait_for_observer(socket: str, channel: str, timeout: float = 10) -> int:
    """A broker crash during launch must not leave the pane waiting forever."""
    try:
        result = subprocess.run(['tmux', '-L', socket, 'wait-for', channel], timeout=timeout, check=False)
        if result.returncode == 0:
            return 0
    except subprocess.TimeoutExpired:
        pass
    print('Rynhult terminal startup failed: timed out waiting for the status observer. Restart this session.',
          file=sys.stderr, flush=True)
    return 1


def observe(socket: str, target: str) -> None:
    parser = StatusParser()
    pending = None
    next_write = 0.0
    while True:
        at = time.monotonic()
        ready, _, _ = select.select([0], [], [], max(0, next_write - at) if pending else None)
        if ready:
            chunk = os.read(0, 65536)
            if not chunk:
                return
            pending = parser.feed(chunk, at) or pending
        if pending and time.monotonic() >= next_write:
            subprocess.run(['tmux', '-L', socket, 'set-option', '-t', '=' + target + ':',
                            '@acc-osc7501', pending], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=2, check=False)
            pending = None
            next_write = time.monotonic() + 1


if __name__ == '__main__':
    if sys.argv[1:2] == ['--wait']:
        sys.exit(wait_for_observer(*sys.argv[2:]))
    observe(*sys.argv[1:])
