"""Authorized file access: accd only handles broker replies, never agent filesystem paths."""
from __future__ import annotations

import base64
import posixpath
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..broker_client import BrokerError
from ..models import AuditLog, Project, Session, Setting, VaultKey, VaultRetired
from .context import Context


def download_disposition(path: str) -> str:
    name = posixpath.basename(path)
    # Header values must be ASCII. The extended parameter retains the exact
    # UTF-8 basename; the quoted fallback has no delimiters or control bytes.
    fallback = ''.join(c for c in name if 32 <= ord(c) < 127 and c not in '\\"/') or 'download'
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'


class Identity(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)


class Mutation(BaseModel):
    action: str = Field('write', pattern='^(write|create|mkdir|rename|delete)$')
    path: str = Field('', max_length=4096)
    content: str = Field('', max_length=1024 * 1024)
    revision: str | None = Field(None, max_length=100)
    destination: str = Field('', max_length=4096)
    message: str | None = Field(None, min_length=1, max_length=2000)


def router(ctx: Context) -> APIRouter:
    api = APIRouter()

    def identity(s, who):
        row = s.get(Setting, 'git.identity.' + who.username)
        return row.value if row else {'name': '', 'email': ''}

    @api.get('/api/files/identity')
    def get_identity(who=Depends(ctx.principal)):
        with ctx.db() as s:
            return identity(s, who)

    @api.post('/api/files/identity')
    def set_identity(body: Identity, who=Depends(ctx.principal)):
        if any(c in body.name + body.email for c in '\r\n\0<>') or not body.name.strip() or not re.fullmatch(r'[^\s@]+@[^\s@]+', body.email):
            raise HTTPException(422, 'Enter a commit name and a valid email address for your local commits.')
        with ctx.db() as s:
            row = s.get(Setting, 'git.identity.' + who.username)
            if row is None:
                row = Setting(key='git.identity.' + who.username)
                s.add(row)
            row.value = {'name': body.name.strip(), 'email': body.email}
            row.updated_by = who.username
            s.add(AuditLog(actor=who.username, action='git.identity.update', target=who.username))
            s.commit()
            return row.value

    def location(s, kind, key, who):
        if kind == 'project' and key.isdigit():
            project = ctx.project_for(s, who, int(key))
            path, account = project.workspace_path, project.account
            host = project.host or 'local'
        elif kind in ('session', 'workspace'):
            # Workspaces use a session id, so identical folder names in two accounts never collide.
            session = ctx.session_for(s, who, key)
            ctx.project_for(s, who, session.project_id)
            path, account, host = session.cwd, session.run_as, session.host
        else:
            raise HTTPException(404, 'That project or workspace wasn\'t found.')
        working = s.scalar(select(Session.id).join(Project, Session.project_id == Project.id).where(
            Session.desired_state == 'running', Session.run_as == account,
            func.coalesce(Session.team_host, Project.host, 'local') == host,
            func.coalesce(Session.worktree, Session.workdir, Project.workspace_path) == path)) is not None
        return {'path': path, 'account': account, 'host': host}, working

    def redactor(s):
        # Files can contain values left by any earlier session, including retired keys.
        ids = set(s.scalars(select(VaultKey.id))) | set(s.scalars(select(VaultRetired.key_id)))
        return ctx.supervisor.vault.redactor(s, ids)

    def redact(s, result):
        mask = redactor(s)
        safe = mask.deep(result)
        if 'content' in result:
            safe['redacted'] = safe['content'] != result['content']
        return safe

    async def call(where, **params):
        try:
            return await ctx.broker.call('files', **where, **params)
        except BrokerError as error:
            raise ctx.refused(error)

    @api.get('/api/files/{kind}/{key}')
    async def listing(kind: str, key: str, path: str = '', who=Depends(ctx.principal)):
        with ctx.db() as s:
            where, working = location(s, kind, key, who)
        result = await call(where, action='list', rel=path)
        with ctx.db() as s:
            return {**redact(s, result), 'working': working}

    @api.get('/api/files/{kind}/{key}/read')
    async def read(kind: str, key: str, path: str = '', who=Depends(ctx.principal)):
        with ctx.db() as s:
            where, _ = location(s, kind, key, who)
        result = await call(where, action='read', rel=path)
        with ctx.db() as s:
            return redact(s, result)

    @api.get('/api/files/{kind}/{key}/download')
    async def download(kind: str, key: str, path: str = '', who=Depends(ctx.principal)):
        with ctx.db() as s:
            where, _ = location(s, kind, key, who)
        result = await call(where, action='download', rel=path)
        raw = base64.b64decode(result['data'], validate=True)
        with ctx.db() as s:
            mask = redactor(s)
            try:
                text = raw.decode('utf-8')
                if '\0' in text:
                    raise UnicodeError()
                raw = mask.text(text).encode('utf-8')
                media_type = 'text/plain; charset=utf-8'
            except UnicodeError:
                decoded = raw.decode('utf-8', errors='replace')
                if mask.text(decoded) != decoded:
                    raise HTTPException(409, 'This file contains a secret value from Keys, so it can\'t be downloaded.')
                media_type = result.get('media_type') or 'application/octet-stream'
        # Inert on direct navigation; no SVG/HTML execution, sniffing, or shared caching.
        return Response(raw, media_type=media_type, headers={'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "sandbox; default-src 'none'",
            'Content-Disposition': download_disposition(path)})

    @api.post('/api/files/{kind}/{key}')
    async def mutate(kind: str, key: str, body: Mutation, who=Depends(ctx.principal)):
        with ctx.db() as s:
            where, _ = location(s, kind, key, who)
            person = identity(s, who)
            if body.message is not None and (not person['name'] or not person['email']):
                raise HTTPException(422, 'Set your Commit identity (name and email) before committing.')
        if body.action == 'write':
            current = await call(where, action='read', rel=body.path)
            with ctx.db() as s:
                if current.get('revision') == body.revision and redact(s, current).get('redacted'):
                    raise HTTPException(409, 'This file contains hidden secret values, so it can\'t be edited here. Ask your agent to edit it instead.')
        result = await call(where, action=body.action, rel=body.path, content=body.content, revision=body.revision,
                            destination=body.destination, message=body.message, **person)
        with ctx.db() as s:
            if not result.get('conflict'):
                paths = [body.path] + ([body.destination] if body.action == 'rename' else [])
                detail = {'paths': paths, 'operation': body.action}
                s.add(AuditLog(actor=who.username, action='files.delete' if body.action == 'delete' else 'files.write',
                               target=f'{kind}/{key}', detail=detail))
                if result.get('commit'):
                    s.add(AuditLog(actor=who.username, action='files.commit', target=f'{kind}/{key}',
                                   detail={'paths': paths, 'commit': result['commit']}))
                s.commit()
            if result.get('conflict'):
                result = {**result, 'current': redact(s, result['current'])}
            return redact(s, result)

    return api
