"""Replacement activation: the refusal and fail-closed paths the main flow tests don't reach. Temporary files only."""
import base64
import dataclasses
import fcntl
import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest
from test_backup_activation import approve, controller, prepared, source  # noqa: F401 (fixtures)

from acc import backup_activation as activation
from acc.backup_activation import Refused


def last_audit(layout):
    return json.loads(layout.audit.read_text().splitlines()[-1])


def rewrite_plan(layout, **changes):
    plan = {**json.loads((layout.prepared / 'plan.json').read_text()), **changes}
    (layout.prepared / 'plan.json').write_text(json.dumps(plan))
    return plan


def test_directories_are_absolute_plain_and_never_reached_through_a_link(tmp_path):
    for path in ('relative/dir', '/tmp/../etc', '../etc'):  # (pathlib already drops '.')
        with pytest.raises(Refused, match='absolute folder path'):
            activation._directory(path)
    (tmp_path / 'real/inner').mkdir(parents=True)
    (tmp_path / 'link').symlink_to(tmp_path / 'real')
    with pytest.raises(OSError):
        activation._directory(tmp_path / 'link/inner')
    os.close(activation._directory(tmp_path / 'real/inner'))


def test_control_files_must_be_private_single_link_objects(tmp_path):
    me = os.getuid()
    folder = tmp_path / 'etc'
    folder.mkdir()
    policy = folder / 'policy.json'
    policy.write_text('{"enabled": true}')
    assert activation._json(policy, owner=me) == {'enabled': True}
    folder.chmod(0o777)
    try:
        with pytest.raises(Refused, match='missing, unreadable or not valid JSON'):
            activation._json(policy, owner=me)
    finally:
        folder.chmod(0o755)
    with pytest.raises(Refused, match='folder holding a replacement control file must be owned by root'):
        activation._read(policy, 1024, owner=me + 1)
    os.link(policy, folder / 'second-name')
    with pytest.raises(Refused, match='single plain file'):
        activation._read(policy, 1024, owner=me)
    (folder / 'second-name').unlink()
    with pytest.raises(Refused, match='single plain file'):
        activation._read(policy, 4)
    policy.write_text('["not", "an", "object"]')
    with pytest.raises(Refused, match='missing, unreadable or not valid JSON'):
        activation._json(policy, owner=me)


@pytest.mark.parametrize('name', ['etc/passwd', 'data', 'data/acc.db/../../etc/shadow', 'data//acc.db', 'data/./acc.db',
                                  'git/a\\b', 'git/a\0b', 'data/other.db', 'data/backup-service/other.key',
                                  'runs/x', '/data/acc.db', ''])
def test_root_installs_only_the_fixed_payload_paths(name):
    with pytest.raises(Refused):
        activation._allowed(name)


@pytest.mark.parametrize('name', ['data/acc.db', 'data/RESTORE-HOLD', 'data/REPLACEMENT-ID', 'data/secrets/vault.key',
                                  'data/backup-service/identity.json', 'git/1.git/HEAD', 'catalog-local/local.json'])
def test_the_fixed_payload_paths_are_installable(name):
    activation._allowed(name)


def test_inventory_refuses_linked_components_and_folders_and_counts_files(tmp_path, monkeypatch):
    root = tmp_path / 'prepared'
    (root / 'data').mkdir(parents=True)
    (root / 'data/acc.db').write_bytes(b'db')
    (tmp_path / 'elsewhere').mkdir()
    (root / 'git').symlink_to(tmp_path / 'elsewhere', target_is_directory=True)
    with pytest.raises(Refused, match='prepared files contain a link'):
        activation._inventory(root)
    (root / 'git').unlink()
    (root / 'data/secrets').symlink_to(tmp_path / 'elsewhere', target_is_directory=True)
    with pytest.raises(Refused, match='prepared files contain a link'):
        activation._inventory(root)
    (root / 'data/secrets').unlink()
    (root / 'data/secrets').mkdir()
    (root / 'data/secrets/vault.key').write_bytes(b'k')
    assert set(activation._inventory(root)) == {'data/acc.db', 'data/secrets/vault.key'}
    monkeypatch.setattr(activation, 'MAX_FILES', 1)
    with pytest.raises(Refused, match='file-count limit'):
        activation._inventory(root)


