"""Owner browser management of the write-only dashboard GitHub credential."""
import asyncio
import os
import tempfile

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, SecretStr

from .. import passkeys
from ..github import GitHub
from ..models import AuditLog, User
from .context import Context


class TokenIn(BaseModel):
    token: SecretStr


async def validate(token: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get('https://api.github.com/user', headers={
                'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28'})
        if response.status_code != 200:
            raise ValueError
        login = response.json()['login']
        if not isinstance(login, str) or not login:
            raise ValueError
        return {'login': login, 'scopes': response.headers.get('x-oauth-scopes', '')}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(400, 'GitHub could not validate this token. Check the token and try again.') from None


def router(ctx: Context) -> APIRouter:
    api = APIRouter()
    lock = asyncio.Lock()
    path = ctx.settings.github_token_path

    def status():
        try:
            token = path.read_text().strip()
        except FileNotFoundError:
            token = ''
        return {'set': bool(token), 'suffix': token[-4:] if len(token) > 4 else None}

    def verified(request, who):
        with ctx.db() as db:
            passkeys.recently_verified(db, getattr(request.state, 'session_hash', None),
                                      db.get(User, who.user_id) if who.user_id else None)

    async def reset_client(token):
        old = ctx.supervisor._github
        ctx.supervisor._github = GitHub(token) if token else None
        if old:
            await old.client.aclose()

    @api.get('/api/providers/github-token')
    def get_token(who=Depends(ctx.owner)):
        return status()

    @api.put('/api/providers/github-token')
    async def save_token(body: TokenIn, request: Request, who=Depends(ctx.owner)):
        verified(request, who)
        token = body.token.get_secret_value().strip()
        if not 8 <= len(token) <= 4096 or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token):
            raise HTTPException(400, 'Enter a valid GitHub token.')
        async with lock:
            identity = await validate(token)
            verified(request, who)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.github-token-')
            try:
                with os.fdopen(fd, 'w') as file:
                    os.fchmod(file.fileno(), 0o600)
                    file.write(token + '\n')
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, path)  # daemon-owned, 0600, including when replacing a legacy file
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            await reset_client(token)
            with ctx.db() as db:
                db.add(AuditLog(actor=who.username, action='github_token.save', target='github'))
                db.commit()
            return {**status(), **identity}

    @api.delete('/api/providers/github-token')
    async def remove_token(request: Request, who=Depends(ctx.owner)):
        async with lock:
            verified(request, who)
            path.unlink(missing_ok=True)
            await reset_client(None)
            with ctx.db() as db:
                db.add(AuditLog(actor=who.username, action='github_token.remove', target='github'))
                db.commit()
            return status()

    return api
