"""Bounded JSON-lines probes for provider CLIs (provider handshakes stay in adapters)."""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import select
import subprocess
import threading
import time

_CANCEL = contextvars.ContextVar("provider_rpc_cancel", default=None)
MAX_FRAME = 256 * 1024


async def cancellable(function, *args):
    """Cancellation signals the probe's worker and joins it, including child cleanup."""
    event = threading.Event()
    token = _CANCEL.set(event)
    try:
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            event.set()
            await asyncio.gather(worker, return_exceptions=True)
            raise
    finally:
        _CANCEL.reset(token)


class RPC:
    def __init__(self, argv: list[str], timeout: float, *, cwd=None):
        self.deadline = time.monotonic() + timeout
        self.cancel = _CANCEL.get()
        self.buffer = bytearray()
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, cwd=cwd, start_new_session=True)
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        finally:
            self.process.stdin.close()
            self.process.stdout.close()

    def remaining(self):
        if self.cancel is not None and self.cancel.is_set():
            raise InterruptedError("provider RPC cancelled")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("provider RPC did not answer before deadline")
        return min(remaining, .05)  # also bound cancellation latency

    def send(self, message: dict):
        payload = memoryview(json.dumps(message).encode() + b"\n")
        if len(payload) > MAX_FRAME:
            raise ValueError("provider RPC request too large")
        while payload:
            _, ready, _ = select.select([], [self.process.stdin], [], self.remaining())
            if ready:
                try:
                    count = os.write(self.process.stdin.fileno(), payload)
                except BlockingIOError:
                    continue
                payload = payload[count:]

    def receive(self, request_id: int) -> dict:
        while True:
            self.remaining()
            if b"\n" in self.buffer:
                raw, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                if len(raw) > MAX_FRAME:
                    raise ValueError("provider RPC frame too large")
                try:
                    message = json.loads(raw)
                except ValueError:
                    raise ValueError("malformed provider RPC frame") from None
                if not isinstance(message, dict):
                    raise ValueError("malformed provider RPC envelope")
                if message.get("id") == request_id and type(message.get("id")) is int:
                    if "error" in message:
                        raise ValueError("provider RPC error")
                    if "result" not in message or (message["result"] is not None and not isinstance(message["result"], dict)):
                        raise ValueError("malformed provider RPC result")
                    return message
                continue  # notifications and unrelated responses
            if len(self.buffer) > MAX_FRAME:
                raise ValueError("provider RPC frame too large")
            ready, _, _ = select.select([self.process.stdout], [], [], self.remaining())
            if ready:
                try:
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    raise ValueError("provider RPC closed before an answer")
                self.buffer.extend(chunk)
