"""Headless desktop user path with real HTTP/WebSocket/broker and fake CLIs."""
import asyncio
import importlib.util
import json
import os
import secrets
import socket
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import uvicorn
from helpers import wait_for
from test_desktop_sessions import (  # noqa: F401 (shared fixture)
    desktop_layout,
    install_fake_npm,
    runtime,
    unprivileged_broker_tools,
)

from acc.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.environ.get('DESKTOP_BROWSER_CHECK') != '1', reason='Set DESKTOP_BROWSER_CHECK=1; needs built desktop UI and read-only node_modules')
async def test_desktop_window_user_path(env, desktop_layout, broker, supervisor_threads, unprivileged_broker_tools):  # noqa: F811
    install_fake_npm(env['home'])
    source = ROOT / 'desktop/main.py'
    spec = importlib.util.spec_from_file_location('desktop_main_browser', source)
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    origin = f'http://127.0.0.1:{listener.getsockname()[1]}'
    settings = replace(env['settings'], static_dir=ROOT / 'desktop/web-dist', allowed_origins=(origin,), host_label="This computer · Standalone")
    assert (settings.static_dir / 'index.html').exists(), 'Build VITE_DESKTOP=1 UI into desktop/web-dist first'
    app = create_app(settings)
    runtime().configure(app, provisioned=True)
    login = runtime().bootstrap_owner(app)
    token = secrets.token_hex(32)
    guarded = main.CapabilityGuard(app, token, origin, login=login)
    server = uvicorn.Server(uvicorn.Config(guarded, log_level='warning', access_log=False, timeout_graceful_shutdown=3))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await wait_for(lambda: server.started)
        result = await asyncio.to_thread(subprocess.run, ['node', str(ROOT / 'desktop/tests/sessions.mjs')],
            input=json.dumps({'origin': origin, 'token': token}), text=True, capture_output=True, timeout=100,
            env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': '/srv/acc/tools/ms-playwright'})
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()
        app.state.db.kw['bind'].dispose()
