"""Personal layouts: browser-only, current authorization, revision-checked saves/resets."""
from fastapi import APIRouter, Depends, HTTPException, Request

from .. import layout_store as store
from ..db import write_in_thread, write_intent
from ..models import User
from .project_briefs import PrivateJSONResponse


def router(ctx):
    api = APIRouter(default_response_class=PrivateJSONResponse)

    def person(db, who, project_id):
        if who.user_id is None:
            raise HTTPException(403, 'Personal layouts require a person sign-in, not an installation API token.')
        user = db.get(User, who.user_id)
        if user is None or user.username != who.username:
            raise HTTPException(401, 'Sign in to continue.')
        return user, ctx.project_for(db, who, project_id)

    def access(db, who, project_id, device, viewport):
        user, _ = person(db, who, project_id)
        if device not in store.DEVICES or viewport not in store.VIEWPORTS:
            raise HTTPException(422, 'Choose desktop, tablet or phone and wide, medium or narrow.')
        key = (user.username, project_id, device, viewport)
        return key, store.scope(db, user, project_id, device, viewport)

    def mint(who, project_id, body):
        location, target, folder, path = body.get('location'), body.get('id'), body.get('folder', ''), body.get('path')
        with ctx.db() as db:
            user, _ = person(db, who, project_id)  # before validation: a foreign project is 404 for any body
            if (location not in store.LOCATIONS or not isinstance(target, str) or not store.IDENTIFIER.fullmatch(target)
                    or not store.clean_folder(folder) or not store.clean_path(path)):
                store.invalid()
            if location == 'project':
                if target != str(project_id):
                    store.invalid()
            else:  # the same resolver Files uses; a session of another project refuses like a missing one
                session = ctx.session_for(db, who, target)
                if session.project_id != project_id:
                    raise HTTPException(404, "That session wasn't found.")
            reference = store.file_ref(db, user.username, project_id, location, target, folder, path)
            db.commit()
            return {'file_ref': reference}

    @api.post('/api/projects/{project_id}/layouts/file-refs')
    async def file_ref(project_id: int, request: Request, who=Depends(ctx.principal)):
        body = await store.read_body(request)
        return await write_in_thread(mint, who, project_id, body)

    @api.get('/api/projects/{project_id}/layouts/{device}/{viewport}')
    @write_intent
    def read(project_id: int, device: str, viewport: str, who=Depends(ctx.principal)):
        with ctx.db() as db:
            key, identity = access(db, who, project_id, device, viewport)
            result = store.record(db, ctx, who, key, identity)
            db.commit()
            return result

    def mutate(who, project_id, device, viewport, body, reset):
        with ctx.db() as db:
            key, identity = access(db, who, project_id, device, viewport)
            layout = None if reset else store.normalize(body.get('layout'))
            accepted = store.save(db, key, body.get('expected_revision'), layout, reset=reset)
            db.flush()
            if accepted:
                store.prune_refs(db, key[0], key[1], reset=reset)
            db.expire_all()
            current = store.record(db, ctx, who, key, identity)
            db.commit()
            if not accepted:
                return PrivateJSONResponse({'detail': 'The layout changed. Choose which arrangement to keep.',
                                            'current': current}, status_code=409)
            return current

    @api.put('/api/projects/{project_id}/layouts/{device}/{viewport}')
    async def save(project_id: int, device: str, viewport: str, request: Request, who=Depends(ctx.principal)):
        body = await store.read_body(request)
        return await write_in_thread(mutate, who, project_id, device, viewport, body, False)

    @api.post('/api/projects/{project_id}/layouts/{device}/{viewport}/reset')
    async def reset(project_id: int, device: str, viewport: str, request: Request, who=Depends(ctx.principal)):
        body = await store.read_body(request)
        return await write_in_thread(mutate, who, project_id, device, viewport, body, True)

    return api
