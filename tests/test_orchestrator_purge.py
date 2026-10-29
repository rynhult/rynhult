"""scripts/orchestrator_v2_purge.sql, the Guide's way to delete what the Orchestrator v2 preview kept (Orchestrator,
"Data it keeps"): it runs on a fully migrated database, covers every table the preview's migrations create, and
leaves everything else alone."""
import re
import sqlite3
from pathlib import Path

from acc.server.db import migrate

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'scripts/orchestrator_v2_purge.sql').read_text()
PREVIEW_MIGRATIONS = ['0069_orchestrator_worklist', '0070_selection', '0071_orchestrator_delivery',
                      '0072_orchestrator_gates', '0073_orchestrator_planning']


def test_the_script_covers_every_table_the_preview_creates():
    created = set()
    for name in PREVIEW_MIGRATIONS:
        text = (ROOT / f'src/acc/server/migrations/versions/{name}.py').read_text()
        created |= set(re.findall(r"create_table\(\s*'(\w+)'", text))
    assert created == set(re.findall(r'DELETE FROM (\w+);', SCRIPT))


def test_the_script_deletes_the_preview_data_and_keeps_the_rest(tmp_path):
    path = tmp_path / 'acc.db'
    migrate(f'sqlite:///{path}')
    db = sqlite3.connect(path)
    db.execute('PRAGMA foreign_keys = ON')
    for key in ('orchestrator.selection_rules', 'orchestrator.host_budget', 'orchestrator.gate_commands', 'guardrails'):
        db.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, '{}', '2026-10-27 00:00:00')", (key,))
    for key, resolved in (('worklist:1', '2026-10-27 00:00:00'), ('worklist:2', None), ('needs_input:x', '2026-10-27 00:00:00')):
        db.execute("INSERT INTO attention_item (dedupe_key, kind, severity, source, verified, title, created_at, updated_at,"
                   " resolved_at) VALUES (?, 'needs_input', 'warn', 'test', 1, 't', '2026-10-27 00:00:00',"
                   " '2026-10-27 00:00:00', ?)", (key, resolved))
    db.commit()
    db.executescript(SCRIPT)
    assert [r[0] for r in db.execute('SELECT key FROM setting')] == ['guardrails']
    assert sorted(r[0] for r in db.execute('SELECT dedupe_key FROM attention_item')) == ['needs_input:x', 'worklist:2']
    db.executescript(SCRIPT)  # a second run changes nothing and doesn't fail
    db.close()
