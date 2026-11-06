"""Disposable browser-server presentation state; never installed in the product."""
import secrets

from sqlalchemy import MetaData, Table, delete, inspect, select
from starlette.responses import JSONResponse
from starlette.routing import Route

from acc.server.admin_features import FEATURES
from acc.server.models import Setting

PREFIXES = ('ui_shell.', 'setup_level.', 'ui_header.', 'theme.person.')
GROUPS = ('admin_features', 'ui_shell', 'setup_level', 'ui_header', 'theme', 'layouts')


class PresentationState:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    @staticmethod
    def group(key):
        return 'layouts' if key in ('personal_layout', 'layout_file_ref') else key.split('.')[0]

    def read(self):
        """Only non-default effective choices; revision bumps are not leaks."""
        result = {}
        with self.db() as db:
            row = db.get(Setting, 'admin_features')
            for key, value in (row.value.get('choices', {}) if row else {}).items():
                if key in FEATURES and value != FEATURES[key].default:
                    result['admin_features.' + key] = value
            for row in db.scalars(select(Setting)):
                if not row.key.startswith(PREFIXES):
                    continue
                value = row.value
                if row.key.startswith('ui_shell.') and value.get('enabled') is True:
                    continue
                if row.key.startswith('theme.person.'):
                    value = value.get('choice')
                    if value is None:
                        continue
                if row.key.startswith('setup_level.'):
                    value = {'level': value.get('level'), 'dismissed': value.get('dismissed') is True}
                    if value == {'level': None, 'dismissed': False}:
                        continue
                if row.key.startswith('ui_header.'):
                    from acc.server.api import settings_routes
                    default_header = getattr(settings_routes, 'default_header', None)
                    if default_header and value == default_header():
                        continue
                result[row.key] = value
            for table in self.layout_tables(db):
                rows = db.execute(select(table)).mappings().all()
                # A reset layout's revision-only tombstone carries no presentation.
                rows = [dict(row) for row in rows if table.name != 'personal_layout' or row['layout'] is not None]
                if rows:
                    result[table.name] = len(rows)
        return result

    @staticmethod
    def layout_tables(db):
        # These tables arrive with P2. Main without P2 still uses the same fixture.
        available = inspect(db.connection()).get_table_names()
        return [Table(name, MetaData(), autoload_with=db.connection())
                for name in ('personal_layout', 'layout_file_ref') if name in available]

    def reset(self, groups=None):
        groups = set(GROUPS if groups is None else groups)
        if groups - set(GROUPS):
            raise ValueError('Unknown presentation group')
        with self.db() as db:
            for row in db.scalars(select(Setting)):
                if row.key.startswith(PREFIXES) and self.group(row.key) in groups:
                    db.delete(row)
                elif row.key == 'admin_features' and 'admin_features' in groups:
                    row.value = {'choices': {}, 'revision': row.value.get('revision', 0) + 1}
            if 'layouts' in groups:
                for table in self.layout_tables(db):
                    db.execute(delete(table))
            db.commit()
            # The production middleware reads an immutable in-process snapshot.
            # Reset the owned fixture's snapshot as well as its database.
            self.settings.features.load(db)


def install(app):
    state = PresentationState(app.state.db, app.state.ctx.settings)
    token = secrets.token_urlsafe(32)
    previous_test = None

    async def presentation(request):
        nonlocal previous_test
        if not secrets.compare_digest(request.headers.get('x-e2e-isolation', ''), token):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        before = sorted(state.read())
        previous = previous_test
        if request.method == 'POST':
            body = await request.json()
            try:
                state.reset(body.get('groups'))
                if isinstance(body.get('test'), str):
                    previous_test = body['test'][:4096]
            except ValueError:
                return JSONResponse({'detail': 'Unknown presentation group.'}, status_code=400)
        # Names only: no private theme/document/layout values enter test reports.
        return JSONResponse({'changed': sorted(state.read()), 'before': before, 'previous_test': previous})

    app.router.routes.insert(0, Route('/e2e-test/presentation', presentation, methods=['GET', 'POST']))
    return token
