"""accd: Rynhult API, supervisor host and dashboard. Runs as the `acc` user, behind Caddy on a Unix socket
(ACC_LISTEN=unix:/run/accd/accd.sock); 127.0.0.1:8740 when nothing is set.

Browser access comes through a reverse proxy (Caddy, private networks only) and the app login
(password + TOTP, server-side sessions, CSRF double-submit, strict Origin checks).
The routers live in acc.server.api; this module builds the shared context and wires them up.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from acc.env import environment

from . import auth, vault
from .api import (
    accounts,
    admin_features,
    backups,
    board,
    catalog,
    certificate,
    containers,
    devices,
    dns,
    endpoints,
    files,
    frontend,
    group_board,
    group_exchange,
    group_machines,
    groups,
    host_backups,
    inbox,
    issue_sources,
    mobile,
    nodes,
    operations,
    organizations,
    packages,
    passkeys,
    ports,
    preview,
    project_briefs,
    projects,
    publishing,
    pulls,
    push,
    schedules,
    sessions,
    settings_routes,
    telemetry,
    terminal,
    tools,
    updates,
    users,
    wiki,
)
from .api import auth as auth_routes
from .api import gates as orchestrator_gates
from .api import plan as orchestrator_plan
from .api import selection as orchestrator_selection
from .api import team_live as orchestrator_team_live
from .api import (
    vault as vault_routes,
)
from .api import worklist as orchestrator_worklist
from .api.context import UNSAFE, Context
from .broker_client import Brokers
from .catalog import load as load_catalog
from .catalog_store import CatalogStore
from .config import Settings
from .db import WRITE_INTENT, MaintenanceBusy, make_engine, make_sessionmaker, migrate
from .node_tasks import NodeTasks
from .nodes import WS_MAX_SIZE, NodeHub
from .publish import Publisher
from .push import Push
from .supervisor import Supervisor
from .tools import Tools

log = logging.getLogger("acc.server")


def create_app(settings: Settings | None = None, publisher: Publisher | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.ha_node_id and not settings.db_url.startswith('postgresql'):
        raise ValueError('Manual HA requires PostgreSQL')
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    from ..backup_activation import startup_blocked
    replacement = settings.data_dir / 'REPLACEMENT-ID'
    wrong_recovery_paths = ((replacement.exists() or replacement.is_symlink()) and (settings.database_url
        or settings.ha_node_id or settings.git_dir != settings.data_dir.parent / 'git'
        or settings.catalog_local_dir != settings.data_dir.parent / 'catalog-local'))
    if startup_blocked(settings.data_dir) or wrong_recovery_paths:
        held = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @held.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
        def quarantine(path: str):
            from fastapi.responses import JSONResponse
            return JSONResponse({'quarantine': True, 'detail': 'This Rynhult was restored from a backup and is on hold until an owner reviews it on the server.'},
                                status_code=503)

        @held.websocket('/{path:path}')
        async def quarantine_socket(websocket: WebSocket, path: str):
            await websocket.close(code=1008)
        return held
    engine = make_engine(settings.db_url, keepalive=bool(settings.ha_node_id))
    from .ha import Authority, Fenced, schema_change
    from .ha_runtime import Gate, Runtime
    if settings.database_url:
        # A shared database: never migrate under a main that holds authority.
        try:
            with schema_change(engine, settings.ha_node_id):
                migrate(settings.db_url)
        except ValueError:
            engine.dispose()
            raise
    else:
        migrate(settings.db_url)
    single_main = None
    if settings.database_url and not settings.ha_node_id:
        from sqlalchemy import text
        if engine.dialect.name == 'postgresql':
            from .db import _postgres_engine
            from .ha import LEADER_LOCK
            single_engine = _postgres_engine(engine.url)
            single_main = single_engine.connect()
            if not single_main.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': LEADER_LOCK}).scalar():
                single_main.close()
                single_engine.dispose()
                engine.dispose()
                raise ValueError('Another PostgreSQL main holds authority')
            single_main.commit()
    authority = Authority(engine, settings.ha_node_id) if settings.ha_node_id else None
    if authority:
        authority.install_write_guard()
    db = make_sessionmaker(engine)
    db.configure(info={'ha_authority': authority})
    from .admin_features import CURRENT, Features, RuntimeSettings
    features = Features(settings)
    with db() as session:
        features.load(session)
    settings = RuntimeSettings(settings, features)
    broker = Brokers(settings.broker_socket, settings.broker_socket_dir, settings.default_account, settings.services_owner,
                     hub=NodeHub(), authority=authority)
    supervisor = Supervisor(settings, db, broker)
    publisher = publisher or Publisher(settings, db)
    publisher.workspace_operations = supervisor.workspace_operations
    supervisor.publisher = publisher
    supervisor.tools = Tools(settings, db, broker)
    supervisor.push = Push(settings.vapid_key_path, settings.push_subject)
    ctx = Context(settings=settings, db=db, supervisor=supervisor, broker=broker, publisher=publisher,
                  token=settings.api_token())
    from .oidc import Providers
    ctx.oidc = Providers()
    from .remote_access import RemoteAccess
    ctx.remote_access = RemoteAccess(ctx)
    from .edge import Edge, EdgeGuard
    ctx.edge = Edge(ctx)
    runtime = Runtime(authority, ctx) if authority else None
    if not authority:
        with db() as s:
            auth.ensure_setup_token(s, settings.setup_token_path)
        migrate_mcp_secrets(settings, db, supervisor)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        CURRENT.set(features)  # all main-owned workers inherit this installation snapshot
        if runtime:
            async with runtime.lifespan():
                yield
            return
        broker.hub.closed = False
        workers = NodeTasks("main-workers")
        task = workers.start(supervisor.run(), "supervisor")
        updater = workers.start(ctx.updates.run(), "updater")
        backups_task = workers.start(ctx.backup_jobs.run(), "backup-jobs")
        remote_task = workers.start(ctx.remote_access.run(), "remote-access")
        from . import orchestrator
        workers.start(orchestrator.reconcile_availability(supervisor), "orchestrator-availability")  # once: nothing left running
        try:
            yield
        finally:
            task.cancel()
            updater.cancel()
            backups_task.cancel()
            remote_task.cancel()
            await asyncio.gather(task, updater, backups_task, remote_task, return_exceptions=True)
            jobs = [*supervisor.tools.tasks, *publisher.tasks]
            for work in jobs:
                work.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await broker.close()
            if single_main is not None:
                single_main.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LEADER_LOCK})
                single_main.commit()
                single_main.close()
                single_engine.dispose()

    app = FastAPI(title="Rynhult", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path.startswith('/api/host/backups/'):
            return JSONResponse(status_code=422, content={'detail': 'Some backup settings aren\'t valid. Check the form and try again.'})
        if request.url.path.startswith("/api/dns/"):
            return JSONResponse(status_code=422, content={"detail": "Some DNS settings aren't valid. Check the form and try again."})
        try:
            return await request_validation_exception_handler(request, error)
        except UnicodeEncodeError:
            # JSON may carry a lone surrogate ("\ud83d"), which the default handler can't echo back as UTF-8.
            # Still a 422, without the input and with every string made encodable.
            def text(value):
                return value.encode('utf-8', 'replace').decode() if isinstance(value, str) else value
            return JSONResponse(status_code=422, content={'detail': [
                {'type': item['type'], 'loc': [text(part) for part in item['loc']], 'msg': text(item['msg'])}
                for item in error.errors()]})


    @app.exception_handler(Exception)
    async def telemetry_error(request: Request, error: Exception):
        supervisor.telemetry.capture(error)
        return JSONResponse({'detail': 'Internal server error'}, status_code=500)

    @app.exception_handler(MaintenanceBusy)
    async def maintenance_busy(request: Request, error: MaintenanceBusy):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': 'Rynhult is busy with a backup or maintenance task. Try again in a moment.'},
                            status_code=503, headers={'Retry-After': '1'})

    @app.exception_handler(Fenced)
    async def fenced(request, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    if authority:
        from sqlalchemy.exc import SQLAlchemyError
        @app.exception_handler(SQLAlchemyError)
        async def authority_database_error(request, error):
            # A constraint, length or lock-timeout error from one request is not loss of
            # authority: re-check, which fences this main only if authority really failed.
            try:
                authority.check()
            except Fenced:
                pass
            return JSONResponse({'detail': 'Rynhult\'s database isn\'t available right now. An owner can check the High availability page, then try again.'}, status_code=503)

    app.state.ha = runtime
    app.state.supervisor = supervisor
    app.state.publisher = publisher
    app.state.db = db
    app.state.ctx = ctx
    app.add_middleware(BodyLimit, max_bytes=MAX_BODY, larger={re.compile(r'^/api/files/(project|session|workspace)/[^/]+/upload$'): 24 << 20, GIT_PREFIX: settings.backup_max_push, EXCHANGE_BODIES: 12 << 20, '/api/project-brief-references/': 15 << 20, re.compile(r'^/api/projects/[0-9]+/brief/documents$'): 56 << 20})

    @app.middleware("http")
    async def write_intent(request: Request, call_next):
        """Mutating requests open write transactions up front (see db.WRITE_INTENT)."""
        token = WRITE_INTENT.set(request.method in UNSAFE)
        try:
            response = await call_next(request)
            from .telemetry import ROUTE_COUNTERS
            route = request.scope.get('route')
            counter = ROUTE_COUNTERS.get((request.method, getattr(route, 'path', '')))
            if counter and 200 <= response.status_code < 300:
                try:
                    with db() as s:
                        supervisor.telemetry.count(s, counter)
                        s.commit()
                except Exception:
                    pass  # Optional local statistics cannot break a successful operation.
            return response
        finally:
            WRITE_INTENT.reset(token)

    for module in (auth_routes, admin_features, passkeys, groups, group_exchange, group_board, group_machines, projects, sessions, schedules, inbox, operations, board, catalog, publishing, accounts, terminal, wiki, push, users, ports, preview,
                   endpoints, dns, packages, settings_routes, devices, telemetry, nodes, organizations, mobile, certificate, containers, vault_routes, backups, host_backups, updates, tools, files, issue_sources, pulls):
        app.include_router(module.router(ctx))
    # Orchestrator v2: one router per slice (src/acc/server/orchestrator); empty until each slice is built.
    for module in (orchestrator_worklist, orchestrator_team_live, orchestrator_selection, orchestrator_gates,
                   orchestrator_plan, project_briefs):
        app.include_router(module.router(ctx))
    if runtime:
        app.include_router(runtime.router())
    from .admin_features import FeatureBoundary
    from .preview import PreviewBoundary
    frontend.install(app, ctx)  # last: its catch-all route serves the dashboard
    app.add_middleware(FeatureBoundary, ctx=ctx)
    app.add_middleware(PreviewBoundary, ctx=ctx)  # preview never inherits dashboard policies
    app.add_middleware(EdgeGuard, ctx=ctx)  # public requests and streams: per-request identity, closed on revocation
    if authority:
        app.add_middleware(Gate, authority=authority, runtime=runtime)  # authority also fences preview traffic
    @app.middleware("http")
    async def retire_legacy_auth_cookies(request: Request, call_next):
        response = await call_next(request)
        if settings.cookie_secure and getattr(request.state, 'cookie_authenticated', False):
            ctx.expire_legacy_cookies(response)
        return response

    return app


def migrate_mcp_secrets(settings: Settings, db, supervisor: Supervisor) -> None:
    """Once: MCP secrets in the local catalog become vault keys (the catalog keeps `vault:<id>` references)."""
    try:
        from ..maintenance import lease
        from .catalog_store import catalog_lock
        # Catalog, then database: the order capture and catalog writers use, so no two of them wait on each other.
        with catalog_lock(settings.catalog_local_dir), lease(settings.data_dir / '.maintenance.lock', exclusive=False):
            with db() as s:
                moved = vault.migrate_catalog(s, supervisor.vault, CatalogStore(settings.catalog_dir, settings.catalog_local_dir))
        if moved:
            log.info("vault: moved %d MCP secrets from the catalog into the vault", moved)
            supervisor.set_catalog(load_catalog(settings.catalog_dir, settings.catalog_local_dir))
    except Exception:  # never in the way of starting; launches that need those secrets say what's wrong
        log.exception("vault: moving the catalog's MCP secrets into the vault failed")


MAX_BODY = 4 << 20  # the largest legitimate request (a catalog import's files) is well under this
# Only a group exchange grant holder sends packs; every other group route keeps MAX_BODY.
EXCHANGE_BODIES = re.compile(r"^/api/groups/[^/]+/projects/[0-9]+/exchange/(transfer$|git/)")
GIT_PREFIX = "/api/git/"  # backup pushes (their own, larger limit; api/backups.py counts too)


class BodyLimit:
    """Refuse oversized request bodies before anything reads them (also before authentication): by the declared
    Content-Length, and by counting when a body is streamed without one. `larger` gives path prefixes their own
    limit."""

    def __init__(self, app, max_bytes: int, larger: dict[str, int] | None = None):
        self.app, self.max_bytes, self.larger = app, max_bytes, larger or {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        limit = next((n for key, n in self.larger.items()
                      if (key.match(path) if isinstance(key, re.Pattern) else path.startswith(key))), self.max_bytes)
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            return await self._refuse(send)
        seen = 0

        async def counted():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    # FastAPI answers it while reading the body
                    raise HTTPException(413, "That's too much to send at once. Try a smaller file or less text.")
            return message
        await self.app(scope, counted, send)

    @staticmethod
    async def _refuse(send):
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"detail":"That\'s too much to send at once. Try a smaller file or less text."}'})


# URLs whose path or query carries a bearer secret: company sign-in callbacks (state, code), sign-in links (invite)
# and local invite links (/invite/<token>). uvicorn logs paths with their query in access lines and WebSocket lines.
SECRET_URL = re.compile(r"(/api/auth/oidc/[A-Za-z]+)\?\S*|(/invite/)[^\s\"?]+(?:\?\S*)?")


class RedactSecretURLs(logging.Filter):
    """Keep the route, drop the secret: rewrites any string argument (or message) of a uvicorn log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        def clean(value):
            return SECRET_URL.sub(lambda m: m.group(1) or m.group(2) + "…", value) if isinstance(value, str) else value
        if isinstance(record.args, tuple):
            record.args = tuple(clean(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: clean(value) for key, value in record.args.items()}
        record.msg = clean(record.msg)
        return True


def uvicorn_log_config() -> dict:
    """uvicorn's own logging setup with RedactSecretURLs on both of its handlers."""
    import copy

    from uvicorn.config import LOGGING_CONFIG
    config = copy.deepcopy(LOGGING_CONFIG)
    config["filters"] = {"secret_urls": {"()": RedactSecretURLs}}
    for handler in config["handlers"].values():
        handler["filters"] = ["secret_urls"]
    return config


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per GitHub/Cloudflare request is noise
    listen = Settings.from_env().listen
    # An explicit WebSocket message ceiling (uvicorn's default is 16 MiB); node frames have their own, smaller one.
    options = {"log_level": "info", "proxy_headers": False, "ws_max_size": WS_MAX_SIZE,
               "log_config": uvicorn_log_config()}  # no sign-in secrets in access lines
    # Behind Caddy on a Unix socket the client address comes from Caddy's X-Forwarded-For (Context.client_key).
    if listen == "systemd":  # socket activation (accd.socket): systemd passes the listener as fd 3
        if environment.get("LISTEN_PID") != str(os.getpid()) or environment.get("LISTEN_FDS") != "1":
            raise SystemExit("ACC_LISTEN=systemd needs accd.socket to pass exactly one listener")
        uvicorn.run(create_app(), fd=3, **options)
    elif listen.startswith("unix:"):
        uvicorn.run(create_app(), uds=listen.removeprefix("unix:"), **options)
    else:
        host, _, port = listen.rpartition(":")
        uvicorn.run(create_app(), host=host, port=int(port), **options)


if __name__ == "__main__":
    main()