def test_schema_canonical_form_keeps_quoted_literals_and_refuses_unbalanced_ddl():
    ddl = "CREATE TABLE t (b TEXT DEFAULT 'it''s, (not) a clause', a INTEGER NOT NULL)"
    head, clauses, tail = activation._schema_sql(ddl)
    assert (head, tail) == ('CREATE TABLE t ', ')')
    assert clauses == ('a INTEGER NOT NULL', "b TEXT DEFAULT 'it''s, (not) a clause'")
    assert activation._schema_sql('CREATE INDEX i ON t (a)') == 'CREATE INDEX i ON t (a)'
    with pytest.raises(Refused, match='read the restored database schema'):
        activation._schema_sql("CREATE TABLE t (a TEXT DEFAULT ')'")


def test_a_restored_table_with_reordered_columns_is_refused(tmp_path):
    # Clause order is canonicalised (Alembic emits sets), so column order needs its own check: without it a restored
    # table could put values in different columns than the trusted release reads.
    from acc.server.db import migrate
    database = tmp_path / 'restored.db'
    migrate(f'sqlite:///{database}')
    activation._trusted_schema(database, tmp_path)
    with sqlite3.connect(database) as db:
        (ddl,) = db.execute("SELECT sql FROM sqlite_master WHERE name='setting'").fetchone()
        head, clauses, tail = activation._schema_sql(ddl)
        columns = [c.strip() for c in ddl[len(head) + 1:-len(tail)].split(', \n')]  # "key", value, ...: no nesting
        assert columns[0] == '"key" VARCHAR(120) NOT NULL' and columns[1] == 'value JSON NOT NULL'
        swapped = head + '(' + ', '.join([columns[1], columns[0], *columns[2:]]) + tail
        assert activation._schema_sql(swapped) == (head, clauses, tail)
        db.execute('PRAGMA writable_schema=ON')
        db.execute("UPDATE sqlite_master SET sql=? WHERE name='setting'", (swapped,))
    with pytest.raises(Refused, match='in its column order'):
        activation._trusted_schema(database, tmp_path)


class FakeDb:
    def __init__(self, row, node):
        self.row, self.node = row, node

    def get(self, model, key):
        return self.row

    def scalar(self, query):
        return self.node


def resume_case(public_key=None, **binding):
    key = public_key or base64.b64encode(b'node key').decode()
    node = SimpleNamespace(id='a' * 32, public_key=key)
    value = {'resume_sessions': ['2' * 32], 'reviewed_nodes': {'a' * 32: hashlib.sha256(b'node key').hexdigest()},
             'resume_bindings': {'2' * 32: {'node_id': 'a' * 32, 'account': 'rowan', 'native_id': 'n-1', **binding}}}
    session = SimpleNamespace(id='2' * 32, host='edge-box', run_as='rowan', native_id='n-1')
    return FakeDb(SimpleNamespace(value=value), node), session


def test_resume_needs_the_reviewed_key_bytes_and_consumes_the_selection_once():
    db, session = resume_case()
    assert activation.resume_permitted(db, session)
    assert activation.resume_permitted(db, session, consume=True)
    assert db.row.value['resume_sessions'] == []
    assert not activation.resume_permitted(db, session)


@pytest.mark.parametrize('case', ['bad_base64', 'other_key', 'no_node', 'other_node', 'local', 'no_row', 'unlisted'])
def test_resume_is_refused_for_any_mismatch_or_unreadable_key(case):
    db, session = resume_case(public_key={'bad_base64': 'not base64!', 'other_key': base64.b64encode(b'other').decode()
                                          }.get(case))
    if case == 'no_node': db.node = None
    if case == 'other_node': db.node.id = 'b' * 32
    if case == 'local': session.host = 'local'
    if case == 'no_row': db.row = None
    if case == 'unlisted': session.id = '3' * 32
    assert not activation.resume_permitted(db, session, consume=True)
    if db.row:
        assert db.row.value['resume_sessions'] == ['2' * 32]


@pytest.mark.parametrize('units, files', [
    (b'accd.service loaded inactive dead\n', b'accd.service enabled enabled\n'),
    (b'rynhult-broker@a.service loaded active running\n', b''),
    (b'x' * ((1 << 20) + 1), b''),
])
def test_services_count_as_stopped_only_when_inactive_and_masked(monkeypatch, units, files):
    monkeypatch.setattr(activation.subprocess, 'run', lambda argv, **kw: SimpleNamespace(
        stdout=units if argv[1] == 'list-units' else files))
    assert not activation.stopped_services()


