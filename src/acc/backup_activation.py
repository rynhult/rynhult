"""Offline replacement preparation and root-console activation; never starts a service.

No network or provider operation. Physical fencing is independent operator evidence,
not a reachability test. Root copies only fixed data destinations, never parses DB/git.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .backup_server import ID, MAX_BYTES, MAX_FILES, Refused, canonical, private_write, validate_and_install

CHECKS = ('authority_reviewed', 'credentials_rotated', 'endpoint_tls_reviewed', 'accounts_paths_reviewed',
          'data_age_exclusions_accepted', 'remote_work_inspected')
HEX = re.compile(r'[0-9a-f]{64}')
REASON = 'Offline replacement: quarantined; requires fresh owner review'
# Authority kept outside the DB: the installation API token is owner access without sign-in, and provider tokens
# are the old main's credentials. A fresh API token is made on first start; the others need deliberate re-entry.
DROPPED_SECRETS = ('api_token', 'cloudflare_token', 'github_token', 'setup_token')


@dataclass(frozen=True)
class Layout:
    root: Path = Path('/srv/acc')
    policy: Path = Path('/etc/acc-replacement.json')
    state: Path = Path('/etc/acc-replacement-state.json')
    audit: Path = Path('/var/log/acc-replacement.jsonl')
    lock: Path = Path('/etc/acc-replacement.lock')

    @property
    def prepared(self):
        return self.root / 'recovery/prepared'


DEFAULT_LAYOUT = Layout()


def plan_digest(plan):
    return hashlib.sha256(canonical(plan)).hexdigest()


def _directory(path):
    """Pin every directory component; no symlink ancestor or traversal."""
    path = Path(path)
    if not path.is_absolute() or any(p in ('.', '..') for p in path.parts):
        raise Refused('A replacement path must be an absolute folder path without . or .. in it.')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(path, limit, owner=None):
    parent = _directory(path.parent)
    try:
        if owner is not None:
            info = os.fstat(parent)
            if info.st_uid != owner or info.st_mode & 0o022:
                raise Refused('The folder holding a replacement control file must be owned by root and not writable by others. Fix its owner and permissions, then run the step again.')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit
                    or (owner is not None and (info.st_uid != owner or info.st_mode & 0o022))):
                raise Refused('A replacement control file must be a single plain file owned by root and not writable by others. Fix its owner and permissions, then run the step again.')
            data = file.read(limit + 1)
            if len(data) > limit:
                raise Refused('A replacement control file is too large. Check that the step uses the right file.')
            return data
    finally:
        os.close(parent)


def _json(path, limit=1 << 20, owner=None):
    try:
        value = json.loads(_read(path, limit, owner))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, RecursionError):
        raise Refused('A replacement control file (policy, state, plan or kit) is missing, unreadable or not valid JSON. Check the file the step reads.') from None


def _allowed(name):
    parts = name.split('/')
    if (any(not p or p in ('.', '..') for p in parts) or '\\' in name or '\0' in name
            or parts[0] not in ('data', 'catalog-local', 'git') or len(parts) < 2):
        raise Refused('The prepared files include a path Rynhult never installs. Prepare the replacement again.')
    if parts[0] == 'data' and not (name in ('data/acc.db', 'data/RESTORE-HOLD', 'data/REPLACEMENT-ID',
            'data/backup-service/identity.json', 'data/backup-service/signing.key') or parts[1] == 'secrets'):
        raise Refused('The prepared data includes a file Rynhult never installs. Prepare the replacement again.')


def _stream(path, limit, target=None, uid=None, gid=None):
    """Hash/copy bounded chunks through pinned descriptors, never a whole DB in memory."""
    parent = _directory(path.parent)
    output = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise Refused("A prepared file isn't a single plain file, or is larger than the backup limit. Prepare the replacement again.")
            if target is not None:
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                output = target.open('xb')
                os.fchmod(output.fileno(), 0o600)
            digest, size = hashlib.sha256(), 0
            while chunk := file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise Refused('The prepared files are larger than the backup size limit. Prepare the replacement again.')
                digest.update(chunk)
                if output:
                    output.write(chunk)
            if output:
                output.flush()
                os.fsync(output.fileno())
                os.fchown(output.fileno(), uid, gid)
                os.fsync(output.fileno())
            return {'size': size, 'sha256': digest.hexdigest()}
    finally:
        if output:
            output.close()
        os.close(parent)


def _inventory(root):
    result = {}
    budget = MAX_BYTES
    for component in ('data', 'catalog-local', 'git'):
        directory = root / component
        if directory.is_symlink():
            raise Refused('The prepared files contain a link, which Rynhult never installs. Prepare the replacement again.')
        if not directory.exists():
            continue
        for parent, directories, files in os.walk(directory, followlinks=False):
            for child in directories:
                if (Path(parent) / child).is_symlink():
                    raise Refused('The prepared files contain a link, which Rynhult never installs. Prepare the replacement again.')
            for child in sorted(files):
                path = Path(parent) / child
                name = path.relative_to(root).as_posix()
                _allowed(name)
                result[name] = _stream(path, budget)
                budget -= result[name]['size']
                if len(result) > MAX_FILES:
                    raise Refused('The prepared files are over the backup file-count limit. Prepare the replacement again.')
    return result


_NUMERIC_TYPES = frozenset({'BOOLEAN', 'INTEGER', 'INT', 'BIGINT', 'SMALLINT', 'NUMERIC',
                            'DECIMAL', 'REAL', 'FLOAT', 'DOUBLE'})


def _numeric_default(value, declared_type):
    """Only remove single quotes around canonical, signed 64-bit integers.

    Numeric affinity makes these defaults equivalent on insertion. TEXT/BLOB/ANY
    defaults can have different storage classes, so must remain byte-for-byte.
    Expressions, padding, decimals, comments and other quote styles stay intact.
    """
    if declared_type.upper() in _NUMERIC_TYPES and isinstance(value, str):
        match = re.fullmatch(r"'(-?(?:0|[1-9][0-9]*))'", value)
        if match and len(match[1]) <= 20 and -(1 << 63) <= int(match[1]) < (1 << 63):
            return match[1]
    return value


def _schema_clause(clause):
    # Tokenize before looking for DEFAULT: words inside quoted identifiers,
    # string literals and nested CHECK/FK expressions must never be rewritten.
    # Comments are deliberately outside this narrow compatibility exception.
    if '--' in clause or '/*' in clause:
        return clause
    tokens = list(re.finditer(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
                             r"`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|[^\s]", clause))
    if len(tokens) < 4 or tokens[1][0].upper() not in _NUMERIC_TYPES:
        return clause
    depth = 0
    for index in range(2, len(tokens) - 1):
        token = tokens[index][0]
        if token == '(':
            depth += 1
        elif token == ')':
            depth -= 1
        elif depth == 0 and token == 'DEFAULT':
            literal = tokens[index + 1]
            # A literal default only, never a concatenation or other expression.
            following = tokens[index + 2][0] if index + 2 < len(tokens) else None
            if following is not None and following not in {
                'NOT', 'NULL', 'PRIMARY', 'UNIQUE', 'CHECK', 'REFERENCES', 'COLLATE', 'CONSTRAINT', 'GENERATED',
            }:
                return clause
            value = _numeric_default(literal[0], tokens[1][0])
            return clause[:literal.start()] + value + clause[literal.end():]
    return clause


def _schema_sql(sql):
    """Canonicalize table-clause order and equivalent numeric DEFAULT quotes only.

    Retain every non-table statement and clause byte except equivalent numeric
    DEFAULT quote spelling. Column order is checked independently with table_xinfo;
    other quoted literals and nested expressions stay intact.
    """
    if not sql or not sql.startswith('CREATE TABLE '):
        return sql
    start = sql.index('(')
    clauses, position, depth, quote = [], start + 1, 1, None
    index = position
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                clauses.append(sql[position:index].strip())
                return (sql[:start], tuple(sorted(_schema_clause(clause) for clause in clauses)), sql[index:])
        elif char == ',' and depth == 1:
            clauses.append(sql[position:index].strip())
            position = index + 1
        index += 1
    raise Refused("Rynhult can't read the restored database schema. Restore a backup made by this Rynhult release.")


def _trusted_schema(database, scratch_parent):
    """A signed DB is still data: triggers/views must not undo recovery sanitization."""
    from .server.db import migrate
    sql = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
    with tempfile.TemporaryDirectory(prefix='.replacement-schema-', dir=scratch_parent) as directory:
        reference = Path(directory) / 'reference.db'
        migrate(f'sqlite:///{reference}')
        with sqlite3.connect(reference) as trusted, sqlite3.connect(database) as restored:
            restored.execute('PRAGMA trusted_schema=OFF')
            expected, actual = trusted.execute(sql).fetchall(), restored.execute(sql).fetchall()
            normalize = lambda rows: [(kind, name, table, _schema_sql(ddl)) for kind, name, table, ddl in rows]
            if normalize(expected) != normalize(actual):
                raise Refused('The restored database schema differs from this Rynhult release (it may hold extra triggers or views). Restore a backup made by this release.')
            for kind, name, _, _ in expected:
                if kind == 'table':
                    query = 'PRAGMA table_xinfo("' + name.replace('"', '""') + '")'
                    def columns(connection):
                        return [(*row[:4], _numeric_default(row[4], row[2]), *row[5:])
                                for row in connection.execute(query)]
                    if columns(trusted) != columns(restored):
                        raise Refused('The restored database schema differs from this Rynhult release in its column order. Restore a backup made by this release.')


def quarantine(db, backup_id, new_id, reviewed_nodes, resume_sessions, dropped=()):
    """Hold every restored effect in an open sqlite3 connection; the caller commits. Fails before any change."""
    dropped = list(dropped)
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA foreign_keys=ON')
    db.row_factory = sqlite3.Row
    nodes = {row['id']: dict(row) for row in db.execute('select * from node')}
    for node_id, fingerprint in reviewed_nodes.items():
        node = nodes.get(node_id)
        if (not node or node['status'] != 'active' or not node['public_key'] or
                hashlib.sha256(base64.b64decode(node['public_key'], validate=True)).hexdigest() != fingerprint):
            raise Refused('Each machine chosen for resumed sessions must be active and match its reviewed fingerprint (node identity). Check the reviewed machines, then prepare again.')
    sessions = {row['id']: dict(row) for row in db.execute(
        'select s.id,s.run_as,s.native_id,s.management, coalesce(pl.host,s.team_host,p.host,\'local\') as host from session s '
        'join project p on p.id=s.project_id left join placement pl on pl.id=s.placement_id')}
    allowed_hosts = {nodes[node_id]['name'] for node_id in reviewed_nodes}
    if any(sid not in sessions or sessions[sid]['host'] == 'local' or sessions[sid]['host'] not in allowed_hosts
           or sessions[sid]['management'] != 'acc' or not sessions[sid]['native_id']
           for sid in resume_sessions):
        raise Refused('Only sessions on reviewed remote machines can be made resumable, never local ones (session release refused). Change the selection, then prepare again.')
    for table in ('auth_session', 'invite', 'passkey_challenge', 'device_pairing', 'oidc_flow', 'sso_invite',
                  'vault_grant', 'group_key_grant', 'group_merge_approval', 'push_subscription'):
        db.execute(f'DELETE FROM {table}')
    db.execute("UPDATE device SET status='revoked'")
    db.execute("UPDATE node SET status='revoked',code_hash=NULL,code_expires=NULL,idle_sleep_minutes=NULL")
    for node_id in reviewed_nodes:
        db.execute("UPDATE node SET status='active' WHERE id=?", (node_id,))
    db.execute("UPDATE session SET desired_state='archived',observed_state='archived',automation=NULL,"
               "team_stop_pending=0,multirun_stop_pending=0,spool_offset=0,run_dir=NULL,last_error=?", (REASON,))
    db.execute('UPDATE project SET compose_autostart=0,git_backup=0')
    db.execute('UPDATE schedule SET enabled=0,next_run_at=NULL')
    for table in ('publish_request', 'tool_request'):
        db.execute(f"UPDATE {table} SET status='failed',detail=? WHERE status NOT IN ('removed','rejected','failed')", (REASON,))
    db.execute("UPDATE schedule_run SET status='cancelled',reason=? WHERE status IN ('pending','claimed','running')", (REASON,))
    db.execute("UPDATE team_request SET delivered=1,result=? WHERE delivered=0", (json.dumps({'error': REASON}),))
    db.execute('UPDATE team SET stopped=1')
    db.execute("UPDATE session_run SET ended_at=CURRENT_TIMESTAMP,end_reason='replacement-held' WHERE ended_at IS NULL")
    db.execute('UPDATE oidc_provider SET enabled=0,revision=revision+1')
    db.execute('UPDATE infrastructure_credential SET disabled=1')
    db.execute("UPDATE dns_binding SET status='saved',applied_revision=NULL,generation=NULL")
    # Approved telemetry would go out under the old installation's key: reports return to owner review, and daily
    # usage is rebuilt from the replacement's own data. Owner-requested deletions stay queued.
    db.execute("UPDATE telemetry_item SET state='queued',result='replacement held' WHERE kind='reports' AND state IN ('approved','retry')")
    db.execute("UPDATE telemetry_item SET state='discarded',result='replacement held' WHERE kind='usage' AND state IN ('approved','retry')")
    # No old derived lease, checkpoint, spool, worker intent or reconnect catch-up is live authority.
    observations = list(db.execute('SELECT key,value FROM observation'))
    db.execute('DELETE FROM observation')
    for item in observations:
        db.execute('INSERT INTO observation(key,value,source,observed_at) VALUES (?,?,?,?)',
                   ('recovery.quarantine.' + hashlib.sha256(item['key'].encode()).hexdigest(),
                    json.dumps({'original_key': item['key'], 'value': json.loads(item['value']), 'quarantined': True}),
                    'offline recovery', time.time()))
    db.execute("DELETE FROM setting WHERE key LIKE 'server-backup.%' OR key LIKE 'remote_access.%' OR key LIKE 'devices.%' OR key IN ('remote_access','remote_access_runtime')")
    db.execute('INSERT OR REPLACE INTO setting(key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)',
               ('backup.replacement', json.dumps({'new_main_id': new_id, 'sessions': sorted(sessions),
                    'resume_sessions': sorted(set(resume_sessions)), 'reviewed_nodes': reviewed_nodes,
                    'resume_bindings': {sid: {'node_id': next(nid for nid in reviewed_nodes if nodes[nid]['name'] == sessions[sid]['host']),
                        'account': sessions[sid]['run_as'], 'native_id': sessions[sid]['native_id']} for sid in resume_sessions}})))
    db.execute("INSERT INTO audit_log(ts,actor,action,target,detail) VALUES (CURRENT_TIMESTAMP,?,?,?,?)",
               ('@offline-recovery', 'backup.replacement_prepare', backup_id,
                json.dumps({'backup_id': backup_id, 'new_main_id': new_id, 'sessions_held': len(sessions),
                            'nodes_revalidated': len(reviewed_nodes), 'credentials_dropped': dropped,
                            'effects_released': False})))

def prepare(stage, kit, backup_id, destination, *, reviewed_nodes=None, resume_sessions=()):
    """Unprivileged authenticated preparation. Root must approve the resulting exact hash."""
    reviewed_nodes = reviewed_nodes or {}
    destination = Path(destination)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Existing authenticated restore refuses an occupied destination, links and version/schema skew.
    manifest = validate_and_install(Path(stage), kit, backup_id, destination)
    try:
        _trusted_schema(destination / 'data/acc.db', destination.parent)
        new_id = uuid.uuid4().hex
        secrets = destination / 'data/secrets'
        dropped = [name for name in DROPPED_SECRETS if (secrets / name).exists() or (secrets / name).is_symlink()]
        for name in dropped:
            (secrets / name).unlink()
        with sqlite3.connect(destination / 'data/acc.db') as db:
            quarantine(db, backup_id, new_id, reviewed_nodes, resume_sessions, dropped)
            db.commit()
        # Fresh signing/control identity; old kit remains needed for historical generations.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.generate()
        service = destination / 'data/backup-service'
        private_write(service / 'identity.json', canonical({'installation_id': manifest['installation_id'],
                      'execution_machine_id': new_id, 'public_key': base64.b64encode(key.public_key().public_bytes_raw()).decode()}))
        private_write(service / 'signing.key', key.private_bytes_raw())
        private_write(destination / 'data/REPLACEMENT-ID', new_id.encode())
        plan = {'format': 1, 'backup_id': backup_id, 'installation_id': manifest['installation_id'],
                'old_main_id': manifest['execution_machine_id'], 'new_main_id': new_id,
                'captured_at': manifest['started_at'], 'exclusions': manifest['coverage_exclusions'],
                'version': manifest['version'], 'build': manifest.get('build'), 'schema': manifest['database']['schema'],
                'reviewed_nodes': reviewed_nodes, 'resume_sessions': sorted(set(resume_sessions)),
                'files': _inventory(destination)}
        private_write(destination / 'plan.json', canonical(plan))
        return plan
    except BaseException:
        # Preparation is disposable; never publish a partially sanitized plan.
        shutil.rmtree(destination)
        raise


def held(db, session):
    """A restored session this replacement holds: inspect only, never input, until a permitted fresh Resume."""
    from .server.models import Setting
    row = db.get(Setting, 'backup.replacement') if session.desired_state == 'archived' else None
    return bool(row and session.id in row.value.get('sessions', []))


def resume_permitted(db, session, *, consume=False):
    """Only a fresh manual Resume may unarchive a preselected remote conversation."""
    from sqlalchemy import select

    from .server.models import Node, Setting
    row = db.get(Setting, 'backup.replacement')
    if not row or session.id not in row.value.get('resume_sessions', []) or session.host == 'local':
        return False
    node = db.scalar(select(Node).where(Node.name == session.host, Node.status == 'active'))
    binding = row.value.get('resume_bindings', {}).get(session.id, {})
    if (not node or node.id != binding.get('node_id') or session.run_as != binding.get('account')
            or session.native_id != binding.get('native_id')):
        return False
    expected = row.value.get('reviewed_nodes', {}).get(node.id)
    try:
        allowed = bool(expected and node.public_key and hashlib.sha256(base64.b64decode(node.public_key, validate=True)).hexdigest() == expected)
    except ValueError:
        return False
    if allowed and consume:
        row.value = {**row.value, 'resume_sessions': [sid for sid in row.value['resume_sessions'] if sid != session.id]}
    return allowed


def stopped_services():
    """Conservative systemd check, fixed commands. Never stops or starts anything."""
    for query in ('list-units', 'list-unit-files'):
        result = subprocess.run(['/usr/bin/systemctl', query, '--all', '--no-legend', '--no-pager', '--plain'],
                                capture_output=True, timeout=15, check=True)
        if len(result.stdout) > 1 << 20:
            return False
        for line in result.stdout.decode('ascii').splitlines():
            fields = line.split()
            if fields and fields[0].startswith(('acc-', 'accd.', 'rynhult-', 'rynhultd.')):
                if query == 'list-units' and len(fields) > 2 and fields[2] not in ('inactive', 'failed'):
                    return False
                if query == 'list-unit-files' and len(fields) > 1 and fields[1] not in ('masked', 'masked-runtime'):
                    return False
    return True


class Controller:
    def __init__(self, layout=DEFAULT_LAYOUT, *, owner=0, uid=None, gid=None, stopped=stopped_services):
        self.layout, self.owner, self.stopped = layout, owner, stopped
        self.uid = pwd.getpwnam('acc').pw_uid if uid is None else uid
        self.gid = pwd.getpwnam('acc').pw_gid if gid is None else gid
        self.plan = None
        self.policy_hash = None
        self.evidence = None

    def audit(self, action, result):
        path = self.layout.audit
        parent = _directory(path.parent)
        try:
            info = os.fstat(parent)
            if info.st_uid != self.owner or info.st_mode & 0o022:
                raise Refused('The folder of the replacement audit log must be owned by root and not writable by others. Fix its owner and permissions, then run the step again.')
            fd = os.open(path.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, 'ab') as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner or info.st_nlink != 1 or info.st_mode & 0o077:
                    raise Refused('The replacement audit log must be a single plain file owned by root and not writable by others. Fix its owner and permissions, then run the step again.')
                ids = {key: value if isinstance(value, str) and ID.fullmatch(value) else None
                       for key in ('backup_id', 'installation_id', 'new_main_id')
                       for value in [(self.plan or {}).get(key)]}
                file.write(canonical({'at': time.time(), 'actor': '@root-console', 'uid': os.geteuid(),
                                      'action': action, 'result': result, 'policy_sha256': self.policy_hash, 'fence': self.evidence, **ids}) + b'\n')
                file.flush()
                os.fsync(file.fileno())
                os.fsync(parent)
        finally:
            os.close(parent)

    def _save(self, state):
        private_write(self.layout.state, canonical(state))
        self.layout.state.chmod(0o644)
        fd = _directory(self.layout.state.parent)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _policy(self):
        policy = _json(self.layout.policy, owner=self.owner)
        self.policy_hash = hashlib.sha256(canonical(policy)).hexdigest()
        plan = self.plan
        if (policy.get('enabled') is not True or policy.get('plan_sha256') != plan_digest(plan)
                or any(policy.get(key) != plan[key] for key in ('installation_id', 'backup_id', 'old_main_id', 'new_main_id'))
                or set(policy.get('checks', {})) != set(CHECKS) or any(policy['checks'][key] is not True for key in CHECKS)):
            raise Refused("Activation isn't approved: the replacement policy (/etc/acc-replacement.json) must be enabled, name this preparation plan's SHA-256 and IDs, and set every review check to true.")
        fence, now = policy.get('fence', {}), time.time()
        if (fence.get('kind') not in ('power-off', 'isolation') or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', fence.get('reference', ''))
                or fence.get('restart_prevented') is not True or fence.get('nodes_dns_helpers_isolated') is not True
                or not now - 3600 <= fence.get('at', 0) <= now or not now < fence.get('expires_at', 0) <= fence['at'] + 3600):
            raise Refused("Fencing evidence is missing or expired: record in the replacement policy, within the last hour, that the old main is powered off or isolated, can't restart, and is cut off from machines, DNS and helpers.")
        self.evidence = {key: fence[key] for key in ('kind', 'reference', 'at', 'expires_at')}
        if not self.stopped():
            raise Refused("Rynhult's services and sockets are still running or not masked. Stop and mask all of them (systemctl stop, then systemctl mask), then run the step again.")
        return policy

    def _run(self, action):
        if os.geteuid() != self.owner:
            raise Refused("Run this as root at the machine's local console.")
        lock_parent = _directory(self.layout.lock.parent)
        try:
            info = os.fstat(lock_parent)
            if info.st_uid != self.owner or info.st_mode & 0o022:
                raise Refused('The folder of the replacement lock file must be owned by root and not writable by others. Fix its owner and permissions, then run the step again.')
            fd = os.open(self.layout.lock.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=lock_parent)
        finally:
            os.close(lock_parent)
        with os.fdopen(fd, 'r+b') as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner or info.st_nlink != 1 or info.st_mode & 0o077:
                raise Refused('The replacement lock file must be a single private file owned by root. Fix its owner and permissions, then run the step again.')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.audit(action, 'refused')
                raise Refused('Another replacement step is running. Wait for it to finish, then try again.') from None
            try:
                self.plan = _json(self.layout.prepared / 'plan.json', limit=32 << 20)
                if (self.plan.get('format') != 1 or any(not ID.fullmatch(self.plan.get(key, ''))
                        for key in ('backup_id', 'installation_id', 'old_main_id', 'new_main_id'))
                        or self.plan['new_main_id'] == self.plan['old_main_id']):
                    raise Refused("The prepared plan.json isn't valid. Prepare the replacement again.")
                from alembic.script import ScriptDirectory

                from . import __version__
                from .server.db import alembic_config
                if (self.plan.get('version') != __version__ or self.plan.get('schema') !=
                        ScriptDirectory.from_config(alembic_config('sqlite://')).get_current_head()):
                    raise Refused('This Rynhult release (or its database schema) differs from the one that prepared the replacement. Install that release, or prepare again with this one.')
                build_file = Path(__file__).resolve().parents[2] / '.acc-build'
                if self.plan.get('build') and (not build_file.is_file() or build_file.read_text().strip() != self.plan['build']):
                    raise Refused('This Rynhult build differs from the one that prepared the replacement. Install that build, or prepare again with this one.')
                policy = self._policy()
                state = _json(self.layout.state, owner=self.owner) if self.layout.state.exists() or self.layout.state.is_symlink() else None
                if state and (state.get('plan_sha256') != plan_digest(self.plan) or state.get('new_main_id') != self.plan['new_main_id']):
                    raise Refused("Another replacement is already recorded in /etc/acc-replacement-state.json. Finish or review that one; Rynhult won't start a second.")
                self.audit(action, 'intent')  # audit unavailable => no transition or install
                if action == 'install':
                    return self._install(state, policy)
                phases = {'activate': ('installed', 'recovery'), 'release_control': ('recovery', 'active')}
                before, after = phases[action]
                if not state or state['phase'] not in (before, after):
                    raise Refused("That step can't run now: run install, then activate, then release-control, in order (status shows the current phase).")
                if action == 'release_control' and policy.get('release_control') is not True:
                    raise Refused("Control release isn't approved: set explicit control release (release_control) to true in the replacement policy, then run release-control again.")
                if _inventory(self.layout.prepared) != self.plan['files']:
                    raise Refused('The prepared files changed since the plan was made. Prepare the replacement again.')
                if state['phase'] != after:
                    if _inventory(self.layout.root) != self.plan['files']:
                        raise Refused("The installed files changed after install (installed payload changed before release). Review the installation; Rynhult won't continue.")
                    state.update(phase=after, updated_at=time.time())
                    self._save(state)
                self.audit(action, 'complete')
                return state
            except Exception as error:
                self.audit(action, 'refused')
                if isinstance(error, Refused):
                    raise
                raise Refused('The replacement step stopped on an unexpected error. Nothing more was changed; check the audit log and the held installation.') from None

    def _install(self, state, policy):
        if state:
            if state['phase'] in ('installed', 'recovery', 'active'):
                self.audit('install', 'unchanged')  # every intent has a result
                return state
            raise Refused("An earlier install stopped part way (partial installation). Review and clean up the installation by hand; Rynhult won't install over it.")
        root = _directory(self.layout.root)
        try:
            info = os.fstat(root)
            if info.st_uid != self.owner or info.st_mode & 0o022:
                raise Refused('The Rynhult folder (/srv/acc) must be owned by root and not writable by others. Fix its owner and permissions, then run the step again.')
            if any((self.layout.root / part).exists() or (self.layout.root / part).is_symlink()
                   for part in ('data', 'catalog-local', 'git')):
                raise Refused('The data, catalog-local or git folder already exists in /srv/acc. Install a replacement only on a fresh machine.')
            for relative in ('run', 'run-root', 'publish', 'updates/requests', 'packages/requests', 'accounts/requests'):
                path = self.layout.root / relative
                if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
                    raise Refused('Runtime and helper queues must be fresh: the run, publish and request folders in /srv/acc must be empty. Install a replacement only on a fresh machine.')
            files = self.plan['files']
            if not isinstance(files, dict) or len(files) > MAX_FILES or 'data/acc.db' not in files:
                raise Refused("The prepared plan's file list isn't valid. Prepare the replacement again.")
            state = {key: self.plan[key] for key in ('backup_id', 'installation_id', 'new_main_id', 'old_main_id')}
            state.update(phase='installing', plan_sha256=plan_digest(self.plan), updated_at=time.time())
            self._save(state)  # a crash here or during copying blocks every subsequent startup
            staging = Path(tempfile.mkdtemp(prefix='.replacement-', dir=self.layout.root))
            budget = MAX_BYTES
            for name, expected in files.items():
                _allowed(name)
                target = staging / name
                actual = _stream(self.layout.prepared / name, budget, target, self.uid, self.gid)
                budget -= actual['size']
                if expected != actual:
                    raise Refused('A prepared file changed while it was copied. Prepare the replacement again.')
            for parent, directories, _ in os.walk(staging, topdown=False):
                for directory in directories:
                    path = Path(parent) / directory
                    path.chmod(0o700)
                    os.chown(path, self.uid, self.gid)
                    directory_fd = _directory(path)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            for part in ('data', 'catalog-local', 'git'):
                if (staging / part).exists():
                    os.rename(staging / part, self.layout.root / part)
                    os.fsync(root)
            staging.rmdir()
            state['phase'] = 'installed'
            self._save(state)
            self.audit('install', 'complete')
            return state
        finally:
            os.close(root)

    def install(self): return self._run('install')
    def activate(self): return self._run('activate')
    def release_control(self): return self._run('release_control')


def startup_blocked(data_dir, *, layout=DEFAULT_LAYOUT, owner=0):
    """Before migrations/initialization. Root receipt survives deletion of the acc-owned hold."""
    hold = data_dir / 'RESTORE-HOLD'
    marker = data_dir / 'REPLACEMENT-ID'
    controlled = data_dir == layout.root / 'data' and (layout.policy.exists() or layout.policy.is_symlink()
                                                         or layout.state.exists() or layout.state.is_symlink())
    if not controlled and not any(p.exists() or p.is_symlink() for p in (hold, marker)):
        return False
    try:
        if not controlled:
            return True
        state = _json(layout.state, owner=owner)
        policy = _json(layout.policy, owner=owner)
        identifier = _read(marker, 32).decode()
        return not (state.get('phase') == 'active' and policy.get('enabled') is True
                    and ID.fullmatch(identifier) and state.get('new_main_id') == identifier == policy.get('new_main_id')
                    and all(state.get(key) == policy.get(key) for key in ('backup_id', 'installation_id', 'old_main_id', 'plan_sha256'))
                    and HEX.fullmatch(state.get('plan_sha256', '')))
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline, gated replacement control; never starts services')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('install', 'activate', 'release-control', 'status'):
        sub.add_parser(action, help='Root console only; fixed root policy and paths')
    args = parser.parse_args(argv)
    try:
        # Preparation is backup_server restore --prepare-replacement, which also binds the snapshot and repository.
        if os.geteuid() != 0:
            raise Refused("Run this as root at the machine's local console.")
        result = (_json(DEFAULT_LAYOUT.state, owner=0) if args.action == 'status'
                  else getattr(Controller(), args.action.replace('-', '_'))())
        print(json.dumps({'phase': result['phase'], 'new_main_id': result['new_main_id']}))
        return 0
    except Exception as error:
        # Refused reasons are fixed and secret-free: name the gate. Anything else stays generic.
        reason = f' ({error})' if isinstance(error, Refused) else ''
        print(f'Replacement operation refused{reason}; inspect the offline policy, audit and held installation.',
              file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
