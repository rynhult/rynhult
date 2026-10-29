"""Opt-in desktop spike entry point. Never imported by the installed server.

One private JSON line on stdin supplies config/capability. EOF requests graceful
shutdown. Stdout contains only readiness, never secrets. Execution uses a separately provisioned systemd/launchd broker; the main never elevates.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import fields
from http.cookies import SimpleCookie
from pathlib import Path
from secrets import compare_digest

from acc.server.config import Settings


class CapabilityGuard:
    def __init__(self, app, token, origin, login=None):
        self.app, self.token, self.origin = app, token, origin
        self.login = login
        self.host = origin.removeprefix('http://')

    async def __call__(self, scope, receive, send):
        if scope['type'] not in {'http', 'websocket'}:
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        cookies = SimpleCookie()
        try:
            cookies.load(headers.get(b'cookie', b'').decode('latin1'))
            cookie = cookies.get('rynhult_desktop')
            valid = cookie is not None and compare_digest(cookie.value.encode(), self.token.encode())
        except (ValueError, TypeError):
            valid = False
        origin = headers.get(b'origin', b'').decode('latin1')
        host = headers.get(b'host', b'').decode('latin1')
        # Refuse DNS rebinding and cross-origin access even with a valid cookie.
        trusted = host == self.host and (not origin or origin == self.origin)
        if not valid or not trusted:
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 4401})
            else:
                await send({'type': 'http.response.start', 'status': 401 if not valid else 403,
                            'headers': [(b'cache-control', b'no-store')]})
                await send({'type': 'http.response.body', 'body': b'Forbidden'})
            return
        if self.login and scope['type'] == 'http' and scope['method'] == 'GET' and scope['path'] == '/':
            from starlette.responses import Response
            response = Response()
            for name, value in zip(('acc_session', 'acc_csrf'), self.login):
                response.set_cookie(name, value, httponly=name == 'acc_session', samesite='strict')
            original_send = send

            async def with_login(message):
                if message['type'] == 'http.response.start':
                    message = {**message, 'headers': [*message.get('headers', []),
                               *(h for h in response.raw_headers if h[0] == b'set-cookie')]}
                await original_send(message)
            send = with_login
        await self.app(scope, receive, send)


def settings_for(root: Path, static: Path, origin: str) -> Settings:
    root = root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError('Desktop profile must be owned by this user and mode 0700')
    if sys.platform == 'darwin':
        acl = subprocess.run(['/bin/ls', '-lde', str(root)], check=True, capture_output=True, text=True,
                             env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if re.search(r'^\s*\d+:.*\ballow\b', acl.stdout, re.MULTILINE):
            raise ValueError('Desktop profile must not have macOS ACL grants; use a fresh private profile')
    defaults = Settings()
    # Explicit isolation from deployment defaults, including optional helper queues.
    paths = {field.name: root / field.name for field in fields(defaults)
             if isinstance(getattr(defaults, field.name), Path)}
    paths.update(static_dir=static.resolve(), agent_home=root / "unprovisioned-agent",
                 catalog_dir=Path(__file__).resolve().parents[1] / "catalog",
                 guide_dir=Path(__file__).resolve().parents[1])
    platform = 'macOS' if sys.platform == 'darwin' else 'Linux'
    return Settings(**paths, cookie_secure=False, allowed_origins=(origin,),
                    host_label=f'This computer · {platform} · Standalone', broker_socket_dir=None,
                    hook_path=str(root / 'unavailable-hook'), statusline_path=str(root / 'unavailable-statusline'),
                    github_interval=1e9,
                    provider_interval=1e9, models_interval=1e9)


def profile_lock(path: Path):
    """Hold a per-profile OS lease; close releases it, including after a crash.

    The Windows turn can consolidate this small adapter with its LockFileEx
    implementation when that patch lands. No POSIX import runs on Windows.
    """
    lock = path.open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if lock.tell() == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        lock.close()
        raise
    return lock


async def serve(config):
    if getattr(os, 'geteuid', lambda: None)() == 0:
        raise ValueError('The desktop main must never run elevated')
    import uvicorn

    from acc.server.app import create_app

    token = config['token']
    if len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('Expected a 256-bit hex capability')
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    origin = f'http://127.0.0.1:{listener.getsockname()[1]}'
    settings = settings_for(Path(config['data_dir']), Path(config['static_dir']), origin)
    from execution import bootstrap_owner, configure, execution_settings, read_manifest
    lock = profile_lock(Path(config['data_dir']) / 'main.lock')
    manifest = read_manifest(os.getuid()) if sys.platform in {'linux', 'darwin'} else None
    settings = execution_settings(settings, manifest)
    inner = create_app(settings)
    inner.state.desktop_agent_identity = manifest.get('agent_guid') if manifest else None
    configure(inner, provisioned=manifest is not None)
    login = bootstrap_owner(inner)
    app = CapabilityGuard(inner, token, origin, login=login)
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level='warning',
                                          proxy_headers=False, timeout_graceful_shutdown=5))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if task.done():
            await task
            raise RuntimeError('Main failed before readiness')
        await asyncio.sleep(.01)
    print(json.dumps({'origin': origin, 'proof': hmac.digest(token.encode(), origin.encode(), 'sha256').hex()}), flush=True)
    # The parent owns stdin. Closing its private pipe stops the main even on host crash.
    await asyncio.to_thread(sys.stdin.buffer.read)
    server.should_exit = True
    await task
    listener.close()
    lock.close()
    # Reached only after uvicorn's lifespan shutdown; lifecycle tests look for it.
    print('desktop_main_stopped', file=sys.stderr, flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    config = json.loads(sys.stdin.buffer.readline(16384))
    asyncio.run(serve(config))