def test_transitions_need_the_owner_a_private_lock_and_no_concurrent_run(prepared, tmp_path):  # noqa: F811
    layout, plan = prepared
    approve(layout, plan)
    with pytest.raises(Refused, match='root at the machine.s local console'):
        activation.Controller(layout, owner=os.getuid() + 1, uid=os.getuid(), gid=os.getgid()).install()
    layout.lock.touch(mode=0o644)
    layout.lock.chmod(0o644)
    with pytest.raises(Refused, match='lock file must be a single private file'):
        controller(layout).install()
    layout.lock.chmod(0o600)
    tmp_path.chmod(0o777)
    try:
        with pytest.raises(Refused, match='folder of the replacement lock file must be owned by root'):
            controller(layout).install()
    finally:
        tmp_path.chmod(0o700)
    with open(layout.lock, 'r+b') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Refused, match='Another replacement step is running'):
            controller(layout).install()
    assert last_audit(layout)['result'] == 'refused'
    assert not (layout.root / 'data').exists()


@pytest.mark.parametrize('change, refusal', [
    ({'format': 2}, 'prepared plan.json'),
    ({'backup_id': '../x'}, 'prepared plan.json'),
    ({'version': '0.0.0-other'}, 'release .* differs from the one that prepared'),
    ({'schema': '0000_other'}, 'release .* differs from the one that prepared'),
    ({'build': 'deadbeef'}, 'build differs from the one that prepared'),
])
def test_a_plan_from_another_release_or_build_never_installs(prepared, change, refusal):  # noqa: F811
    layout, _ = prepared
    plan = rewrite_plan(layout, **change)
    approve(layout, plan)
    with pytest.raises(Refused, match=refusal):
        controller(layout).install()
    assert last_audit(layout)['result'] == 'refused'
    assert not (layout.root / 'data').exists()


def test_new_main_identical_to_old_main_is_refused(prepared):  # noqa: F811
    layout, plan = prepared
    approve(layout, rewrite_plan(layout, new_main_id=plan['old_main_id']))
    with pytest.raises(Refused, match='prepared plan.json'):
        controller(layout).install()


def test_another_recorded_replacement_or_an_early_transition_is_refused(prepared):  # noqa: F811
    layout, plan = prepared
    approve(layout, plan)
    with pytest.raises(Refused, match='step can.t run now'):
        controller(layout).activate()
    with pytest.raises(Refused, match='step can.t run now'):
        controller(layout).release_control()
    layout.state.write_text(json.dumps({'phase': 'installed', 'plan_sha256': '0' * 64, 'new_main_id': plan['new_main_id']}))
    with pytest.raises(Refused, match='Another replacement is already recorded'):
        controller(layout).install()
    assert not (layout.root / 'data').exists()


def test_prepared_files_changed_after_install_block_activation(prepared):  # noqa: F811
    layout, plan = prepared
    approve(layout, plan)
    ctl = controller(layout)
    ctl.install()
    (layout.prepared / 'data/REPLACEMENT-ID').write_bytes(b'f' * 32)
    with pytest.raises(Refused, match='prepared files changed'):
        ctl.activate()
    assert json.loads(layout.state.read_text())['phase'] == 'installed'


def test_install_needs_a_private_destination_and_a_complete_inventory(prepared):  # noqa: F811
    layout, plan = prepared
    approve(layout, plan)
    layout.root.chmod(0o777)
    try:
        with pytest.raises(Refused, match='Rynhult folder .* must be owned by root'):
            controller(layout).install()
    finally:
        layout.root.chmod(0o755)
    assert not layout.state.exists()
    files = {k: v for k, v in plan['files'].items() if k != 'data/acc.db'}
    approve(layout, rewrite_plan(layout, files=files))
    with pytest.raises(Refused, match='prepared plan.s file list isn.t valid'):
        controller(layout).install()
    assert not layout.state.exists() and not (layout.root / 'data').exists()


