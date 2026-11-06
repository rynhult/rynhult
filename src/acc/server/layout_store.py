"""Bounded presentation references. No contents, paths, grants, or arbitrary extension bags."""
import hashlib
import json
import re
import secrets
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from .models import AuditLog, LayoutFileRef, PersonalLayout, Setting

MAX_BYTES = 32768
MAX_VIEWS = 32
MAX_COLUMNS = 3
MAX_FILE_REFS = 512  # per person and project; nine layouts of 32 views is 288
SCHEMA_VERSION = 2
DEVICES = {'desktop', 'tablet', 'phone'}
VIEWPORTS = {'wide', 'medium', 'narrow'}
VIEW_KINDS = {'terminal', 'chat', 'file', 'page'}
LOCATIONS = {'project', 'session', 'workspace'}
IDENTIFIER = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
TAB = re.compile(r'^[a-z][a-z-]{0,31}$')


def invalid():
    raise HTTPException(422, 'Choose a supported layout with bounded presentation references.')


def _resource(ref):
    if not isinstance(ref, dict) or ref.get('kind') not in ('project', 'session', 'team'):
        invalid()
    identifier = ref.get('id')
    if ref['kind'] == 'project':
        if type(identifier) is not int or not 0 < identifier < 2**53:
            invalid()
    elif not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
        invalid()
    return {'kind': ref['kind'], 'id': identifier}


def _flags(value, out):
    for key, fields in (('sidebar', ('collapsed',)), ('header', ('compact',))):
        if key in value:
            if not isinstance(value[key], dict):
                invalid()
            out[key] = {}
            for field in fields:
                if field in value[key]:
                    if type(value[key][field]) is not bool:
                        invalid()
                    out[key][field] = value[key][field]


def _view(view):
    if not isinstance(view, dict) or not isinstance(view.get('id'), str) or not IDENTIFIER.fullmatch(view['id']):
        invalid()
    kind = view.get('kind')
    if kind not in VIEW_KINDS:
        invalid()
    item = {'id': view['id'], 'kind': kind}
    if kind == 'file':
        reference = view.get('file_ref')
        if not isinstance(reference, str) or not IDENTIFIER.fullmatch(reference):
            invalid()
        item['file_ref'] = reference
        return item
    if 'resource' in view:
        item['resource'] = _resource(view['resource'])
    if kind in ('terminal', 'chat') and item.get('resource', {}).get('kind') != 'session':
        invalid()
    if kind == 'page' and 'tab' in view:
        if not isinstance(view['tab'], str) or not TAB.fullmatch(view['tab']):
            invalid()
        item['tab'] = view['tab']
    return item


def normalize(value):
    """Ignore unknown fields, including nested fields; never persist untyped payloads.

    Saves are version 2 only: an older client must reload, not write a shape this
    server would have to guess at. Unknown fields are dropped, not round-tripped:
    they cannot smuggle editor contents or capabilities into storage.
    """
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != SCHEMA_VERSION:
        invalid()
    views, columns = value.get('views'), value.get('columns')
    if (not isinstance(views, list) or len(views) > MAX_VIEWS or not isinstance(columns, list)
            or not 1 <= len(columns) <= MAX_COLUMNS):
        invalid()
    out = {'schema_version': SCHEMA_VERSION, 'views': [_view(view) for view in views], 'columns': []}
    ids = [view['id'] for view in out['views']]
    if len(set(ids)) != len(ids):
        invalid()
    placed, total = set(), 0
    for column in columns:
        if not isinstance(column, dict) or not isinstance(column.get('tabs'), list):
            invalid()
        width = column.get('width')
        if type(width) not in (int, float) or not 0 < width <= 1:  # also refuses NaN, infinity and huge integers
            invalid()
        tabs, active = column['tabs'], column.get('active')
        if any(not isinstance(tab, str) or tab not in ids or tab in placed for tab in tabs) or len(set(tabs)) != len(tabs):
            invalid()
        if active is not None and (not isinstance(active, str) or active not in tabs) or tabs and active is None:
            invalid()
        placed.update(tabs)
        total += width
        out['columns'].append({'tabs': list(tabs), 'active': active, 'width': width})
    if placed != set(ids) or abs(total - 1) > 0.001:
        invalid()
    focused, filled = value.get('focused'), value.get('filled')
    if type(focused) is not int or not 0 <= focused < len(columns) or filled is not None and (not isinstance(filled, str) or filled not in placed):
        invalid()
    out['focused'], out['filled'] = focused, filled
    _flags(value, out)
    return out


