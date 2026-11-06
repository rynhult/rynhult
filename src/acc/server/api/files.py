"""Authorized file access: accd only handles broker replies, never agent filesystem paths."""
from __future__ import annotations

import base64
import hashlib
import json
import posixpath
import re
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from acc.broker.files import MAX_UPLOAD, MAX_UPLOAD_BATCH

from ..broker_client import BrokerBusy, BrokerError
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
    file_identity: str | None = Field(None, pattern=r'^[a-f0-9]{64}$')
    workspace_revision: str | None = Field(None, pattern=r'^[a-f0-9]{64}$')
    destination: str = Field('', max_length=4096)
    message: str | None = Field(None, min_length=1, max_length=2000)


class UploadFile(BaseModel):
    path: str = Field(max_length=4096)
    data: str
    overwrite: str = Field('refuse', pattern='^(refuse|replace|keep-both)$')
    revision: str | None = Field(None, max_length=100)


class UploadBatch(BaseModel):
    files: list[UploadFile] = Field(default_factory=list, max_length=100)
    terminal_upload: str | None = Field(None, max_length=100)


class SearchEntry(BaseModel):
    name: str
    path: str
    kind: Literal['file']


class SearchResult(BaseModel):
    results: list[SearchEntry]
    visited: int
    truncated: bool
    reason: str | None


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

    def file_identity(where, rel):
        # Authorized placement, not a display name or the route alias used to reach it.
        return hashlib.sha256(json.dumps([where['host'], where['account'], where['path'], rel],
                                         separators=(',', ':')).encode()).hexdigest()

    @api.get('/api/files/{kind}/{key}')
    async def listing(kind: str, key: str, path: str = '', who=Depends(ctx.principal)):
        with ctx.db() as s:
            where, working = location(s, kind, key, who)
        result = await call(where, action='list', rel=path)
        with ctx.db() as s:
            return {**redact(s, result), 'working': working}

    searches = set()

    def search_binding(request: Request, who=Depends(ctx.principal)):
        # Dependencies resolve before FastAPI validates endpoint query fields.
        # Unauthorized callers must get the same refusal with any query shape.
        params = request.path_params
        if 'project_id' in params:
            kind, key = 'project', str(params['project_id'])
        elif 'acc_id' in params:
            kind, key = 'session', params['acc_id']
        else:
            kind, key = params['kind'], params['key']
        with ctx.db() as s:
            where, _ = location(s, kind, key, who)
        return where, who

    async def search_location(binding, q, limit):
        from acc.broker.file_search import validate
        from acc.broker.ops import OpError
        where, who = binding
        try:
            validate(q, limit)
        except OpError as error:
            raise HTTPException(422, str(error)) from None
        from ..ha import Fenced
        slot = (who.username, where.get('host'), where.get('account'), where['path'])
        if slot in searches:
            return JSONResponse(status_code=429, content={'state': 'unavailable',
                'detail': 'A search is already running for this location. Try again shortly.'})
        searches.add(slot)
        client = None
        try:
            client = ctx.broker.fresh(where.get('account'), host=where.get('host'), timeout=3)
            result = await client.call('files_search', path=where['path'], q=q, limit=limit)
        except BrokerBusy:
            return JSONResponse(status_code=429, headers={'Retry-After': '1'}, content={'state': 'busy',
                'detail': 'File search is busy for this account. Try again shortly.'})
        except (BrokerError, Fenced):
            return JSONResponse(status_code=503, content={'state': 'unavailable',
                'detail': 'File search is unavailable for this location. Reconnect or update its account service and try again.'})
        finally:
            searches.discard(slot)
            if client is not None:
                await client.close()
        with ctx.db() as s:
            return redact(s, result)

    @api.get('/api/projects/{project_id}/files/search', response_model=SearchResult)
    async def project_search(project_id: int, q: str = Query(min_length=1, max_length=200),
                             limit: int = Query(20, ge=1, le=100), binding=Depends(search_binding)):
        return await search_location(binding, q, limit)

    @api.get('/api/sessions/{acc_id}/files/search', response_model=SearchResult)
    async def session_search(acc_id: str, q: str = Query(min_length=1, max_length=200),
                             limit: int = Query(20, ge=1, le=100), binding=Depends(search_binding)):
        return await search_location(binding, q, limit)

    @api.get('/api/files/{kind}/{key}/search', response_model=SearchResult)
    async def search(kind: str, key: str, q: str = Query(min_length=1, max_length=200),
                     limit: int = Query(20, ge=1, le=100), binding=Depends(search_binding)):
        return await search_location(binding, q, limit)

    @api.get('/api/files/{kind}/{key}/read')
    async def read(kind: str, key: str, path: str = '', who=Depends(ctx.principal)):
        with ctx.db() as s:
            where, _ = location(s, kind, key, who)
        result = await call(where, action='read', rel=path)
        with ctx.db() as s:
            return {**redact(s, result), 'file_identity': file_identity(where, path)}

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

    @api.post('/api/files/{kind}/{key}/upload')
    async def upload(kind: str, key: str, body: UploadBatch, who=Depends(ctx.principal)):
        with ctx.db() as s:
            where, _ = location(s, kind, key, who)
        if body.terminal_upload is not None:
            if kind != 'session' or not 1 <= len(body.files) <= 10:
                raise HTTPException(422, 'Paste or drop between 1 and 10 files into a session.')
            writer = ctx.writers.by_session.get(key)
            controller = ctx.writers.controllers.get(writer)
            if (writer is None or (getattr(writer, 'query_params', None) or {}).get('upload_id') != body.terminal_upload
                    or getattr(controller, 'actor', None) != who.username):
                raise HTTPException(403, 'Read-only terminal: acquire control before uploading files.')
        upload_epoch = ctx.writers.epoch(key)
        total = 0
        invalid = {}
        for index, item in enumerate(body.files):
            if len(item.data) > ((MAX_UPLOAD + 2) // 3) * 4:
                invalid[index] = 'Upload exceeds the 4 MiB per-file limit.'
                continue
            try:
                raw = base64.b64decode(item.data, validate=True)
                total += len(raw)
                if len(raw) > MAX_UPLOAD:
                    invalid[index] = 'Upload exceeds the 4 MiB per-file limit.'
            except ValueError:
                invalid[index] = 'Invalid upload encoding.'
        if total > MAX_UPLOAD_BATCH:
            raise HTTPException(413, 'Uploads exceed the 16 MiB batch limit.')
        results = []
        for index, item in enumerate(body.files):
            try:
                if index in invalid:
                    raise HTTPException(422, invalid[index])
                if body.terminal_upload is not None and (ctx.writers.by_session.get(key) is not writer or ctx.writers.epoch(key) != upload_epoch):
                    raise HTTPException(403, 'Read-only terminal: control changed during upload.')
                if item.overwrite == 'replace' and body.terminal_upload is None:
                    current = await call(where, action='download', rel=item.path)
                    with ctx.db() as s:
                        text = base64.b64decode(current['data'], validate=True).decode('utf-8', errors='replace')
                        if redactor(s).text(text) != text:
                            raise HTTPException(409, 'This file contains hidden secret values, so it cannot be replaced here.')
                result = await call(where, action='terminal-upload' if body.terminal_upload is not None else 'upload', rel=item.path, data=item.data,
                                    overwrite=item.overwrite, revision=item.revision)
                results.append({'requested_path': item.path, **result})
            except HTTPException as error:
                results.append({'requested_path': item.path, 'error': str(error.detail)})
        with ctx.db() as s:
            safe = redact(s, {'files': results})
            s.add(AuditLog(actor=who.username, action='files.upload', target=f'{kind}/{key}',
                           detail={'files': [{k: v for k, v in row.items() if k in {
                               'requested_path', 'path', 'size', 'conflict', 'error', 'previous_copy', 'incoming_copy'}} for row in safe['files']]}))
            s.commit()
        return safe

    @api.post('/api/files/{kind}/{key}')
    async def mutate(kind: str, key: str, body: Mutation, who=Depends(ctx.principal)):
        with ctx.db() as s:
            where, _ = location(s, kind, key, who)
            if body.file_identity is not None and body.file_identity != file_identity(where, body.path):
                raise HTTPException(409, 'The file placement changed. Reopen it before saving.')
            if (body.file_identity is None) != (body.workspace_revision is None):
                raise HTTPException(422, 'The file placement and folder revision must be supplied together.')
            person = identity(s, who)
            if body.message is not None and (not person['name'] or not person['email']):
                raise HTTPException(422, 'Set your Commit identity (name and email) before committing.')
        if body.action == 'write':
            current = await call(where, action='read', rel=body.path)
            with ctx.db() as s:
                if current.get('revision') == body.revision and redact(s, current).get('redacted'):
                    raise HTTPException(409, 'This file contains hidden secret values, so it can\'t be edited here. Ask your agent to edit it instead.')
        result = await call(where, action=body.action, rel=body.path, content=body.content, revision=body.revision,
                            destination=body.destination, message=body.message,
                            **({'workspace_revision': body.workspace_revision} if body.workspace_revision else {}), **person)
        if body.action == 'write':
            result = {**result, 'file_identity': file_identity(where, body.path)}
        with ctx.db() as s:
            if not result.get('conflict') or result.get('incoming_copy'):
                paths = [body.path] + ([body.destination] if body.action == 'rename' else [])
                detail = {'paths': paths, 'operation': body.action,
                          **{k: result[k] for k in ('previous_copy', 'incoming_copy') if k in result}}
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
