"""Git backup (M15): git's smart HTTP for brokers' backup pushes, and the project page's Backup card.

/api/git/<id>.git/ takes pushes only (receive-pack), each with the one-push token accd minted for that project.
Everything else here is for people: status, the on/off switch, "Back up now", and the backup as a git bundle.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from starlette.background import BackgroundTask

from .. import gitbackup
from ..db import WRITE_INTENT
from ..models import AuditLog, Node, Project
from ..nodes import verify_signature as node_proof
from ..supervisor_common import log
from .context import Context
from .schemas import BackupIn, project_out

RECEIVE_PACK = "git-receive-pack"


def router(ctx: Context) -> APIRouter:
    settings, db, supervisor = ctx.settings, ctx.db, ctx.supervisor
    backend = gitbackup.HttpBackend(settings.git_dir, settings.backup_max_push, settings.backup_timeout)
    api = APIRouter()

    def authorized(request: Request, repo: str) -> int:
        match = gitbackup.REPO_NAME.match(repo)
        if match is None:
            raise HTTPException(404, "That backup repository was not found.")
        project_id = int(match.group(1))
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not supervisor.backup_tokens.check(token.strip(), project_id):
            raise HTTPException(401, "A backup token for this project is required.",
                                headers={"WWW-Authenticate": 'Bearer realm="acc-backup"'})
        with db() as s:
            project = s.get(Project, project_id)
            if project is None or not project.git_backup:
                raise HTTPException(403, 'Backups are off for this project.')
            node = s.scalar(select(Node).where(Node.name == project.host)) if project.host != 'local' else None
            if getattr(request.state, 'edge_fingerprint', None) and not gitbackup.public_backup_origin(s, node.id if node else None):
                raise HTTPException(403, 'Backups through the public address are off.')
            owner = gitbackup.identity(project)
        path = gitbackup.repo_path(settings.git_dir, project_id)
        if not (path / "HEAD").is_file() or gitbackup.recorded_identity(path) != owner:
            raise HTTPException(404, "That backup repository was not found.")  # accd records the project in it before every push
        return project_id

    def repo_info(project_id: int, owner: str) -> dict | None:
        """What the backup at the project's id holds, if it is this project's (ids come back after a restore)."""
        if not gitbackup.belongs(settings.git_dir, project_id, owner):
            return None
        return gitbackup.repo_info(settings.git_dir, project_id)

    async def serve(request: Request, repo: str, path_info: str) -> Response:
        status, headers, body = await backend.run(request.method, f"/{repo}{path_info}", request.url.query,
                                                  {k.lower(): v for k, v in request.headers.items()}, request.stream())
        if status >= 400:
            log.warning("git backup: %s %s answered %d", request.method, repo, status)
        return Response(body, status_code=status, headers=dict(headers))

    @api.post("/api/git/{repo}/token")
    async def git_backup_token(repo: str, request: Request):
        match = gitbackup.REPO_NAME.fullmatch(repo)
        scheme, _, raw = request.headers.get('authorization', '').partition(' ')
        pid = int(match.group(1)) if match else 0
        entry = supervisor.backup_grants.lookup(raw, pid) if scheme.lower() == 'backupgrant' else None
        if entry is None:
            raise HTTPException(401, 'A current backup job grant is required')
        node_id = entry['node']
        channel = 'public' if getattr(request.state, 'edge_fingerprint', None) else 'private'
        def check_scope():
            with db() as s:
                project = s.get(Project, pid)
                node = s.get(Node, node_id)
                if not (gitbackup.public_backup_origin(s, node_id) and project is not None and project.git_backup
                        and node is not None and node.status == 'active' and project.host == node.name):
                    return 'Roaming git backup is not enabled for this machine and project'
                # The grant is bound to the machine it was issued for, on the path it is sent on (security review 7,
                # SR7-2): a grant shown to a fake public server is useless at the private address, and vice versa.
                if not node_proof(node.public_key, gitbackup.grant_proof_message(channel, pid, raw),
                                  request.headers.get(gitbackup.PROOF_HEADER, '')):
                    return 'The backup grant must be signed by the machine it was issued for'
                return None
        refusal = await asyncio.to_thread(check_scope)
        if refusal:
            raise HTTPException(403, refusal)

        # Recheck after the database wait; the job may have ended or expired.
        entry = supervisor.backup_grants.lookup(raw, pid)
        if entry is None:
            raise HTTPException(401, 'A current backup job grant is required')
        # Public requests have already passed EdgeGuard's own-leaf/project binding.
        entry['via'] = channel
        if entry['via'] == 'public' and not entry.get('audited'):
            # A credential crossed the public edge: record it whether or not the push then succeeds.
            entry['audited'] = True
            def record():
                intent = WRITE_INTENT.set(True)
                try:
                    with db() as s:
                        project, node = s.get(Project, pid), s.get(Node, node_id)
                        s.add(AuditLog(actor=f'node:{node.name if node else node_id}', action='git_backup.public_token',
                                       target=project.slug if project else str(pid),
                                       detail={'project_id': pid, 'node_id': node_id}))
                        s.commit()
                finally:
                    WRITE_INTENT.reset(intent)
            try:
                await asyncio.to_thread(record)
            except BaseException:
                # Marked first so concurrent retries audit once; a failed write unmarks it, so the next exchange on
                # this grant is still audited (security review 7, SR7-4).
                entry['audited'] = False
                raise
            entry = supervisor.backup_grants.lookup(raw, pid)
            if entry is None:
                raise HTTPException(401, 'A current backup job grant is required')
        return Response(json.dumps({'token': supervisor.backup_grants.exchange(raw, pid)}),
                        media_type='application/json', headers={'Cache-Control': 'no-store'})

    @api.get("/api/git/{repo}/info/refs")
    async def git_refs(repo: str, request: Request, service: str = ""):
        if service != RECEIVE_PACK:  # fetching (upload-pack) and the dumb protocol: never
            raise HTTPException(403, "Backups only accept git push; you can't fetch from them.")
        authorized(request, repo)
        return await serve(request, repo, "/info/refs")

    @api.post("/api/git/{repo}/" + RECEIVE_PACK)
    async def git_receive_pack(repo: str, request: Request):
        authorized(request, repo)
        return await serve(request, repo, "/" + RECEIVE_PACK)

    @api.post("/api/git/{repo}/git-upload-pack")
    async def git_upload_pack(repo: str):
        raise HTTPException(403, "Backups only accept git push; you can't fetch from them.")

    # ------------------------------------------------------------------ the project page

    @api.get("/api/projects/{project_id}/backup")
    async def backup_view(project_id: int, who=Depends(ctx.principal)):
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            enabled, owner = project.git_backup, gitbackup.identity(project)
        # backup_status opens its own session: never inside the one above (a POST's write lock would wait on itself)
        out = {"enabled": enabled, "configured": bool(settings.backup_base), **supervisor.backup_status(project_id)}
        out.pop("digest", None)
        out["repo"] = await asyncio.to_thread(repo_info, project_id, owner)
        return out

    @api.post("/api/projects/{project_id}/backup")
    async def set_backup(project_id: int, body: BackupIn, who=Depends(ctx.principal)):
        def write():
            with db() as s:
                project = ctx.project_for(s, who, project_id)
                project.git_backup = body.enabled
                s.add(AuditLog(actor=who.username, action="project.backup", target=project.slug,
                               detail={"enabled": body.enabled}))
                s.commit()
                if body.enabled:
                    supervisor.backup_soon(project_id)
                return project_out(project)
        result = await asyncio.to_thread(write)
        if not body.enabled and ctx.edge:
            await ctx.edge.sweep()  # open public pushes for this project end now, not at the next sweep (EI-3)
        return result

    @api.post("/api/projects/{project_id}/backup/run")
    async def backup_now(project_id: int, who=Depends(ctx.principal)):
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            if not project.git_backup:
                raise HTTPException(409, "Backups are off for this project. Turn them on in the Backup card first.")
        if not settings.backup_base:
            raise HTTPException(409, "Backups need this Rynhult's https address. Set ACC_BACKUP_URL in /etc/default/accd on the main machine.")
        await supervisor.backup_project(project_id, force=True)
        return await backup_view(project_id, who)

    @api.get("/api/projects/{project_id}/backup/bundle")
    async def backup_bundle(project_id: int, who=Depends(ctx.principal)):
        """The whole backup as one file: `git clone <file>` restores it. Owners, and the member whose project it is."""
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            slug, owner = project.slug, gitbackup.identity(project)
        info = await asyncio.to_thread(repo_info, project_id, owner)
        if not info or not info["branches"]:
            raise HTTPException(404, "Nothing has been backed up for this project yet.")
        settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="bundle-", dir=settings.data_dir))
        target = scratch / f"{slug}.bundle"
        try:
            await asyncio.to_thread(gitbackup.make_bundle, settings.git_dir, project_id, target)
        except Exception as error:
            shutil.rmtree(scratch, ignore_errors=True)
            raise HTTPException(500, f"Couldn't make the backup download: {error}") from None
        def audit_download():
            # This transaction only inserts: SQLite can wait for a writer without
            # upgrading a read snapshot. Wait off the event loop so that writer
            # can finish any awaited work. Keep the download audit mandatory.
            with db() as s:
                s.add(AuditLog(actor=who.username, action="project.backup_download", target=slug,
                               detail={"bytes": target.stat().st_size, "branches": len(info["branches"])}))
                s.commit()

        try:
            await asyncio.to_thread(audit_download)
        except Exception:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        return FileResponse(target, media_type="application/octet-stream", filename=target.name,
                            background=BackgroundTask(shutil.rmtree, scratch, ignore_errors=True))

    return api