def migrate(layout):
    """Read a version 1 row as one column of its views. The row itself changes only on the next explicit save.

    Geometry has no version 2 meaning. A view that named no resource stays as a
    reference without one, and so restores as an unavailable placeholder.
    """
    if not layout or layout.get('schema_version') == SCHEMA_VERSION:
        return layout
    views = []
    for view in layout.get('views', []):
        resource = view.get('resource')
        kind = 'terminal' if resource and resource['kind'] == 'session' else 'page'
        views.append({'id': view['id'], 'kind': kind, **({'resource': resource} if resource else {})})
    ids = [view['id'] for view in views]
    selected = layout.get('selected_tab')
    out = {'schema_version': SCHEMA_VERSION, 'views': views, 'focused': 0, 'filled': None,
           'columns': [{'tabs': ids, 'active': selected if selected in ids else (ids[0] if ids else None), 'width': 1}]}
    for key in ('sidebar', 'header'):
        if key in layout:
            out[key] = layout[key]
    return out


def scope(db, user, project_id, device, viewport):
    setting = db.get(Setting, 'organizations.namespace')
    if setting is None:
        setting = Setting(key='organizations.namespace', value={'uuid': str(uuid.uuid4())}, updated_by='system')
        db.add(setting)
        db.flush()
    namespace = setting.value['uuid']
    return {'installation': namespace,
            'person': str(uuid.uuid5(uuid.UUID(namespace), 'layout-person:' + user.username)),
            'project_id': project_id, 'device': device, 'viewport': viewport}


def _resolve(db, ctx, who, key, reference):
    """Current authorization decides. Another person's or an unknown reference reads the same as a revoked one."""
    row = db.get(LayoutFileRef, reference)
    if row is None or row.owner != key[0] or row.project_id != key[1]:
        return None
    if row.location == 'project':
        project = ctx.project_for(db, who, row.project_id)
        if project.archived or project.deleting:
            return None
    else:
        session = ctx.session_for(db, who, row.target)
        if session.project_id != row.project_id or session.desired_state == 'archived':
            return None
        ctx.project_for(db, who, session.project_id)
    return {'location': row.location, 'id': row.target, 'folder': row.folder, 'path': row.path}


def record(db, ctx, who, key, identity):
    row = db.get(PersonalLayout, key)
    layout = migrate(row.layout if row else None)
    resources = {}
    for view in (layout or {}).get('views', []):
        ref = view.get('resource')
        available, file = False, None
        try:
            if view.get('file_ref'):
                file = _resolve(db, ctx, who, key, view['file_ref'])
                available = file is not None
            elif ref:
                if ref['kind'] == 'project':
                    resource = ctx.project_for(db, who, ref['id'])
                    available = not resource.archived and not resource.deleting
                else:
                    resource = ctx.session_for(db, who, ref['id'])
                    available = resource.project_id == identity['project_id'] and resource.desired_state != 'archived'
                    if ref['kind'] == 'team':
                        from .models import Team
                        available = available and db.get(Team, ref['id']) is not None
        except HTTPException:
            available, file = False, None
        # The layout never holds a path. A file view's location appears only here,
        # recomputed for this person on every response. Availability is a hint: the
        # resource API checks authorization again when the view is opened.
        resources[view['id']] = {'available': bool(available), **({'file': file} if file else {})}
    return {'scope': identity, 'revision': row.revision if row else 0,
            'schema_version': SCHEMA_VERSION, 'layout': layout, 'resources': resources}


def _storable(text):
    """Valid UTF-8 with no control characters: a lone surrogate or NUL can't reach the database or a route."""
    try:
        text.encode('utf-8')
    except UnicodeError:
        return False
    return not re.search(r'[\x00-\x1f\x7f]', text)


