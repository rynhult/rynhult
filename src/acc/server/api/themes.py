"""Catalog-owned typed theme review and strictly personal presentation preferences."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .. import auth, catalog_import, themes
from ..catalog_store import catalog_lock
from ..catalog_themes import ThemePackages, ThemeReviews
from ..models import API_PRINCIPAL, AuditLog, AuthSession, Setting, User


class Install(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=1, max_length=100)


class Preview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,62}$')
    digest: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')


class Preference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str | None = Field(default=None, min_length=1, max_length=100)
    revision: int = Field(ge=0, strict=True)
    request_id: str = Field(pattern=r'^[a-zA-Z0-9-]{1,80}$')


def register(api: APIRouter, ctx, store):
    packages = ThemePackages(store)
    reviews = ThemeReviews()

    def human(who):
        if who.username == API_PRINCIPAL or who.user_id is None:
            raise HTTPException(403, 'Themes require a signed-in person, not an agent or installation token.')
        return who

    def current(request, owner=False):
        who = human(ctx.principal(request))
        if owner and not who.is_owner:
            raise HTTPException(403, 'Only an owner can import Catalog themes.')
        return who

    def publication_authority(s, request, who):
        # SQLite mutating requests use BEGIN IMMEDIATE. On PostgreSQL hold
        # the principal and login rows until this publication transaction ends,
        # serializing demotion/removal/logout with the authoritative decision.
        s.scalar(select(User).where(User.id == who.user_id).with_for_update())
        token = request.cookies.get(ctx.session_cookie)
        if token:
            s.scalar(select(AuthSession).where(AuthSession.token_hash == auth.digest(token)).with_for_update())
        record = auth.lookup_session(s, token,
                                     edge_fingerprint=getattr(request.state, 'edge_fingerprint', None),
                                     native=getattr(request.state, 'edge_native', False))
        if record is None or record.user_id != who.user_id or record.user.username != who.username:
            raise HTTPException(401, 'Sign in to continue.')
        if record.user.role != 'owner':
            raise HTTPException(403, 'Only an owner can import Catalog themes.')

    def failure(error):
        if isinstance(error, PermissionError):
            raise HTTPException(403, str(error)) from None
        if isinstance(error, catalog_import.ImportError_):
            raise HTTPException(410, str(error)) from None
        if isinstance(error, themes.ThemeError):
            raise HTTPException(409, str(error)) from None
        raise HTTPException(409, 'The theme package is unavailable. Built-in Rynhult remains available.') from None

    def preference_value(who):
        with ctx.db() as s:
            row = s.get(Setting, 'theme.person.' + who.username)
            value = dict(row.value) if row else {'choice': None, 'revision': 0}
        effective, unavailable = None, False
        if value['choice']:
            try:
                effective = packages.read(value['choice']['package_id'], value['choice']['digest'])
            except (ValueError, OSError):
                unavailable = True
        return {**{k: value[k] for k in ('choice', 'revision')}, 'effective': effective, 'unavailable': unavailable}

    @api.get('/themes')
    def list_themes(response: Response, who=Depends(ctx.principal)):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Vary'] = 'Cookie, Authorization'
        human(who)
        try:
            with catalog_lock(store.local_root):
                return packages.list(owner=who.is_owner)
        except (ValueError, OSError) as error:
            failure(error)

    @api.post('/themes/review')
    async def review_theme(request: Request, who=Depends(ctx.owner)):
        human(who)
        # Bounded intake even when the HTTP body limit is larger than this package type.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > themes.LIMIT:
                raise HTTPException(413, 'Theme archive limit is 2 MiB.')
        who = current(request, owner=True)
        try:
            result = themes.validate(bytes(raw))
            with catalog_lock(store.local_root):
                token = reviews.put({'raw': bytes(raw), 'result': result, 'actor': who.username,
                                     'revision': packages.revision(), 'kind': 'install'})
            return {**result, 'token': token, 'expires_at': time.time() + 900, 'scope': 'Catalog', 'type': 'theme'}
        except (ValueError, OSError) as error:
            if isinstance(error, themes.ThemeError):
                raise HTTPException(422, str(error)) from None
            failure(error)

    @api.post('/themes/install')
    def install_theme(body: Install, request: Request, who=Depends(ctx.owner)):
        human(who)
        try:
            with catalog_lock(store.local_root):
                who = current(request, owner=True)
                receipt = packages.receipt(who.username, body.token)
                if receipt and receipt.get('published'):
                    return {k: receipt[k] for k in ('package_id', 'digest')}
                previous = packages.index()
                if receipt:
                    result = receipt
                else:
                    payload = reviews.peek(body.token, who.username)
                    themes.require(payload['kind'] == 'install', 'This review is not a Catalog import.')
                    result, _ = packages.install(payload, who.username, body.token)
                receipt_id = packages.receipt_key(who.username, body.token)
                try:
                    with ctx.db() as s:
                        publication_authority(s, request, who)
                        prior = s.scalars(select(AuditLog).where(AuditLog.actor == who.username,
                                                                AuditLog.action == 'catalog.theme.install'))
                        if not any(row.detail.get('receipt') == receipt_id for row in prior):
                            s.add(AuditLog(actor=who.username, action='catalog.theme.install', target=result['package_id'],
                                           detail={**{k: result[k] for k in ('package_id', 'digest')}, 'receipt': receipt_id}))
                        s.commit()
                    # Audit must be durable before exposing the filesystem index.
                    # Commit released its locks: refresh again and hold authority
                    # through publication, including crash-retry pending receipts.
                    with ctx.db() as s:
                        publication_authority(s, request, who)
                        result = packages.publish(who.username, body.token)
                        s.commit()
                except Exception:
                    # Unreferenced immutable blobs are harmless. Restore the visible
                    # index/receipt if audit fails; crashes leave only hidden pending entries.
                    packages.atomic(packages.root / 'index.json', json.dumps(previous, sort_keys=True).encode())
                    raise
                reviews._items.pop(body.token, None)
                return result
        except (ValueError, OSError) as error:
            failure(error)

    @api.post('/themes/preview')
    def preview_theme(body: Preview, request: Request, who=Depends(ctx.principal)):
        human(who)
        try:
            with catalog_lock(store.local_root):
                who = current(request)
                result = packages.read(body.id, body.digest)
                token = reviews.put({'actor': who.username, 'kind': 'apply', 'choice': {'package_id': body.id, 'digest': body.digest},
                                     'revision': packages.revision()})
                return {**result, 'token': token, 'expires_at': time.time() + 900, 'scope': 'My account', 'type': 'theme'}
        except (ValueError, OSError) as error:
            failure(error)

    @api.post('/themes/cancel')
    def cancel_theme(body: Install, who=Depends(ctx.principal)):
        human(who)
        try:
            with catalog_lock(store.local_root):
                reviews.peek(body.token, who.username)
                reviews.take(body.token)
            return {'cancelled': True}
        except (ValueError, OSError) as error:
            failure(error)

    @api.get('/themes/preference')
    def get_theme_preference(response: Response, who=Depends(ctx.principal)):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Vary'] = 'Cookie, Authorization'
        human(who)
        with catalog_lock(store.local_root):
            return preference_value(who)

    @api.put('/themes/preference')
    def put_theme_preference(body: Preference, request: Request, who=Depends(ctx.principal)):
        human(who)
        try:
            with catalog_lock(store.local_root):
                who = current(request)
                key = 'theme.person.' + who.username
                with ctx.db() as s:
                    row = s.get(Setting, key)
                    old = dict(row.value) if row else {'choice': None, 'revision': 0}
                    if old.get('request_id') == body.request_id:
                        themes.require(old.get('request_token') == body.token, 'Idempotency key used for a different change.')
                    else:
                        themes.require(old['revision'] == body.revision, 'Theme preference changed. Reload before saving.')
                        choice = None
                        if body.token:
                            payload = reviews.peek(body.token, who.username)
                            themes.require(payload['kind'] == 'apply' and payload['revision'] == packages.revision(),
                                           'Catalog changed or wrong review type. Preview again.')
                            choice = payload['choice']
                            packages.read(choice['package_id'], choice['digest'])
                        value = {'choice': choice, 'previous': old['choice'], 'revision': old['revision'] + 1,
                                 'request_id': body.request_id, 'request_token': body.token}
                        s.merge(Setting(key=key, value=value, updated_by=who.username))
                        s.add(AuditLog(actor=who.username, action='theme.apply' if choice else 'theme.reset',
                                       target=who.username, detail={'choice': choice, 'revision': value['revision']}))
                        s.commit()
                        if body.token:
                            reviews.take(body.token)
                return preference_value(who)
        except (ValueError, OSError) as error:
            failure(error)
