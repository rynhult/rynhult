"""Test helpers shared by the supervisor tests."""
from __future__ import annotations

import asyncio
import inspect
import os
import socket
from contextlib import contextmanager

from acc.server.db import WRITE_INTENT

TOTP_AT = 1_800_000_015.0


async def wait_for(predicate, timeout: float = 15.0, interval: float = 0.1):
    """Poll a sync or async predicate until it returns a truthy value."""
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = predicate()
        if inspect.isawaitable(last):
            last = await last
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


def control(env, acc_id: str, command: str) -> None:
    path = env["run_dir"] / acc_id / "fake.ctl"
    scratch = path.with_suffix(".tmp")
    scratch.write_text(command)
    scratch.replace(path)


@contextmanager
def write_db(factory):
    """Take SQLite's write lock before reading test data that will be changed. Never await inside this scope."""
    token = WRITE_INTENT.set(True)
    try:
        with factory() as db:
            yield db
    finally:
        WRITE_INTENT.reset(token)


async def verified_post(client, url: str, json: dict | None = None):
    """POST from the owner's browser sign-in (never the API token), freshly verified: inviting an owner and resetting
    someone else need that. test_security_review_passkeys proves the verification itself."""
    from acc.server import auth
    from acc.server.models import AuthSession
    with write_db(client.app.state.db) as s:
        s.get(AuthSession, auth.digest(client.cookies.get("acc_session"))).reauthenticated_at = auth.now()
        s.commit()
    return await client.post(url, json=json or {}, headers={
        "Authorization": "", "Origin": client.app.state.ctx.settings.allowed_origins[0],
        "X-ACC-CSRF": client.cookies.get("acc_csrf") or ""})


async def reconcile_passes(api, count=3):
    """Exercise the negative assertion after actual passes, not an assumed amount of scheduler time."""
    for _ in range(count):
        await api.app.state.supervisor.reconcile()


async def pause_supervisor(api):
    """Let a test drive production stages in order, without a second scheduler racing those stages."""
    api.supervisor_task.cancel()
    await asyncio.gather(api.supervisor_task, return_exceptions=True)
    backup = api.app.state.supervisor._backup_task
    if backup is not None:
        await asyncio.gather(backup, return_exceptions=True)
    await asyncio.gather(*api.supervisor_threads)


@contextmanager
def preview_listener(ports=range(8000, 8100), *, run_id=None):
    """Allocate a run-specific preview socket, retaining it until handed to the server.

    E2E passes its ingress port as run_id; Python workers default to their PID. Candidate overlap (including identical
    run IDs) falls back via exclusive bind, never a find-close-rebind race or SO_REUSEPORT. Exhaustion refuses startup.
    """
    candidates = list(ports)
    if not candidates:
        raise RuntimeError('No free preview test port')
    offset = (os.getpid() if run_id is None else run_id) % len(candidates)
    candidates = candidates[offset:] + candidates[:offset]
    for port in candidates:
        listener = socket.socket()
        # TIME_WAIT must not consume the preview range during repeated runs. Bind and listen must both succeed:
        # SO_REUSEADDR permits two binds before either listens, so losing the listen race retries with a new socket.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(('127.0.0.1', port))
            listener.listen()
        except OSError:
            listener.close()
            continue
        try:
            yield listener
        finally:
            listener.close()
        return
    raise RuntimeError('No free preview test port')


@contextmanager
def task_snapshot():
    """Capture task identities before a lifecycle test; pair with assert_no_task_leaks."""
    before = set(asyncio.all_tasks())
    yield before


async def assert_no_task_leaks(before):
    """Fail on any new live task, then cancel/join it so a failure cannot poison later tests."""
    for _ in range(3):
        await asyncio.sleep(0)
    leaked = set(asyncio.all_tasks()) - before - {asyncio.current_task()}
    try:
        assert not leaked, "Leaked tasks: " + ", ".join(sorted(t.get_name() for t in leaked))
    finally:
        for task in leaked:
            task.cancel()
        await asyncio.gather(*leaked, return_exceptions=True)
