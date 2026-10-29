"""Upgrade rehearsal: a synthetic, populated database at the last pre-RC schema (0066), upgraded to this tree's head.

Everything here is invented (people, projects, machines, credentials); nothing is read from a live instance. Used by
tests/test_upgrade_rehearsal.py (SQLite, and PostgreSQL from local binaries) and scripts/upgrade_rehearsal.py (which
also starts the e2e server on the upgraded database).

seed() writes the data at BASE; snapshot() reads every BASE-era column of every seeded table, so an upgrade or a
downgrade can be compared value by value. after_upgrade() adds rows that only the newer schema can hold, so a
downgrade shows what it drops.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import pwd
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, inspect, select, text
from sqlalchemy.engine import Engine

from acc.server import auth
from acc.server.db import alembic_config, make_engine
from acc.server.infrastructure import aad_for
from acc.server.vault import Cipher

BASE = '0066'  # main before release-candidate preparation (#343 added 0067)
# Synthetic sign-in for the e2e probe; never a real person's.
OWNER, OWNER_PASSWORD, OWNER_TOTP = 'alex', 'rehearsal-password-alex', 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'
SKIP = {'alembic_version'}
NOW = dt.datetime(2026, 10, 20, 12, 0, 0)


def at(days=0, hours=0):
    return NOW + dt.timedelta(days=days, hours=hours)


def head(url: str) -> str:
    return ScriptDirectory.from_config(alembic_config(url)).get_current_head()


def upgrade(url: str, revision: str = 'head') -> None:
    """The app's own path (db.migrate) runs these same scripts; this one can stop at any revision."""
    _run(url, command.upgrade, revision)


def downgrade(url: str, revision: str) -> None:
    _run(url, command.downgrade, revision)


def _run(url, action, revision):
    from sqlalchemy.engine import make_url

    from acc.server.db import _postgres_engine, _sqlite_engine
    config = alembic_config(url)
    postgres = make_url(url).get_backend_name() == 'postgresql'
    engine = _postgres_engine(url) if postgres else _sqlite_engine(url, foreign_keys=False)
    try:
        with engine.connect() as connection:
            config.attributes['connection'] = connection
            action(config, revision)
            if postgres:
                connection.commit()
    finally:
        engine.dispose()


def version(url: str) -> str:
    engine = make_engine(url)
    try:
        with engine.connect() as db:
            return db.execute(text('select version_num from alembic_version')).scalar_one()
    finally:
        engine.dispose()


# ------------------------------------------------------------------------------------------------ PostgreSQL --

def short_folder(work: Path, prefix: str) -> Path:
    """Unix sockets (PostgreSQL's, the e2e broker's) must fit 107 bytes: a long work path gets a short sibling in /tmp."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir='/tmp' if len(str(work)) > 40 else work))


class ClusterUnavailable(RuntimeError):
    """This machine can't run a throwaway cluster; the message says why. Tests skip on it, never fail."""