def clean_path(path):
    return (isinstance(path, str) and 0 < len(path) <= 2048 and not path.startswith('/') and '\\' not in path
            and _storable(path) and all(part not in ('', '.', '..') for part in path.split('/')))


def clean_folder(folder):
    return (isinstance(folder, str) and len(folder) <= 255 and _storable(folder) and '/' not in folder
            and '\\' not in folder and folder not in ('.', '..'))


def prune_refs(db, owner, project_id, *, reset=False):
    """A path is kept only while a saved layout uses it. Reset removes all of this person's for the project."""
    where = (LayoutFileRef.owner == owner, LayoutFileRef.project_id == project_id)
    used = set()
    if not reset:
        for layout in db.scalars(select(PersonalLayout.layout).where(PersonalLayout.owner == owner, PersonalLayout.project_id == project_id)):
            used.update(view['file_ref'] for view in (layout or {}).get('views', []) if view.get('file_ref'))
    db.execute(delete(LayoutFileRef).where(*where, LayoutFileRef.id.not_in(used)))


def file_ref(db, owner, project_id, location, target, folder, path):
    """The same file gets the same opaque reference, so saved layouts stay comparable between windows."""
    digest = hashlib.sha256(path.encode()).hexdigest()
    where = (LayoutFileRef.owner == owner, LayoutFileRef.project_id == project_id, LayoutFileRef.location == location,
             LayoutFileRef.target == target, LayoutFileRef.path_hash == digest)
    existing = db.scalar(select(LayoutFileRef).where(*where))
    if existing:
        return existing.id
    rows = db.scalars(select(LayoutFileRef).where(LayoutFileRef.owner == owner, LayoutFileRef.project_id == project_id)
                      .order_by(LayoutFileRef.created_at)).all()
    if len(rows) >= MAX_FILE_REFS:
        used = set()
        for layout in db.scalars(select(PersonalLayout.layout).where(PersonalLayout.owner == owner, PersonalLayout.project_id == project_id)):
            used.update(view['file_ref'] for view in (layout or {}).get('views', []) if view.get('file_ref'))
        spare = next((row for row in rows if row.id not in used), None)
        if spare is None:
            raise HTTPException(409, 'Too many saved file views in this project. Close some and try again.')
        db.execute(delete(LayoutFileRef).where(LayoutFileRef.id == spare.id))
    identifier = secrets.token_urlsafe(18)
    try:
        with db.begin_nested():
            db.add(LayoutFileRef(id=identifier, owner=owner, project_id=project_id, location=location, target=target,
                                 folder=folder, path=path, path_hash=digest))
            db.flush()
    except IntegrityError:  # another window minted this file's reference first
        return db.scalar(select(LayoutFileRef.id).where(*where))
    return identifier


def save(db, key, expected, layout, *, reset=False):
    if type(expected) is not int or not 0 <= expected < 2**53:
        invalid()
    if expected == 0:
        # A SAVEPOINT makes a concurrent first insert a normal CAS conflict on
        # SQLite and PostgreSQL without aborting the surrounding transaction.
        try:
            with db.begin_nested():
                db.add(PersonalLayout(**dict(zip(('owner', 'project_id', 'device', 'viewport'), key, strict=True)),
                                      revision=1, layout=layout))
                db.flush()
        except IntegrityError:
            return False
    else:
        query = update(PersonalLayout).where(
            PersonalLayout.owner == key[0], PersonalLayout.project_id == key[1],
            PersonalLayout.device == key[2], PersonalLayout.viewport == key[3], PersonalLayout.revision == expected)
        if db.execute(query.values(revision=expected + 1, layout=layout)).rowcount != 1:
            return False
    if reset:
        db.add(AuditLog(actor=key[0], action='layout.reset', target=str(key[1]),
                       detail={'device': key[2], 'viewport': key[3], 'revision': expected + 1}))
    return True


async def read_body(request):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, 'The layout is too large (32 KiB maximum).')
        raw.extend(chunk)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        invalid()
    if not isinstance(value, dict):
        invalid()
    return value
