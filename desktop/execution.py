"""Desktop composition. Installed server entry points never import this module."""
from __future__ import annotations

import asyncio
import json
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import delete, select
from starlette.responses import JSONResponse

from acc.server import auth
from acc.server.broker_client import BrokerError
from acc.server.models import AuditLog, AuthSession, Observation, Project, User
from acc.server.supervisor import LaunchRefused


class CLIInstall(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: Literal['claude', 'codex']
    version: str = Field(pattern=r'^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$', max_length=100)


class NativeConsent(BaseModel):
    accept: StrictBool


def validate_account(value: dict, owner_uid: int):
    import pwd
    account = pwd.getpwnam(value['agent'])
    if account.pw_uid in (0, owner_uid, 65534) or account.pw_uid < 1000:
        raise ValueError('Desktop execution requires a separate unprivileged agent account')
    if account.pw_dir != value['home']:
        raise ValueError('Agent home differs from provisioned configuration')
    if account.pw_name in {'root', 'nobody'}:
        raise ValueError('Invalid agent account')
    if sys.platform == 'darwin':
        from mac_setup import verify_identity
        if account.pw_uid != value['agent_uid'] or account.pw_gid != value['agent_gid']:
            raise ValueError('macOS dedicated account UID/GID changed')
        verify_identity(value)
    return account


def read_manifest(owner_uid: int) -> dict | None:
    if sys.platform == 'darwin':
        try:
            from mac_setup import read_identity
        except ModuleNotFoundError as error:
            if error.name != 'mac_setup':
                raise
            # The bundled window may open before the one-time runtime package exists.
            if not Path(f'/Library/Application Support/Rynhult Desktop/Config/{owner_uid}/execution.json').exists():
                return None
            raise ValueError('macOS execution resources are missing; repair the installed package') from None
        value = read_identity(owner_uid)
        if value is not None:
            validate_account(value, owner_uid)
        return value
    if sys.platform != 'linux':
        return None
    path = Path(f'/etc/rynhult-desktop/{owner_uid}/execution.json')
    if not path.exists():
        return None
    for item in (path, *path.parents):
        info = item.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError('Desktop execution configuration must be root-owned and protected')
    value = json.loads(path.read_text())
    if value['version'] != 1 or value['owner_uid'] != owner_uid or value['agent'] != f'rynd-{owner_uid}':
        raise ValueError('Desktop execution configuration identity mismatch')
    base = Path(f'/var/lib/rynhult-desktop/{owner_uid}')
    expected = {'home': str(base / value['agent']), 'run_dir': str(base / 'run'), 'projects': str(base / 'projects'),
                'socket': f"/run/rynhult-desktop/{owner_uid}/{value['agent']}.sock"}
    if any(value.get(key) != val for key, val in expected.items()):
        raise ValueError('Desktop execution configuration paths differ from setup')
    if value['resources'] != str(Path(__file__).resolve().parents[1]):
        raise ValueError('Desktop package changed location; administrator must review execution setup')
    validate_account(value, owner_uid)
    return value


def execution_settings(settings, value):
    if value is None:
        return settings
    return replace(settings, agent_home=Path(value['home']), run_dir=Path(value['run_dir']),
                   workspaces_root=Path(value['projects']),
                   broker_socket_dir=Path(value['socket']).parent,
                   broker_socket=Path(value['socket']),
                   hook_path=str(Path(value['resources']) / 'bin/acc-hook'),
                   statusline_path=str(Path(value['resources']) / 'bin/acc-statusline'))


def bootstrap_owner(app):
    """Called only by the native entry point before listening, never an HTTP route."""
    with app.state.db() as db:
        people = list(db.scalars(select(User)))
        if people and (len(people) != 1 or people[0].username != 'desktop-owner' or people[0].role != 'owner'):
            raise ValueError('Use a fresh desktop profile; existing server people cannot be adopted')
        if not people:
            owner = User(username='desktop-owner', role='owner', account=app.state.ctx.settings.default_account,
                         password_hash='', totp_secret='')
            db.add(owner)
            db.flush()
            db.add(AuditLog(actor=owner.username, action='desktop.owner.bootstrap', target=owner.username))
        else:
            owner = people[0]
            owner.account = app.state.ctx.settings.default_account
        db.execute(delete(AuthSession).where(AuthSession.user_id == owner.id))
        token, csrf = auth.create_session(db, owner, 'Rynhult desktop host')
        db.commit()
        auth.ensure_setup_token(db, app.state.ctx.settings.setup_token_path)
    return token, csrf


def configure(app, *, provisioned: bool):
    ctx = app.state.ctx
    api = APIRouter()
    execution_lock = asyncio.Lock()
    updating = False

    def consent_value(project):
        value = {'path': project.workspace_path, 'account': project.account}
        identity = getattr(app.state, 'desktop_agent_identity', None)
        if identity is not None:
            value['identity'] = identity
        return value

    def approved(db, project):
        record = db.get(Observation, f'desktop.native.{project.id}')
        return record is not None and record.value == consent_value(project)

    def check(db, project, provider):
        if not provisioned:
            raise LaunchRefused('Complete the one-time desktop execution setup before starting sessions')
        if provider not in {'claude', 'codex'}:
            raise LaunchRefused('This desktop slice supports Claude and Codex only')
        if project is None or project.host not in (None, 'local') or project.account != ctx.settings.default_account:
            raise LaunchRefused('Desktop sessions must use the provisioned local account')
        if not approved(db, project):
            raise LaunchRefused('Approve this project in Desktop execution first: Not isolated · Separate agent account')

    create = ctx.supervisor.create_session

    async def create_session(project_id, inputs, *args, **kwargs):
        async with execution_lock:
            with ctx.db() as db:
                check(db, db.get(Project, project_id), inputs.provider)
            return await create(project_id, inputs, *args, **kwargs)

    ctx.supervisor.create_session = create_session
    call = ctx.broker.call

    async def broker_call(method, *args, **kwargs):
        if not provisioned:
            raise BrokerError('Complete the one-time desktop execution setup first')
        if method == 'launch':
            if updating:
                raise BrokerError('CLI update is in progress; retry after it completes')
            from acc.server.models import Session
            with ctx.db() as db:
                session = db.get(Session, kwargs.get('acc_id'))
                try:
                    check(db, session.project if session else None, kwargs.get('provider'))
                except LaunchRefused as error:
                    raise BrokerError(str(error)) from None
        if method.startswith('compose_') or method == 'tool_change':
            raise BrokerError('Host services and software changes are unavailable in this desktop slice')
        return await call(method, *args, **kwargs)

    ctx.broker.call = broker_call

    @api.post('/api/desktop/cli')
    async def install_cli(body: CLIInstall, who=Depends(ctx.owner)):
        nonlocal updating
        from acc.server.models import Session
        if not provisioned:
            raise HTTPException(409, 'Complete desktop execution setup first')
        async with execution_lock:
            with ctx.db() as db:
                if db.scalar(select(Session.id).where(Session.desired_state == 'running').limit(1)):
                    raise HTTPException(409, 'Stop running sessions before installing or updating a CLI')
            updating = True
            client = ctx.broker.fresh(ctx.settings.default_account, timeout=930)
            package = {'claude': '@anthropic-ai/claude-code', 'codex': '@openai/codex'}[body.provider]
            try:
                # A committed stop intent precedes the actual broker kill. Do
                # not replace CLI files while that pane is still alive (or its
                # stop acknowledgement was lost). Launches are fenced by updating.
                panes = await client.call('list')
                if any(not pane['dead'] for pane in panes):
                    raise HTTPException(409, 'Stop running sessions and wait for their processes to exit before updating a CLI')
                result = await client.call('tool_change', manager='npm', name=package,
                                           version=body.version, operation='install')
            except BrokerError as error:
                raise ctx.refused(error) from None
            finally:
                await client.close()
                updating = False
            with ctx.db() as db:
                db.add(AuditLog(actor=who.username, action='desktop.cli.install', target=body.provider,
                                detail={'package': package, 'version': body.version, 'account': ctx.settings.default_account}))
                db.commit()
            return result

    @api.get('/api/desktop/status')
    def status(who=Depends(ctx.principal)):
        with ctx.db() as db:
            projects = list(db.scalars(select(Project).where(Project.archived.is_(False))))
            return {'provisioned': provisioned, 'setup_supported': sys.platform in {'linux', 'darwin'},
                    'setup_kind': 'macos-installer' if sys.platform == 'darwin' else 'linux' if sys.platform == 'linux' else 'unavailable', 'account': ctx.settings.default_account,
                    'protection': 'Not isolated · Separate agent account',
                    'projects': [{'id': p.id, 'name': p.name, 'approved': approved(db, p)} for p in projects]}

    @api.post('/api/desktop/projects/{project_id}/native')
    def consent(project_id: int, body: NativeConsent, who=Depends(ctx.owner)):
        if not provisioned:
            raise HTTPException(409, 'Complete desktop execution setup first')
        with ctx.db() as db:
            project = ctx.project_for(db, who, project_id)
            if project.host not in (None, 'local') or project.account != ctx.settings.default_account:
                raise HTTPException(409, 'Project must use the provisioned desktop account')
            key = f'desktop.native.{project_id}'
            old = db.get(Observation, key)
            if old:
                db.delete(old)
                db.flush()
            if body.accept:
                db.add(Observation(key=key, value=consent_value(project),
                                   source='desktop-owner', observed_at=time.time()))
            db.add(AuditLog(actor=who.username, action='desktop.native.approve' if body.accept else 'desktop.native.revoke',
                            target=project.slug, detail={'protection': 'Not isolated', 'account': project.account}))
            db.commit()
        return {'approved': body.accept}

    # Insert before the existing SPA catch-all, retaining its normal auth dependencies.
    app.router.routes[0:0] = api.routes

    @app.middleware('http')
    async def desktop_scope(request, call_next):
        path = request.url.path
        unavailable = ('/api/users', '/api/nodes', '/api/groups', '/api/teams', '/api/schedules', '/api/multiruns',
                       '/api/publish', '/api/dns', '/api/remote-access', '/api/host/', '/api/updates',
                       '/api/auth/setup', '/api/auth/invite', '/api/auth/password', '/api/devices',
                       # General tool/package requests reach the broker through fresh clients, past broker_call.
                       '/api/tools', '/api/packages')
        if not provisioned and request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and (path.startswith('/api/sessions') or path in {'/api/projects/new', '/api/projects/import'}):
            return JSONResponse({'detail': 'Complete the one-time desktop execution setup first'}, status_code=409)
        if path.startswith(unavailable):
            return JSONResponse({'detail': 'Unavailable in this desktop session slice'}, status_code=409)
        return await call_next(request)