class Cluster:
    """A throwaway PostgreSQL cluster: trust auth, no TCP listener, a short socket folder.

    PostgreSQL refuses to run as root (the gates run as root). Then initdb, pg_ctl and createdb run as an
    unprivileged account through runuser (ACC_TEST_POSTGRES_USER, default nobody), in a fresh /tmp folder that account
    owns: root's private test folders aren't reachable for it."""

    def __init__(self, bin_dir: Path, work: Path):
        self.bin, self.user, self.home = bin_dir, None, None
        if os.geteuid() == 0:
            self.user = os.environ.get('ACC_TEST_POSTGRES_USER', 'nobody')
            try:
                account = pwd.getpwnam(self.user)
            except KeyError:
                raise ClusterUnavailable(f'running as root, and there is no {self.user!r} account to run PostgreSQL as '
                                         '(set ACC_TEST_POSTGRES_USER)') from None
            if account.pw_uid == 0:
                raise ClusterUnavailable(f'{self.user!r} is root; PostgreSQL needs an unprivileged account')
            if not shutil.which('runuser'):
                raise ClusterUnavailable('running as root without runuser to start PostgreSQL as an unprivileged account')
            self.home = Path(tempfile.mkdtemp(prefix='rhpg.', dir='/tmp'))
            self.data, self.socket, self.log = self.home / 'data', self.home / 's', self.home / 'pg.log'
            self.socket.mkdir()
            for path in (self.home, self.socket):
                os.chown(path, account.pw_uid, account.pw_gid)
        else:
            self.data, self.log = work / 'pg', work / 'pg.log'
            self.socket = short_folder(work, 'rhpg.')

    def __enter__(self):
        steps = [('initdb', '-D', self.data, '-U', 'rehearsal', '--auth=trust', '-E', 'UTF8', '--locale=C.UTF-8'),
                 ('pg_ctl', '-D', self.data, '-l', self.log, '-w', 'start',
                  '-o', f"-c listen_addresses='' -k {self.socket} -c fsync=off"),
                 ('createdb', '-h', self.socket, '-U', 'rehearsal', 'acc')]
        for tool, *args in steps:
            try:
                self.run(tool, *args)
            except subprocess.CalledProcessError as error:
                self.__exit__(None, None, None)
                last = (error.stderr or error.stdout or '').strip().splitlines()[-1:] or ['no output']
                raise ClusterUnavailable(f"{tool}{f' as {self.user}' if self.user else ''} failed: {last[0]}") from None
        return f'postgresql+psycopg://rehearsal@/acc?host={self.socket}'

    def __exit__(self, *_):
        if self.data.exists():
            self.run('pg_ctl', '-D', self.data, '-m', 'fast', '-w', 'stop', check=False)
        for folder in ((self.home,) if self.home else (self.data, self.socket)):
            shutil.rmtree(folder, ignore_errors=True)

    def run(self, tool, *args, check=True):
        command = [str(self.bin / tool), *map(str, args)]
        if self.user:  # root: the same command as the unprivileged account, from a folder it can enter
            return subprocess.run(['runuser', '-u', self.user, '--', *command], check=check, capture_output=True,
                                  text=True, cwd=self.home)
        return subprocess.run(command, check=check, capture_output=True, text=True)


# ------------------------------------------------------------------------------------------------------- seed --

