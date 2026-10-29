"""Report a future ID backfill on an in-memory snapshot. Never migrate application tables.

Usage: PYTHONPATH=src python -m acc.organization_ids DATABASE_COPY --namespace UUID
Use one saved installation namespace for repeatable proposals. Output contains legacy
identifiers and must be treated as private migration material, not audit telemetry.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from pathlib import Path

# Explicit reviewable principal references. JSON, settings keys, paths and audit
# targets need semantic migration; they must not be rewritten by string replacement.
PRINCIPAL_COLUMNS = {
    'project': ('owner',), 'session': ('owner',), 'multirun': ('owner',),
    'node': ('created_by',), 'setting': ('updated_by',), 'audit_log': ('actor',),
    'retired_user': ('removed_by',), 'invite': ('created_by',),
    'attention_item': ('owner',), 'publish_request': ('owner', 'decided_by'),
    'tool_request': ('owner', 'decided_by'), 'installed_tool': ('owner',),
    'port_reservation': ('owner',), 'push_subscription': ('username',),
    'model_endpoint': ('created_by',), 'vault_key': ('owner',), 'vault_grant': ('given_by',),
    'schedule': ('owner',), 'acc_group': ('created_by',), 'placement': ('person', 'approved_by'),
    'group_merge_approval': ('approved_by',), 'group_key_grant': ('given_by',),
    'infrastructure_credential': ('owner',),
}
SYSTEM_ACTORS = ('@api', 'api', 'supervisor', 'acc', 'system', 'root')


def person_id(namespace: str, user_id: int) -> str:
    """The stable ID this report proposes for user row `user_id`; phase 1 persists the same value when it binds."""
    return str(uuid.uuid5(uuid.UUID(namespace), f'user:{user_id}'))


def report_copy(path: Path, *, namespace: str) -> dict:
    """Read SQLite through mode=ro and inspect only its consistent private snapshot."""
    ns = uuid.UUID(namespace)
    path = Path(path).resolve(strict=True)
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(':memory:') as db:
        source.backup(db)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {t: {r[1] for r in db.execute(f'PRAGMA table_info("{t}")')}
                   for t in set(PRINCIPAL_COLUMNS) | {'user', 'retired_user', 'session', 'acc_group', 'project'}
                   if t in tables}
        principals = {name: str(uuid.uuid5(ns, 'service:' + name)) for name in SYSTEM_ACTORS}
        entities = {}
        for table in ('user', 'retired_user', 'project', 'session', 'acc_group'):
            if table not in columns:
                continue
            key = 'username' if table == 'retired_user' else 'id'
            keys = [r[0] for r in db.execute(f'SELECT "{key}" FROM "{table}" ORDER BY "{key}"')]
            preserve = table in ('session', 'acc_group')
            entities[table] = {'rows': len(keys), 'preserve_existing_ids': preserve,
                               'proposed_ids': {str(k): str(k) if preserve else person_id(str(ns), k) if table == 'user'
                                                else str(uuid.uuid5(ns, f'{table}:{k}'))
                                                for k in keys}}
        if 'user' in columns:
            for key, name in db.execute('SELECT id, username FROM user'):
                if name in principals:
                    raise ValueError('reserved or duplicate identity collision; manual review required')
                principals[name] = entities['user']['proposed_ids'][str(key)]
        if 'retired_user' in columns:
            for name in entities['retired_user']['proposed_ids']:
                if name in principals:
                    raise ValueError('active/retired or reserved identity collision; manual review required')
                principals[name] = entities['retired_user']['proposed_ids'][name]
        references = {}
        for table, fields in PRINCIPAL_COLUMNS.items():
            for field in fields:
                if field not in columns.get(table, ()):
                    continue
                counts = {'resolved': 0, 'unresolved': 0, 'null': 0}
                for value, count in db.execute(f'SELECT "{field}", count(*) FROM "{table}" GROUP BY "{field}"'):
                    kind = 'null' if value is None else 'resolved' if value in principals else 'unresolved'
                    counts[kind] += count
                references[f'{table}.{field}'] = counts
        foreign_keys = {}
        # Names here come from sqlite_master, so quote embedded quotes as identifiers.
        for table in sorted(tables):
            quoted = table.replace('"', '""')
            for fk in db.execute(f'PRAGMA foreign_key_list("{quoted}")'):
                if fk[2] in ('user', 'project', 'session', 'acc_group'):
                    foreign_keys[f'{table}.{fk[3]}'] = {
                        'target': f'{fk[2]}.{fk[4]}',
                        'rows': db.execute(f'SELECT count(*) FROM "{quoted}"').fetchone()[0],
                        'operation': 'preserve' if fk[2] in ('session', 'acc_group') else 'map to opaque ID',
                    }
        settings = {prefix: db.execute('SELECT count(*) FROM setting WHERE substr(key,1,?)=?',
                                      (len(prefix), prefix)).fetchone()[0] if 'setting' in tables else 0
                    for prefix in ('budget.', 'tools.person.', 'risk_labels.', 'phone_banner.')}
        return {'schema_version': 1, 'namespace': str(ns), 'entities': entities, 'principals': principals,
                'foreign_keys': foreign_keys, 'name_keyed_settings': settings,
                'references': references, 'blocked': any(r['unresolved'] for r in references.values()),
                'manual_review': ['JSON/frozen launch specs', 'setting keys', 'audit targets/detail',
                                  'filesystem/broker/backup bindings', 'external references and queued work']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database_copy', type=Path)
    parser.add_argument('--namespace', required=True, help='Saved installation UUID (never regenerate between dry runs)')
    args = parser.parse_args()
    result = report_copy(args.database_copy, namespace=args.namespace)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result['blocked'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
