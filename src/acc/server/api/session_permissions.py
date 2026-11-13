"""Person-owned inspection and narrowing; agents have no grant mutation tool."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .. import session_caps
from ..models import API_PRINCIPAL
from .security_settings import browser


class Narrow(BaseModel):
    model_config = ConfigDict(extra='forbid')
    permissions: dict


def router(ctx):
    api = APIRouter()

    @api.get('/api/sessions/{session_id}/permissions')
    def inspect_permissions(session_id: str, who=Depends(ctx.principal)):
        with ctx.db() as db:
            row = ctx.session_for(db, who, session_id)
            return {'requested': session_caps.normalize(row.cap_requested), 'effective': session_caps.effective(db, row),
                    'generation_snapshot': row.cap_effective, 'parent_chain': row.parent_chain,
                    'root_owner': row.root_owner, 'native_helpers_capped': False}

    @api.patch('/api/sessions/{session_id}/permissions')
    def narrow_permissions(session_id: str, body: Narrow, request: Request, who=Depends(ctx.principal)):
        with ctx.db() as db:
            row = ctx.session_for(db, who, session_id)
            browser(db, request, who)
            if row.root_owner != who.username and not (row.root_owner == API_PRINCIPAL and who.is_owner):
                raise HTTPException(403, 'Only the root session’s person may narrow its permissions.')
            from ..security_settings import lock_revision
            lock_revision(db)
            try:
                result = session_caps.narrow(db, row, body.permissions, who.username)
            except ValueError as error:
                raise HTTPException(409, str(error)) from None
            db.commit()
            return {'effective': result}

    return api
