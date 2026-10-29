"""Bearer secrets in URLs (company sign-in callbacks, sign-in links, local invites) never reach accd's logs
(security review 2026-10-25, finding 3). Uses uvicorn's real server, formatters and accd's own logging config."""
from __future__ import annotations

import asyncio
import logging
import socket

import httpx
import uvicorn
from fastapi import FastAPI

from acc.server.app import uvicorn_log_config

SECRETS = ("state-SECRET-1", "code-SECRET-2", "invite-SECRET-3", "localinvite-SECRET-4", "ws-SECRET-5")


async def test_access_and_error_logs_never_record_sign_in_secrets(capsys):
    app = FastAPI()

    @app.get("/{path:path}")
    def anything(path: str):
        return {}

    saved = {name: (logging.getLogger(name).handlers[:], logging.getLogger(name).filters[:])
             for name in ("uvicorn", "uvicorn.error", "uvicorn.access")}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_config=uvicorn_log_config(), log_level="info", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            await client.get(f"/api/auth/oidc/callback?state={SECRETS[0]}&code={SECRETS[1]}")
            await client.get(f"/api/auth/oidc/start?invite={SECRETS[2]}")
            await client.get(f"/invite/{SECRETS[3]}")
            await client.get("/api/health?probe=visible-ordinary-query")
        # uvicorn's WebSocket lines go to uvicorn.error with the path: the same rule applies there.
        logging.getLogger("uvicorn.error").info('%s - "WebSocket %s" %s', "127.0.0.1:1",
                                                f"/api/auth/oidc/callback?code={SECRETS[4]}", "403")
    finally:
        server.should_exit = True
        await task
        listener.close()
        for name, (handlers, filters) in saved.items():
            logging.getLogger(name).handlers[:], logging.getLogger(name).filters[:] = handlers, filters
    output = "".join(capsys.readouterr())
    assert "/api/auth/oidc/callback" in output and "/api/auth/oidc/start" in output and "/invite/" in output
    assert "visible-ordinary-query" in output  # other requests log as before
    for secret in SECRETS:
        assert secret not in output, output
