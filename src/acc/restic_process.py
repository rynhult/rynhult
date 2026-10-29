"""Own each restic child until it exits, allowing SIGINT's repository-lock cleanup."""
from __future__ import annotations

import contextvars
import signal
import subprocess
import threading
import time

cancelled: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar('restic_cancelled', default=None)
CLEANUP_SECONDS = 65  # pinned restic allows up to one minute for lock removal


def check_cancelled():
    event = cancelled.get()
    if event is not None and event.is_set():
        raise InterruptedError('backup command cancelled')


def stop(process, *, cleanup_timeout=None):
    """Only this recorded Popen child; never discover or signal unrelated processes."""
    if process.poll() is None:
        try:
            process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass  # it exited between poll and terminate
    try:
        process.communicate(timeout=CLEANUP_SECONDS if cleanup_timeout is None else cleanup_timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def run(argv, *, env, stdin=None, stdout=subprocess.PIPE, timeout=7200, output_limit=None, idle_timeout=None,
        started=None, cleanup_timeout=None):
    """`started(pid)` runs once the child exists; the child is reaped before this returns or raises."""
    check_cancelled()
    process = subprocess.Popen(argv, env=env, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    last_size, last_output = 0, time.monotonic()
    try:
        if started:
            started(process.pid)
        while True:
            check_cancelled()
            if output_limit is not None:
                size = stdout.tell()
                if size > output_limit:
                    raise OverflowError('restore stream exceeds limit')
                if size != last_size:
                    last_size, last_output = size, time.monotonic()
                if idle_timeout and time.monotonic() - last_output >= idle_timeout:
                    raise TimeoutError('restore stream timed out')
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('restic command timed out')
            try:
                output, error = process.communicate(timeout=min(.2, left))
                if output_limit is not None and stdout.tell() > output_limit:
                    raise OverflowError('restore stream exceeds limit')
                return subprocess.CompletedProcess(argv, process.returncode, output, error)
            except subprocess.TimeoutExpired:
                continue
    finally:
        try:
            if process.poll() is None:
                stop(process, cleanup_timeout=cleanup_timeout)
            else:
                process.wait()
        finally:
            # Cancellation may race natural exit before communicate ever runs.
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