def certificate(name: str) -> tuple[str, str, str]:
    """A self-signed test certificate: PEM, SHA-256 fingerprint, serial."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(NOW - dt.timedelta(days=1))
            .not_valid_after(NOW + dt.timedelta(days=365)).sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(), cert.fingerprint(hashes.SHA256()).hex(),
            format(cert.serial_number, 'x'))


def rows(data_dir: Path) -> dict[str, list[dict]]:
    """Synthetic rows at BASE. Encrypted values are sealed with data_dir/secrets/vault.key (created here)."""
    cipher = Cipher(data_dir / 'secrets' / 'vault.key')
    people = [('alex', 'owner', 'alex-agent'), ('sam', 'member', 'sam-agent'), ('robin', 'member', 'robin-agent'),
              ('kim', 'member', 'kim-agent')]
    users = [dict(id=i, username=name, password_hash=auth.hash_password(f'rehearsal-password-{name}'),
                  totp_secret=OWNER_TOTP if name == OWNER else base64.b32encode(secrets.token_bytes(20)).decode(),
                  created_at=at(-30 + i), role=role, account=account, person_id=f'00000000-0000-4000-8000-00000000000{i}',
                  company_only=name == 'kim', recovery_hash=None)
             for i, (name, role, account) in enumerate(people, start=1)]
    groups = [dict(id='g' + '1' * 31, name='Platform team', created_by='alex', home_site='main', archived=False,
                   revision=3, paused=False, created_at=at(-20), board_revision=2, concurrent=2, daily_launches=10)]
    memberships = [dict(group_id=groups[0]['id'], user_id=uid, role=role, provenance='local')
                   for uid, role in ((1, 'maintainer'), (2, 'member'), (3, 'viewer'))]
    projects = [dict(id=i, slug=slug, name=name, host='main', workspace_path=f'/srv/acc/workspaces/{slug}',
                     repo=f'https://github.com/example-org/{slug}' if i < 4 else None, import_mode='clone', trusted=True,
                     archived=slug == 'old-site', notes='Notes: ünïcødé ✓' if i == 1 else None, created_at=at(-25 + i),
                     account=account, owner=None if slug == 'shared-infra' else owner, git_backup=i != 2, backup_identity=f'backup-{slug}',
                     private_workspace=False, group_id=groups[0]['id'] if slug == 'shared-infra' else None,
                     containers=False, issue_sources=[{'kind': 'github'}] if i == 1 else None)
                for i, (slug, name, owner, account) in enumerate([
                    ('web-shop', 'Web shop', 'alex', 'alex-agent'), ('api', 'API', 'alex', 'alex-agent'),
                    ('mobile-app', 'Mobile app', 'sam', 'sam-agent'), ('shared-infra', 'Shared infra', 'alex', 'alex-agent'),
                    ('old-site', 'Old site', 'robin', 'robin-agent')], start=1)]
    nodes = [dict(id='a1b2c3d4e5f60718', name='build-box', status='active', public_key='k' * 43, info={'os': 'linux', 'arch': 'x86_64'},
                  last_seen=at(0), created_by='alex', created_at=at(-15), role='always_on', offline_threshold_seconds=900,
                  downtime_policy={'windows': [{'start': '01:30', 'until_reconnect': True}]}, downtime_until_reconnect=True),
             dict(id='b1b2c3d4e5f60718', name='studio-mac', status='active', public_key='m' * 43,
                  info={'os': 'darwin', 'arch': 'arm64'}, last_seen=at(-1), created_by='alex', created_at=at(-10),
                  role='workstation', idle_sleep_minutes=30),
             dict(id='c1b2c3d4e5f60718', name='lab-pc', status='revoked', info={'os': 'linux', 'wsl': '1'},
                  created_by='alex', created_at=at(-12), role='workstation')]
    machine_accounts = [dict(id='m' + '1' * 31, node_id=nodes[0]['id'], person='robin', account='robin-build', status='pending')]
    placements = [dict(id=1, project_id=4, person='sam', host='main', account='sam-agent', path='/srv/acc/workspaces/shared-infra-sam',
                       trust='trusted', sync_status='synced', approved_by='alex')]
    specs = [dict(id=i, provider=p, inputs={'model': m, 'prompt': 'Fix the tests'}, files={}, digest=f'{i:064x}', created_at=at(-9))
             for i, (p, m) in enumerate([('claude', 'opus'), ('codex', 'gpt'), ('copilot', None)], start=1)]
    sessions, runs, events, attention = [], [], [], []
    for i in range(12):
        sid = f's{i:031d}'
        project = projects[i % 5]
        state = ('running', 'stopped', 'archived')[i % 3]
        sessions.append(dict(
            id=sid, project_id=project['id'], provider=specs[i % 3]['provider'], name=f'task-{i}', tmux_name=f'acc-task-{i}',
            native_id=f'native-{i}', launch_spec_id=specs[i % 3]['id'], desired_state=state, observed_state=state,
            recovery_attempts=0, spool_offset=1000 * i, created_at=at(-8, i), updated_at=at(-1, i), run_as=project['account'],
            owner=project['owner'], workdir=project['workspace_path'], vault_keys=['v' + '1' * 31] if i == 0 else None,
            changes={'files': i, 'added': 10 * i, 'removed': i}, worktree=f'/srv/acc/worktrees/{sid}' if i % 4 == 0 else None,
            worktree_branch=f'acc/task-{i}' if i % 4 == 0 else None, team_id='t' + '1' * 31 if i in (6, 7) else None,
            parent_session_id='s' + '6'.rjust(31, '0') if i == 7 else None, team_depth=1 if i == 7 else None,
            placement_id=1 if project['id'] == 4 and i % 2 else None, activity='Running npm test' if state == 'running' else None))
        runs += [dict(session_id=sid, reason=reason, started_at=at(-8, i + n), ended_at=at(-8, i + n + 1) if n == 0 else None,
                      exit_status=0 if n == 0 else None, end_reason='stopped' if n == 0 else None)
                 for n, reason in enumerate(('launch', 'resume'))]
        events += [dict(session_id=sid, ts=at(-8, i) + dt.timedelta(minutes=m), source='hook', kind=kind, verified=True,
                        payload={'tool': 'Bash', 'text': f'step {m} — “quoted” ✓', 'n': m})
                   for m, kind in enumerate(('user_prompt', 'tool_use', 'tool_result', 'stop', 'notification') * 2)]
        if i < 4:
            attention.append(dict(dedupe_key=f'question:{sid}', session_id=sid, kind='question', severity='warn', source='hook',
                                  verified=True, title=f'task-{i} asks a question', created_at=at(-1, i), updated_at=at(-1, i),
                                  owner=project['owner']))
    devices, pairings = [], []
    for n, (person, label) in enumerate([('alex', 'Alex laptop'), ('sam', 'Sam desktop')]):
        pem, fingerprint, serial = certificate(label)
        devices.append(dict(id=f'd{n:031d}', person=person, label=label, kind='browser', status='active', certificate_pem=pem,
                            fingerprint=fingerprint, serial=serial, expires_at=at(300), last_seen_at=at(-1), created_at=at(-5)))
    pairings.append(dict(id='p' + '1' * 31, person='alex', label="Alex's phone", token_hash=auth.digest('pair-token-1'),
                         status='pending', expires_at=at(1), created_at=at(0)))
    vault = []
    for n, (name, variable, value, owner, shared) in enumerate([
            ('npm token', 'NPM_TOKEN', 'npm_SYNTHETIC0000000000000000000000000', 'alex', False),
            ('Staging DB', 'DATABASE_URL', 'postgres://app:synthetic@db.example.invalid/app', 'alex', True),
            ('Sentry', 'SENTRY_DSN', 'https://synthetic@sentry.example.invalid/1', 'sam', False)]):
        key_id = 'v' + str(n + 1) * 31
        vault.append(dict(id=key_id, name=name, variable=variable, owner=owner, shared=shared, remote=False,
                          ciphertext=cipher.seal(key_id, variable, value, create=True), created_at=at(-7), updated_at=at(-7)))
    credentials = []
    for cid, name, provider, purpose, content in [
            ('c' + '1' * 31, 'Cloudflare certificates', 'cloudflare', 'certificate', 'cf-synthetic-token-000000000000000000000'),
            ('c' + '2' * 31, 'Cloudflare publishing', 'cloudflare', 'publishing', 'cf-synthetic-token-111111111111111111111'),
            ('c' + '3' * 31, 'Route 53 (preview)', 'route53', 'publishing',
             json.dumps({'access_key_id': 'AKIASYNTHETIC0000000', 'secret_access_key': 'synthetic/secret/0000000000000000000000000',
                         'hosted_zone_id': 'Z0000000SYNTHETIC'}, sort_keys=True))]:
        credentials.append(dict(id=cid, name=name, provider=provider, purpose=purpose, owner='alex', revision=1, retired=[],
                                disabled=False, updated_at=at(-6),
                                ciphertext=cipher.seal(cid, aad_for(provider, purpose, 1), content, create=True)))
    settings = [dict(key=k, value=v, updated_by='alex', updated_at=at(-2)) for k, v in [
        ('telemetry.consent', {'usage': True, 'usage_schema': 'usage/1', 'reports': False}),
        ('backup.capture_checkpoint', {'id': 'stage-synthetic', 'at': 1792000000.0}),
        ('publish.zone', 'example.com'), ('ui.theme.alex', 'dark')]]
    return {
        'user': users, 'acc_group': groups, 'group_membership': memberships, 'project': projects, 'node': nodes,
        'group_machine_account': machine_accounts, 'placement': placements, 'launch_spec': specs, 'session': sessions,
        'session_run': runs, 'event': events, 'attention_item': attention, 'device': devices, 'device_pairing': pairings,
        'auth_session': [dict(token_hash=auth.digest(f'login-{n}'), csrf=f'{n:064x}', user_id=uid, user_agent='Firefox',
                              created_at=at(-1), last_seen_at=at(0), expires_at=at(6), method='password', passkey_verified=False)
                         for n, uid in enumerate((1, 2, 3))],
        'passkey': [dict(user_id=2, credential_id='Y3JlZGVudGlhbC1zYW0', public_key='pQECAyYgASFYIA', rp_id='acc.example.lan',
                         name='Sam security key', sign_count=4, created_at=at(-3))],
        'push_subscription': [dict(endpoint='https://push.example.invalid/e/1', p256dh='B' * 87, auth='a' * 22, username='alex',
                                   label='Alex laptop', created_at=at(-4), failures=0)],
        'vault_key': vault, 'infrastructure_credential': credentials,
        'dns_binding': [dict(id=1, provider='cloudflare', zone='example.com', certificate_id=credentials[0]['id'],
                             publishing_id=credentials[1]['id'], revision=2, applied_revision=2, status='applied')],
        'setting': settings,
        'telemetry_item': [dict(id='usage:2026-10-19', kind='usage', key_id='K' * 22, body='{"schema":"usage/1"}', state='sent',
                                created=1792400000.0, due=1792400000.0, attempts=1, result='accepted', receipt='r' * 36,
                                occurrences=1, sent_at=1792400100.0)],
        'telemetry_counter': [dict(id=f'2026-10-19:{e}:{p}', day='2026-10-19', event=e, provider=p, count=c)
                              for e, p, c in [('session.launch', 'claude', 7), ('session.launch', 'codex', 3), ('guardrail.block', '', 2)]],
        'audit_log': [dict(ts=at(-10, n), actor=('alex', 'sam', '@local-admin')[n % 3], action=action, target=f't{n}',
                           detail={'n': n})
                      for n, action in enumerate(['auth.login', 'project.create', 'session.launch', 'backup.target_add',
                                                  'backup.capture', 'dns.apply', 'vault.create', 'group.create'] * 5)],
        'schedule': [dict(id='sch' + str(n) * 29, owner='alex', account='alex-agent', launch={'project': 1, 'provider': 'claude'},
                          timing={'kind': 'daily', 'at': '07:00'}, timezone='Europe/Stockholm', missed='skip',
                          enabled=n == 1, deleted=False, next_run_at=at(1), created_at=at(-5)) for n in (1, 2)],
        'invite': [dict(token_hash=auth.digest('invite-1'), role='member', account='new-agent', created_by='alex',
                        created_at=at(-1), expires_at=at(6))],
        'sso_invite': [dict(token_hash=auth.digest('sso-1'), email='kim@example.com', account='kim-agent', created_by='alex',
                            created_at=at(-2), expires_at=at(5), withdrawn_at=at(-1))],
        'oidc_provider': [dict(issuer='https://login.example.com', client_id='rynhult-synthetic', enabled=False, revision=1,
                               session_hours=8, updated_by='alex', updated_at=at(-3), mfa_acr=[], passkey_enrollment=True)],
        'external_identity': [dict(organization='example', issuer='https://login.example.com', subject='kim-0001',
                                   person_id=users[3]['person_id'], created_by='alex', created_at=at(-2))],
        'team': [dict(id='t' + '1' * 31, limits={'max_helpers': 3, 'max_depth': 1}, expires_at=at(2), stopped=False, started=1)],
        'port_reservation': [dict(port=8000 + n, project_id=pid, label=f'dev {n}', owner='alex', created_at=at(-4))
                             for n, pid in enumerate((1, 1, 3))],
    }


def backup_targets(data_dir: Path) -> None:
    """Backups are configured outside the database (backup-service/targets.json); the upgrade must not need them."""
    root = data_dir / 'backup-service'
    root.mkdir(parents=True, exist_ok=True)
    (root / 'targets.json').write_text(json.dumps([
        {'id': 'f' * 32, 'kind': 'node', 'node_id': 'a1b2c3d4e5f60718', 'node_name': 'build-box', 'repository': 'enrolled:build-box',
         'repository_id': None, 'key_epoch': 1, 'enabled': False, 'max_size_bytes': 20 * 2 ** 30, 'setup_verified_at': None,
         'created_at': 1792000000.0}]))


def seed(url: str, data_dir: Path) -> dict[str, int]:
    """Writes the BASE data in foreign-key order; returns the row count per table."""
    data = rows(data_dir)
    backup_targets(data_dir)
    engine = make_engine(url)
    try:
        meta = MetaData()
        meta.reflect(engine, only=list(data))
        with engine.begin() as db:
            for table, values in data.items():
                for value in values:  # rows differ in which optional columns they set; unset ones take defaults
                    db.execute(meta.tables[table].insert(), {k: v for k, v in value.items() if v is not None})
        postgres = engine.dialect.name == 'postgresql'
        if postgres:  # explicit ids above: move the sequences past them, as a real history would have
            with engine.begin() as db:
                for table in data:
                    for column in meta.tables[table].primary_key.columns:
                        if column.autoincrement is True or (column.type.python_type is int and len(meta.tables[table].primary_key.columns) == 1):
                            seq = db.execute(text('select pg_get_serial_sequence(:t, :c)'), {'t': f'"{table}"', 'c': column.name}).scalar()
                            if seq:
                                db.execute(text(f'select setval(:s, (select coalesce(max("{column.name}"), 1) from "{table}"))'), {'s': seq})
    finally:
        engine.dispose()
    return {table: len(values) for table, values in data.items()}


# --------------------------------------------------------------------------------------------------- snapshot --

def _plain(value):
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None).isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, str) and value[:1] in '{[':
        try:
            return json.dumps(json.loads(value), sort_keys=True)
        except ValueError:
            return value
    return value


def columns(engine: Engine) -> dict[str, list[str]]:
    schema = inspect(engine)
    return {t: [c['name'] for c in schema.get_columns(t)] for t in schema.get_table_names()
            if t not in SKIP and not t.startswith('transcript_fts')}


def snapshot(url: str, only: dict[str, list[str]] | None = None) -> dict[str, list[tuple]]:
    """Every row of every table, restricted to `only`'s columns (BASE's), in a backend-neutral form."""
    engine = make_engine(url)
    try:
        present = columns(engine)
        meta = MetaData()
        meta.reflect(engine, only=list(only or present))
        out = {}
        with engine.connect() as db:
            for table, names in (only or present).items():
                cols = [meta.tables[table].c[n] for n in names]
                key = [c for c in meta.tables[table].primary_key.columns] or cols
                out[table] = sorted((tuple(_plain(v) for v in row) for row in db.execute(select(*cols).order_by(*key))),
                                    key=repr)
        return out
    finally:
        engine.dispose()


def schema(url: str) -> dict[str, list[str]]:
    engine = make_engine(url)
    try:
        return columns(engine)
    finally:
        engine.dispose()


def integrity(url: str) -> list:
    """SQLite's own checks; PostgreSQL enforces foreign keys as rows are written."""
    engine = make_engine(url)
    try:
        if engine.dialect.name != 'sqlite':
            return []
        with engine.connect() as db:
            problems = [tuple(r) for r in db.exec_driver_sql('PRAGMA foreign_key_check')]
            check = db.exec_driver_sql('PRAGMA integrity_check').scalar()
            return problems + ([] if check == 'ok' else [check])
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------- newer-schema rows --