def test_startup_fails_closed_on_any_hold_marker_or_unreadable_control(tmp_path):
    layout = activation.Layout(root=tmp_path / 'srv', policy=tmp_path / 'policy.json', state=tmp_path / 'state.json',
                               audit=tmp_path / 'audit.jsonl', lock=tmp_path / 'lock')
    data = layout.root / 'data'
    data.mkdir(parents=True)
    assert not activation.startup_blocked(data, layout=layout, owner=os.getuid())
    (data / 'REPLACEMENT-ID').symlink_to(tmp_path / 'missing')
    assert activation.startup_blocked(data, layout=layout, owner=os.getuid())
    (data / 'REPLACEMENT-ID').unlink()
    layout.state.write_text('{"phase": "active"}')  # controlled, but no policy or marker can be read
    assert activation.startup_blocked(data, layout=layout, owner=os.getuid())
    other = tmp_path / 'elsewhere/data'
    other.mkdir(parents=True)
    assert not activation.startup_blocked(other, layout=layout, owner=os.getuid())


def test_command_line_refuses_the_wrong_account_without_details(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(activation.os, 'geteuid', lambda: 1000)
    for argv in (['install'], ['activate'], ['release-control'], ['status']):
        assert activation.main(argv) == 1
    monkeypatch.setattr(activation.os, 'geteuid', lambda: 0)
    from acc import backup_server
    kit = tmp_path / 'kit.json'
    kit.write_text('{}')
    args = ['restore', '--prepare-replacement', '--kit', str(kit), '--target-id', 'b' * 32,
            '--backup-id', 'a' * 32, '--destination', str(tmp_path / 'out')]
    assert backup_server.main(args) == 1
    err = capsys.readouterr().err
    assert err.count('Replacement operation refused') == 4 and str(tmp_path) not in err
    assert 'replacement preparation must run unprivileged' in err
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('attack', ['shared_file', 'writable_parent'])
def test_an_unsafe_audit_record_refuses_before_any_install(prepared, tmp_path, attack):  # noqa: F811
    layout, plan = prepared
    approve(layout, plan)
    logs = tmp_path / 'log'
    logs.mkdir(mode=0o700)
    layout = dataclasses.replace(layout, audit=logs / 'acc-replacement.jsonl')
    if attack == 'shared_file':
        layout.audit.touch(mode=0o644)
        layout.audit.chmod(0o644)
    else:
        logs.chmod(0o777)
    with pytest.raises(Refused, match='replacement audit log.*owned by root'):
        controller(layout).install()
    assert not layout.state.exists() and not (layout.root / 'data').exists()


@pytest.mark.parametrize('kind', ['BOOLEAN', 'INTEGER', 'NUMERIC', 'REAL'])
@pytest.mark.parametrize('value', ['0', '1', '-1', '600'])
def test_schema_accepts_only_quote_spelling_of_numeric_defaults(kind, value):
    plain = f'CREATE TABLE t (a {kind} DEFAULT {value} NOT NULL)'
    quoted = f"CREATE TABLE t (a {kind} DEFAULT '{value}' NOT NULL)"
    assert activation._schema_sql(plain) == activation._schema_sql(quoted)
    with sqlite3.connect(':memory:') as db:
        db.execute(plain.replace('TABLE t ', 'TABLE plain '))
        db.execute(quoted.replace('TABLE t ', 'TABLE quoted '))
        db.execute('INSERT INTO plain DEFAULT VALUES')
        db.execute('INSERT INTO quoted DEFAULT VALUES')
        assert db.execute('SELECT a, typeof(a) FROM plain').fetchone() == db.execute(
            'SELECT a, typeof(a) FROM quoted').fetchone()


@pytest.mark.parametrize('left,right', [
    ("a TEXT DEFAULT '0'", 'a TEXT DEFAULT 0'),
    ("a BLOB DEFAULT '0'", 'a BLOB DEFAULT 0'),
    ("a BOOLEAN DEFAULT '0'", 'a BOOLEAN DEFAULT 1'),
    ("a BOOLEAN DEFAULT '00'", 'a BOOLEAN DEFAULT 0'),
    ("a BOOLEAN DEFAULT ('0')", 'a BOOLEAN DEFAULT (0)'),
    ("a BOOLEAN DEFAULT '1.0'", 'a BOOLEAN DEFAULT 1.0'),
    ("a INTEGER DEFAULT '9223372036854775808'", 'a INTEGER DEFAULT 9223372036854775808'),
    ("a INTEGER DEFAULT '0' CHECK(a = '0')", 'a INTEGER DEFAULT 0 CHECK(a = 0)'),
    ("a INTEGER CHECK(typeof(a) = 'DEFAULT ''0''')", "a INTEGER CHECK(typeof(a) = 'DEFAULT 0')"),
    ("a BOOLEAN CHECK(a = '0')", 'a BOOLEAN CHECK(a = 0)'),
    ("a TEXT DEFAULT 'DEFAULT ''0'''", "a TEXT DEFAULT 'DEFAULT 0'"),
    ('a INTEGER UNIQUE', 'a INTEGER'),
    ('a INTEGER REFERENCES t(a)', 'a INTEGER'),
])
def test_schema_preserves_other_default_and_constraint_differences(left, right):
    assert activation._schema_sql(f'CREATE TABLE t ({left})') != activation._schema_sql(f'CREATE TABLE t ({right})')


def test_migrated_database_with_historical_quoted_boolean_defaults_is_trusted(tmp_path):
    import shutil

    from alembic import command

    from acc.server.db import MIGRATIONS, alembic_config, migrate
    database = tmp_path / 'history.db'
    url = f'sqlite:///{database}'
    # Reproduce the actual migration spellings shipped before 060816ff (#216):
    # 0040 and 0041 used server_default='0', later changed to sa.false(). Only
    # this temporary migration tree uses those old spellings; never edit source.
    history = tmp_path / 'historical-migrations'
    shutil.copytree(MIGRATIONS, history, ignore=shutil.ignore_patterns('__pycache__'))
    for name, count in [('0040_containers.py', 2), ('0041_passkeys.py', 1)]:
        path = history / 'versions' / name
        text = path.read_text()
        assert text.count('server_default=sa.false()') == count
        path.write_text(text.replace('server_default=sa.false()', "server_default='0'"))
    config = alembic_config(url)
    config.set_main_option('script_location', str(history))
    command.upgrade(config, '0041')
    migrate(url)  # All remaining upgrades use the real current release migrations.
    reference = tmp_path / 'fresh.db'
    migrate(f'sqlite:///{reference}')
    with sqlite3.connect(database) as db, sqlite3.connect(reference) as fresh:
        for table, column in [('auth_session', 'passkey_verified'), ('project', 'containers'), ('session', 'container')]:
            query = 'SELECT sql FROM sqlite_master WHERE name=?'
            actual = db.execute(query, (table,)).fetchone()[0]
            expected = fresh.execute(query, (table,)).fetchone()[0]
            assert f"{column} BOOLEAN DEFAULT '0'" in actual
            assert f'{column} BOOLEAN DEFAULT 0' in expected
            assert actual != expected
    activation._trusted_schema(database, tmp_path)


@pytest.mark.parametrize('drift', ['trigger', 'view', 'table', 'index', 'check', 'foreign_key', 'unique'])
def test_other_schema_drift_is_still_refused(tmp_path, drift):
    from acc.server.db import migrate
    database = tmp_path / 'drift.db'
    migrate(f'sqlite:///{database}')
    with sqlite3.connect(database) as db:
        if drift in ('trigger', 'view', 'table', 'index'):
            sql = {
                'trigger': 'CREATE TRIGGER injected AFTER INSERT ON setting BEGIN DELETE FROM setting; END',
                'view': 'CREATE VIEW injected AS SELECT * FROM setting',
                'table': 'CREATE TABLE injected (a INTEGER)',
                'index': 'CREATE INDEX injected ON setting(value)',
            }[drift]
            db.execute(sql)
        else:
            ddl = db.execute("SELECT sql FROM sqlite_master WHERE name='setting'").fetchone()[0]
            clause = {'check': 'CHECK(length(value)>1)', 'foreign_key': 'FOREIGN KEY(value) REFERENCES user(id)',
                      'unique': 'UNIQUE(value)'}[drift]
            db.execute('PRAGMA writable_schema=ON')
            db.execute("UPDATE sqlite_master SET sql=? WHERE name='setting'", (ddl[:-1] + ', ' + clause + ')',))
    with pytest.raises(Refused, match='schema differs'):
        activation._trusted_schema(database, tmp_path)


def test_unmodified_historical_revision_upgrades_to_trusted_schema(tmp_path):
    from alembic import command

    from acc.server.db import alembic_config, migrate
    database = tmp_path / 'old.db'
    url = f'sqlite:///{database}'
    command.upgrade(alembic_config(url), '0040')
    migrate(url)
    activation._trusted_schema(database, tmp_path)