def after_upgrade(url: str) -> dict[str, int]:
    """Rows only the upgraded schema can hold (when its revisions are present), so a downgrade shows what it drops."""
    engine = make_engine(url)
    added = {}
    try:
        tables = columns(engine)
        with engine.begin() as db:
            if 'kind' in tables['device_pairing']:  # 0067: a native phone enrollment waiting to be claimed
                db.execute(text("insert into device_pairing (id, person, label, token_hash, status, expires_at, created_at, kind, "
                                "installation_id, platform) values ('p2222222222222222222222222222222', 'sam', 'Sam phone', "
                                ":h, 'pending', :e, :c, 'native', 'inst-sam-0001', 'android')"),
                           {'h': auth.digest('native-pair-token'), 'e': at(1), 'c': at(0)})
                added['device_pairing'] = 1
            if 'device_fingerprint' in tables['auth_session']:  # 0067: Sam's sign-in bound to that phone's key
                db.execute(text("insert into auth_session (token_hash, csrf, user_id, created_at, last_seen_at, expires_at, "
                                "method, device_fingerprint, device_bound_at) values (:h, :c, 2, :n, :n, :e, 'password', :f, :n)"),
                           {'h': auth.digest('phone-login'), 'c': 'c' * 64, 'n': at(0), 'e': at(6), 'f': 'f' * 64})
                added['auth_session'] = 1
            if 'work_item' in tables:  # Orchestrator v2 (0069+): the team's NOW and one working-list question
                db.execute(text("insert into orchestration_run (team_id, now, now_revision, lead_seen_now, revision) "
                                "values (:t, 'Ship the release notes', 1, 1, 1)"), {'t': 't' + '1' * 31})
                db.execute(text("insert into work_item (team_id, position, kind, title, steps, state, source, owner_edited, links, "
                                "revision, created_at, updated_at) values (:t, 1.0, 'question', 'Which DNS provider?', '[]', "
                                "'open', 'lead', :f, '[]', 1, :c, :c)"), {'t': 't' + '1' * 31, 'f': False, 'c': at(0)})
                added['orchestration_run'] = added['work_item'] = 1
            if 'public_git_backup' in tables['node']:  # 0068: the owner's opt-in on one machine
                db.execute(text("update node set public_git_backup = :t where name = 'studio-mac'"), {'t': True})
                added['node.public_git_backup'] = 1
    finally:
        engine.dispose()
    return added
