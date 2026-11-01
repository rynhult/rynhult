"""Uninstalled target-local worker. No agent entrypoint, shell or request-selected UID.

The transport must authenticate the main service, not the agent account. A root
administrator supplies the mapping and qualification; requests cannot supply it.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
from contextlib import ExitStack
from pathlib import PurePosixPath

from .folder_acl import ACL, ACLError, effective_fd, grant_fd, proposed, proposed_default, restore_fd, snapshot
from .folder_policy import (
    FolderError,
    inode,
    mount_type,
    open_directory,
    overlaps,
    reject_nested_mounts,
    trusted_file,
    validate_path,
)

MAX_ENTRIES = 100000
MAX_DEPTH = 64
MAX_SECONDS = 30
MAX_RECORD = 64 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class FolderWorker:
    def __init__(self, state, policy, *, host_root='/', owner=0, reload=None, lifecycle=None):
        self.stack = ExitStack()
        self.owner, self.policy, self.reload = owner, policy, reload
        self.lifecycle = lifecycle
        self.host = self.stack.enter_context(open_directory(str(host_root))).fd
        self.fd = self.stack.enter_context(open_directory(str(state))).fd
        st = os.fstat(self.fd)
        if st.st_uid != owner or stat.S_IMODE(st.st_mode) != 0o700:
            self.stack.close()
            raise FolderError('Folder worker state must be private service state.')
        self.lock = os.open('operation.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        trusted_file(self.lock, owner=owner)
        self.stack.callback(os.close, self.lock)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stack.close()

    def read(self, name, default=None):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return default
        try:
            trusted_file(fd, owner=self.owner)
            with os.fdopen(fd, 'rb', closefd=False) as file:
                raw = file.read(MAX_RECORD + 1)
            if len(raw) > MAX_RECORD:
                raise FolderError('The folder operation record is too large.')
            return json.loads(raw)
        finally:
            os.close(fd)

    def write(self, name, value):
        # Exclusive staging, same directory, no symlink targets, fsync both levels.
        # Unique names leave a crashed stage harmless rather than blocking retries.
        staging = '.stage-' + secrets.token_hex(16)
        raw = json.dumps(value, sort_keys=True).encode()
        if len(raw) > MAX_RECORD:
            raise FolderError('The permission ledger exceeds its byte budget. Use a narrower folder.')
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as file:
                file.write(raw)
                file.flush()
                os.fsync(fd)
            os.rename(staging, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            os.close(fd)
            try:
                os.unlink(staging, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def handle(self, message):
        if not isinstance(message, dict):
            raise FolderError('Invalid folder operation.')
        action = message.get('action')
        common = {'action', 'operation_id', 'machine_id', 'account', 'revision'}
        extras = {'preview': {'path', 'method', 'write'}, 'apply': {'digest'}, 'undo': {'digest'}, 'cancel': {'digest'},
                  'status': set(), 'policy_preview': {'roots', 'allowed_folders'}, 'policy_apply': {'digest'},
                  'policy_recover': {'digest', 'recovery_digest'}}
        if (action not in extras or set(message) != common | extras[action]
                or not re.fullmatch('[0-9a-f]{32}', str(message.get('operation_id', '')))
                or message.get('machine_id') != self.policy.get('machine_id')
                or message.get('account') not in self.policy.get('accounts', {})
                or type(message.get('revision')) is not int or message['revision'] < 0):
            raise FolderError('Invalid folder operation or machine/account identity.')
        self.started, self.visited = time.monotonic(), 0
        if action in ('policy_preview', 'policy_apply') and self.policy.get('policy_qualified') is not True:
            raise FolderError('The target policy writer has not been qualified. Use existing configured roots.')
        account = self.policy['accounts'][message['account']]
        if account['uid'] < 1000 or (action in ('preview', 'apply', 'undo') and account['uid'] == account['owner_uid']):
            raise FolderError('Grant access requires a separate provisioned agent account.')
        # Never wait indefinitely for another grant. IPC retries can read status.
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FolderError('Another folder operation is running. Check its status first.') from None
        try:
            if action in ('apply', 'undo'):
                if self.policy.get('qualified') is not True:
                    raise FolderError('Automatic grants are not qualified on this target.')
                reviewed = self.read(message['operation_id'] + '.json')
                if (not reviewed or reviewed['request']['action'] != 'preview'
                        or reviewed['request']['account'] != message['account']
                        or reviewed['request']['machine_id'] != message['machine_id'] or reviewed['digest'] != message['digest']
                        or reviewed.get('mapping_digest') != digest({k: v for k, v in account.items() if k != 'groups'})):
                    raise FolderError('No matching reviewed folder operation.')
                if reviewed['request']['method'] == 'shared_group' and self.policy.get('shared_group_qualified') is not True:
                    raise FolderError('The shared-group method is not qualified on this target.')
                if action == 'apply' and reviewed['state'] == 'applied' and reviewed.get('result'):
                    return reviewed['result']
                if action == 'undo' and reviewed['state'] == 'undone' and reviewed.get('result'):
                    return reviewed['result']
                access = self.read('access-' + message['account'] + '.json', {'revision': 0})
                if access['revision'] != message['revision']:
                    raise FolderError('The access revision changed. Review again.')
                if action == 'apply' and (reviewed['state'] != 'preview' or reviewed['expires_at'] <= time.time()):
                    raise FolderError('The folder preview expired, changed or was cancelled. Review again.')
                if action == 'undo' and reviewed['state'] not in ('applied', 'partial', 'applying', 'repair'):
                    raise FolderError('This operation cannot be undone.')
                if self.lifecycle is None:
                    raise FolderError('Stopped-account and agent-access qualification are required.')
                try:
                    with self.lifecycle.stopped(account):
                        result = self._handle(message, account)
                    return result
                except (OSError, ValueError):
                    name = message['operation_id'] + '.json'
                    record = self.read(name)
                    if record and record.get('state') in ('applying', 'applied', 'partial', 'undone'):
                        record['state'], record['result'] = 'partial', {'state': 'partial', 'message': 'Access or broker recovery needs review.'}
                        self.write(name, record)
                    raise
            return self._handle(message, account)
        except ACLError as error:
            raise FolderError(str(error)) from None
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)

    def _budget(self):
        self.visited += 1
        if self.visited > MAX_ENTRIES or time.monotonic() - self.started >= MAX_SECONDS:
            raise FolderError('The folder exceeds the preview budget. Choose a narrower folder.')

    def _manifest(self, path, account, write, stack, inspect_only=False, group=None):
        validate_path(path, homes=account['homes'], protected=account['protected'])
        root = stack.enter_context(open_directory(path, root_fd=self.host))
        mount = root.identity[2]
        if mount_type(root.fd) in ('9p', 'drvfs', 'fuse.drvfs'):
            raise FolderError('Windows drives use existing access only; no automatic permission changes.')
        with open_directory(path.rsplit('/', 1)[0], root_fd=self.host) as parent:
            if parent.identity[2] != mount:
                raise FolderError('Mount roots and bind aliases need administrator provisioning.')
        records, descriptors = [], []
        started = self.started

        def visit(fd, relative, depth):
            self._budget()
            if len(records) >= MAX_ENTRIES or depth > MAX_DEPTH or time.monotonic() - started >= MAX_SECONDS:
                raise FolderError('The folder exceeds the preview budget. Choose a narrower folder.')
            st = os.fstat(fd)
            if inode(fd)[2] != mount or st.st_uid not in (account['owner_uid'], account['uid']):
                raise FolderError('Mount crossings and other owners require administrator provisioning.')
            before = snapshot(fd)
            access = None if inspect_only else proposed(before, group['gid'] if group else account['uid'], write=write, group=group is not None, directory=stat.S_ISDIR(st.st_mode))
            default = None
            if not inspect_only and stat.S_ISDIR(st.st_mode):
                default = proposed_default(before, group['gid'] if group else account['uid'], write=write, group=group is not None)
            records.append({'path': relative, 'before': before, 'access': base64.b64encode(access).decode() if access is not None else None,
                            'default': base64.b64encode(default).decode() if default is not None else None})
            descriptors.append(fd)
            if stat.S_ISDIR(st.st_mode):
                with os.scandir(fd) as children:
                    for entry in children:
                        if time.monotonic() - started >= MAX_SECONDS:
                            raise FolderError('The folder exceeds the preview budget. Choose a narrower folder.')
                        name = entry.name
                        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                        stack.callback(os.close, child)
                        visit(child, relative + '/' + name if relative else name, depth + 1)

        try:
            visit(root.fd, '', 0)
        except (OSError, ACLError):
            raise FolderError('Unsafe link, ACL, file ownership or filesystem. No access was granted.') from None
        # Git metadata is never interpreted by root. Linked/outside Git admin is
        # refused instead of invoking git or following a pointer during a grant.
        for record, fd in zip(records, descriptors, strict=True):
            rel = record['path']
            if rel == '.git' and not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise FolderError('Linked Git metadata needs separate administrator approval.')
            if rel in ('.git/objects/info/alternates', '.git/commondir'):
                raise FolderError('External Git metadata needs separate administrator approval.')
        ordered = sorted(zip(records, descriptors, strict=True), key=lambda item: item[0]['path'])
        return root.identity, [r for r, _ in ordered], [fd for _, fd in ordered]

    def _ancestors(self, path, account):
        candidate = PurePosixPath(path)
        home = next((PurePosixPath(p) for p in account['homes'] if PurePosixPath(p) in candidate.parents), None)
        if home is None:
            raise FolderError('Choose a repository subtree of the associated home.')
        records = []
        shared = self.read('ancestors.json', {})
        blocked = False
        current = home
        parts = candidate.relative_to(home).parts
        for next_component in parts:
            self._budget()
            with open_directory(str(current), root_fd=self.host) as directory:
                before = snapshot(directory.fd)
                if before['uid'] not in (account['owner_uid'], account['uid']):
                    raise FolderError('Other-owned ancestors need administrator provisioning.')
                rights = effective_fd(directory.fd, account['uid'], account.get('groups', []))
                key = ':'.join(map(str, [*before['identity'], account['uid']]))
                existing = shared.get(key)
                if existing is not None:
                    if before != existing['after']:
                        raise FolderError('An ancestor grant changed. Repair it before adding another grant.')
                    records.append({'path': str(current), 'key': key, 'before': before, 'shared': True})
                elif not rights & 1:
                    blocked = True
                    # A new traverse entry must not awaken someone else's mask.
                    proposed(before, account['uid'], write=False, traverse=True, directory=True)
                    records.append({'path': str(current), 'key': key, 'before': before, 'shared': False})
                elif blocked and rights & 6:
                    raise FolderError('Traversal would expose an unrelated readable ancestor. Use local setup.')
                if blocked:
                    with os.scandir(directory.fd) as children:
                        for entry in children:
                            self._budget()
                            if entry.name == next_component:
                                continue
                            fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory.fd)
                            try:
                                if effective_fd(fd, account['uid'], account.get('groups', [])):
                                    raise FolderError('Traversal would expose unrelated files. Use local setup.')
                            finally:
                                os.close(fd)
            current /= next_component
        return records

    def _apply_ancestors(self, ancestors, operation, old, op_name, account):
        shared = self.read('ancestors.json', {})
        old['ancestor_keys'] = []
        for entry in ancestors:
            with open_directory(entry['path'], root_fd=self.host) as directory:
                if snapshot(directory.fd) != entry['before']:
                    raise FolderError('Ancestor permissions changed. Review again.')
                record = shared.get(entry['key'])
                if record is None:
                    record = {'path': entry['path'], 'before': entry['before'], 'after': None, 'refs': []}
                    shared[entry['key']] = record
                    self.write('ancestors.json', shared)
                    record['after'] = grant_fd(directory.fd, account['uid'], write=False, traverse=True)
                if operation not in record['refs']:
                    record['refs'].append(operation)
                self.write('ancestors.json', shared)
                old['ancestor_keys'].append(entry['key'])
                self.write(op_name, old)

    def _undo_ancestors(self, old, operation, conflicts):
        shared = self.read('ancestors.json', {})
        for key in reversed(old.get('ancestor_keys', [])):
            entry = shared.get(key)
            if not entry or operation not in entry['refs']:
                continue
            others = [ref for ref in entry['refs'] if ref != operation]
            if others:
                entry['refs'] = others
            else:
                try:
                    with open_directory(entry['path'], root_fd=self.host) as directory:
                        if entry['after'] is None:
                            raise FolderError('Unconfirmed ancestor ACL mutation.')
                        restore_fd(directory.fd, entry['before'], entry['after'])
                    del shared[key]
                except (OSError, ACLError, FolderError):
                    conflicts.append('Ancestor permission conflict')
            self.write('ancestors.json', shared)

    def _check_name(self, root, relative, fd):
        path = root + ('/' + relative if relative else '')
        with open_directory(path.rsplit('/', 1)[0], root_fd=self.host) as parent:
            current = os.open(path.rsplit('/', 1)[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent.fd)
            try:
                if inode(current) != inode(fd):
                    raise FolderError('Folder entries moved during apply. Review partial access and Undo.')
            finally:
                os.close(current)

    def _no_overlap(self, path, operation):
        index = self.read('grants-index.json', {})
        if any(key != operation and overlaps(PurePosixPath(path), PurePosixPath(value['path']))
               for key, value in index.items()):
            raise FolderError('This grant overlaps an existing grant. Reuse its access or Undo it first.')

    def _check_policy_roots(self, operation, account):
        for path, identity in zip(operation['request']['roots'], operation['identities'], strict=True):
            if identity is not None:
                validate_path(path, homes=account['homes'], protected=account['protected'])
                with open_directory(path, root_fd=self.host, expected=identity, same_mount=True) as directory:
                    reject_nested_mounts(directory.fd)

    def _recover_policy(self, message, account, state, old, consent, mapping, name, op_name):
        """Close the old intent before reloading only the acknowledged baseline.

        A lost reload reply may have left the broker ahead of durable account
        state. Never send a conflicting rollback at the abandoned revision.
        """
        if old and (old['request']['action'] != 'policy_preview' or old['digest'] != message['digest']):
            raise FolderError('No matching published policy operation.')
        prior = old.get('recovery') if old else None
        replay = bool(prior and prior['consent'] == message['recovery_digest'] and prior['mapping'] == mapping
                      and state in (prior['baseline'], prior['desired']))
        if not replay and message['recovery_digest'] != consent:
            raise FolderError('Target status changed. Review recovery again.')
        if self.reload is None:
            raise FolderError('The account service is unavailable. Recovery remains pending.')
        if old is None:
            # Restored target state: a tombstone also prevents a delayed old
            # preview/apply from recreating the abandoned intent.
            old = {'request': {**message, 'action': 'policy_preview'}, 'digest': message['digest'],
                   'mapping_digest': mapping}
        if not replay:
            revision = max(state['revision'], message['revision'], old.get('desired', {}).get('revision', 0),
                           (prior or {}).get('desired', {}).get('revision', 0)) + 1
            old['recovery'] = {'consent': message['recovery_digest'], 'mapping': mapping, 'baseline': state,
                               'desired': {**state, 'revision': revision}}
        desired = old['recovery']['desired']
        old['state'] = 'recovering'
        self.write(op_name, old)  # closes policy_apply BEFORE any reload
        reply = self.reload(message['account'], {**desired, 'uid': account['uid'],
            'homes': account['homes'], 'protected': account['protected']})
        if not isinstance(reply, dict) or type(reply.get('revision')) is not int or reply['revision'] != desired['revision']:
            raise FolderError('The account service has not acknowledged recovery. Retry recovery after reconnecting.')
        self.write(name, desired)
        old['state'] = 'recovered'
        self.write(op_name, old)
        return {'state': 'recovered', **desired}

    def _handle(self, message, account):
        name = 'account-' + message['account'] + '.json'
        state = self.read(name, {'revision': 0, 'roots': account['roots'], 'allowed_folders': True})
        op_name = message['operation_id'] + '.json'
        old = self.read(op_name)
        if old is not None and (old['request']['account'] != message['account'] or old['request']['machine_id'] != message['machine_id']):
            raise FolderError('The operation belongs to another machine or assigned account.')
        mapping = digest({k: v for k, v in account.items() if k != 'groups'})
        changed = bool(old and old.get('mapping_digest') != mapping)
        # Expiry and status precede the mapping refusal. Recovery needs a durable
        # answer even when the original operation can no longer be completed.
        if (old and old['request']['action'] == 'policy_preview'
                and old['state'] == 'preview' and old['expires_at'] <= time.time()):
            old['state'] = 'expired'
            self.write(op_name, old)
        recovery_revision = (old or {}).get('recovery', {}).get('desired', {}).get('revision')
        consent = digest({'state': state, 'mapping': mapping, 'operation': message['operation_id'],
                          'digest': old.get('digest') if old else None, 'recovery_revision': recovery_revision})
        if message['action'] == 'status':
            access = self.read('access-' + message['account'] + '.json', {'revision': 0})
            completable = bool(old and not changed and self.policy.get('policy_qualified') is True
                               and old['state'] in ('preview', 'pending', 'applied'))
            if completable and old['request']['action'] == 'policy_preview':
                try:
                    self._check_policy_roots(old, account)
                except (OSError, ValueError):
                    completable = False
            return {'revision': state['revision'], 'access_revision': access['revision'],
                    'roots': list(state['roots']), 'allowed_folders': state['allowed_folders'],
                    'operation': ({**(old.get('result') or {}), 'state': old['state'],
                                   'mapping_changed': changed, 'can_complete': completable} if old else None),
                    'recovery_digest': consent, 'recovery_revision': recovery_revision, 'policy_recovery': True,
                    'preview': old.get('summary') if old else None,
                    'shared_group_qualified': self.policy.get('shared_group_qualified') is True,
                    'qualified': self.policy.get('qualified') is True, 'policy_qualified': self.policy.get('policy_qualified') is True}
        if message['action'] == 'policy_recover':
            return self._recover_policy(message, account, state, old, consent, mapping, name, op_name)
        if changed:
            raise FolderError('The assigned account mapping changed. Review again.')
        if message['action'] == 'policy_apply' and old and old['state'] in ('recovering', 'recovered'):
            raise FolderError('This operation was closed for owner recovery. Review again.')
        if (message['action'] == 'policy_apply' and old and old.get('state') == 'pending'
                and old.get('desired') == state and old['digest'] == message['digest']
                and old['request']['account'] == message['account'] and old['request']['revision'] == message['revision']):
            if self.reload is None:
                raise FolderError('The account service is unavailable. Retry after local repair.')
            ack = self.reload(message['account'], {**state, 'uid': account['uid'],
                'homes': account['homes'], 'protected': account['protected']})
            if not isinstance(ack, dict) or type(ack.get('revision')) is not int or ack.get('revision') != state['revision']:
                raise FolderError('The account service did not re-acknowledge this revision.')
            old['state'], old['result'] = 'applied', {'state': 'applied', 'revision': state['revision']}
            self.write(op_name, old)
            return old['result']
        if message['action'] == 'apply' and old and old.get('state') == 'cancelled':
            raise FolderError('This access request was cancelled. Review again.')
        if message['action'] in ('apply', 'policy_apply') and old and old.get('result') and old['request']['account'] == message['account'] and old['request']['revision'] == message['revision'] and old['digest'] == message['digest']:
            if message['action'] == 'policy_apply':
                if state['revision'] != old['result']['revision'] or self.reload is None:
                    raise FolderError('This policy operation is no longer current. Review again.')
                ack = self.reload(message['account'], {**state, 'uid': account['uid'],
                    'homes': account['homes'], 'protected': account['protected']})
                if not isinstance(ack, dict) or type(ack.get('revision')) is not int or ack.get('revision') != state['revision']:
                    raise FolderError('The broker has not acknowledged this revision. Retry after local repair.')
            return old['result']
        if message['action'] == 'cancel':
            if not old or old['digest'] != message['digest'] or old['request']['account'] != message['account'] or old['request']['action'] != 'preview':
                raise FolderError('No matching access preview.')
            if old['state'] not in ('preview', 'cancelled'):
                raise FolderError('Access work may have started. Check its result or review Undo.')
            old['state'] = 'cancelled'
            old['result'] = {'state': 'cancelled', 'message': 'Unstarted access request cancelled. No OS changes.'}
            self.write(op_name, old)
            return old['result']
        if message['action'] in ('preview', 'apply', 'undo'):
            name = 'access-' + message['account'] + '.json'
            state = self.read(name, {'revision': 0})
        if state['revision'] != message['revision']:
            raise FolderError('The folder revision changed. Review again.')
        if message['action'] == 'policy_preview':
            roots = message['roots']
            if (old is not None or type(message['allowed_folders']) is not bool or not isinstance(roots, list)
                    or not 1 <= len(roots) <= 128 or len(set(roots)) != len(roots)
                    or roots[0] != account['roots'][0]):
                raise FolderError('Choose a whole root set retaining the default workspace folder.')
            identities = []
            for path in roots:
                if path in account['roots']:
                    identities.append(None)  # unchanged installer roots retain legacy provenance, not new validation
                    continue
                validate_path(path, homes=account['homes'], protected=account['protected'])
                with open_directory(path, root_fd=self.host, same_mount=True) as directory:
                    reject_nested_mounts(directory.fd)
                    identities.append(directory.identity)
            preview = {'request': message, 'identities': identities, 'expires_at': time.time() + 300, 'state': 'preview'}
            preview['mapping_digest'] = digest({k: v for k, v in account.items() if k != 'groups'})
            preview['digest'] = digest(preview)
            self.write(op_name, preview)
            return {'digest': preview['digest'], 'roots': roots, 'state': 'preview', 'expires_at': preview['expires_at'],
                    'legacy_roots': [r for r, i in zip(roots, identities, strict=True) if i is None]}
        if message['action'] == 'policy_apply':
            if (not old or old['request']['action'] != 'policy_preview' or old['request']['account'] != message['account']
                    or old['digest'] != message['digest'] or old['state'] not in ('preview', 'pending')
                    or (old['state'] == 'preview' and old['expires_at'] <= time.time())):
                raise FolderError('The policy preview expired or changed. Review again.')
            if self.reload is None:
                raise FolderError('The installed broker cannot acknowledge this policy. Local setup is required.')
            self._check_policy_roots(old, account)
            desired = {'revision': state['revision'] + 1, 'roots': old['request']['roots'],
                       'allowed_folders': old['request']['allowed_folders']}
            old['state'], old['desired'] = 'pending', desired
            self.write(op_name, old)
            reply = self.reload(message['account'], {**desired, 'uid': account['uid'],
                'homes': account['homes'], 'protected': account['protected']})
            if not isinstance(reply, dict) or type(reply.get('revision')) is not int or reply.get('revision') != desired['revision']:
                raise FolderError('The broker did not acknowledge this revision. Policy remains pending.')
            self.write(name, desired)
            old['state'], old['result'] = 'applied', {'state': 'applied', 'revision': desired['revision']}
            self.write(op_name, old)
            return old['result']
        if message['action'] == 'preview':
            self._no_overlap(message['path'], message['operation_id'])
            if old is not None:
                raise FolderError('This operation ID is already used. Start a fresh preview.')
            if message['method'] not in ('named_acl', 'shared_group') or type(message['write']) is not bool:
                raise FolderError('This target has not qualified the requested grant method.')
            group = None
            if message['method'] == 'shared_group':
                if self.policy.get('shared_group_qualified') is not True:
                    raise FolderError('The shared-group method is not qualified on this target.')
                if self.lifecycle is None:
                    raise FolderError('The shared-group lifecycle is not qualified on this target.')
                group = self.lifecycle.plan_group(message['operation_id'], account)
            ancestors = self._ancestors(message['path'], account)
            with ExitStack() as stack:
                identity, records, _ = self._manifest(message['path'], account, message['write'], stack, group=group)
            # Ancestors use separate traverse-only ledger entries and shared
            # references; no sibling grant is revoked by another grant's Undo.
            preview = {'request': message, 'identity': identity, 'records': records, 'ancestors': ancestors, 'group': group,
                       'expires_at': time.time() + 300, 'state': 'preview'}
            # Reserve enough for the complete before/after progress ledger before
            # granting anything. Large ACLs may hit bytes before the entry cap.
            worst = len(json.dumps(preview).encode()) + sum(2 * len(json.dumps(r['before']).encode()) + 512 for r in records)
            if worst > MAX_RECORD:
                raise FolderError('The permission ledger exceeds its byte budget. Choose a narrower folder.')
            preview['mapping_digest'] = digest({k: v for k, v in account.items() if k != 'groups'})
            preview['digest'] = digest(preview)
            changes = []
            for record in records:
                before = record['before']
                changes.append({'path': record['path'] or '.', 'owner': before['uid'],
                    'group_before': before['gid'], 'group_after': group['gid'] if group else before['gid'],
                    'mode_before': oct(before['mode']),
                    'mode_add': 'setgid on directory' if group and record['default'] else 'none',
                    'access_before': ACL.decode(base64.b64decode(before['access'])).entries if before['access'] else [],
                    'access_after': ACL.decode(base64.b64decode(record['access'])).entries,
                    'default_before': ACL.decode(base64.b64decode(before['default'])).entries if before['default'] else [],
                    'default_after': ACL.decode(base64.b64decode(record['default'])).entries if record['default'] else []})
            summary = {'digest': preview['digest'], 'count': len(records), 'agent_uid': account['uid'], 'owner_uid': account['owner_uid'], 'expires_at': preview['expires_at'],
                'identity': identity, 'method': message['method'], 'write': message['write'], 'group': group,
                'path': message['path'], 'changes': changes, 'state': 'preview', 'ancestors': [e['path'] for e in ancestors],
                'ancestor_changes': [{'path': e['path'], 'shared': e['shared'], 'default_changed': False,
                    'access_before': ACL.decode(base64.b64decode(e['before']['access'])).entries if e['before']['access'] else [],
                    'access_after': ACL.decode(proposed(e['before'], account['uid'], write=False, traverse=True, directory=True)).entries}
                    for e in ancestors]}
            if len(json.dumps(summary).encode()) > 48000:
                raise FolderError('The exact permission review is too large. Choose a narrower folder.')
            preview['summary'] = summary
            self.write(op_name, preview)
            return summary
        if (old is None or old['digest'] != message['digest'] or old['request']['account'] != message['account']):
            raise FolderError('No matching reviewed folder operation.')
        if self.policy.get('qualified') is not True:
            raise FolderError('Automatic grants are not qualified on this target. Use existing access or local setup.')
        if message['action'] == 'apply':
            self._no_overlap(old['request']['path'], message['operation_id'])
            if old['state'] != 'preview' or old['expires_at'] <= time.time():
                raise FolderError('The folder preview expired or changed. Review again.')
            ancestors = self._ancestors(old['request']['path'], account)
            if ancestors != old['ancestors']:
                raise FolderError('Ancestor permissions changed. Review again.')
            with ExitStack() as stack:
                identity, records, descriptors = self._manifest(old['request']['path'], account, old['request']['write'], stack, group=old.get('group'))
                if identity != old['identity'] or records != old['records']:
                    raise FolderError('The folder identity or permissions changed. Review again.')
                index = self.read('grants-index.json', {})
                index[message['operation_id']] = {'path': old['request']['path'], 'account': message['account']}
                self.write('grants-index.json', index)
                old['state'], old['ledger'] = 'applying', []
                self.write(op_name, old)
                try:
                    if old.get('group'):
                        self.lifecycle.create_group(old['group'], account, lambda: self.write(op_name, old))
                    self._apply_ancestors(ancestors, message['operation_id'], old, op_name, account)
                    for record, fd in zip(records, descriptors, strict=True):
                        if snapshot(fd) != record['before']:
                            raise FolderError('Permissions changed during apply. Review partial access and Undo.')
                        # Write intent first. A crash or failed second ACL syscall
                        # leaves an honest repair state, never a claimed success.
                        item = {'path': record['path'], 'before': record['before'], 'after': None}
                        old['ledger'].append(item)
                        self.write(op_name, old)
                        self._check_name(old['request']['path'], record['path'], fd)
                        item['after'] = grant_fd(fd, old['group']['gid'] if old.get('group') else account['uid'],
                            write=old['request']['write'], group=bool(old.get('group')))
                        self.write(op_name, old)
                    verification = self.lifecycle.verify(old['request']['path'], records, account,
                        old['request']['write'], group=old.get('group'))
                except BaseException:
                    old['state'] = 'partial'
                    self.write(op_name, old)
                    raise
            state['revision'] += 1
            self.write(name, state)
            old['state'] = 'applied'
            old['result'] = {'state': 'applied', 'revision': state['revision'], 'changed': len(records), 'verification': verification}
            self.write(op_name, old)
            return old['result']
        if old['state'] not in ('applied', 'partial', 'applying', 'repair'):
            raise FolderError('This operation cannot be undone.')
        # Re-open from a pinned host root and compare every inode and complete
        # post-grant ACL. A conflict never overwrites a later human change.
        conflicts = []
        try:
            with ExitStack() as stack:
                _, current, _ = self._manifest(old['request']['path'], account, False, stack, inspect_only=True)
            known = {tuple(item['before']['identity']) for item in old['ledger']}
            conflicts.extend(record['path'] for record in current if tuple(record['before']['identity']) not in known)
        except FolderError:
            conflicts.append('Unconfirmed descendants')
        for item in reversed(old['ledger']):
            if item.get('undone'):
                continue
            path = old['request']['path'] + ('/' + item['path'] if item['path'] else '')
            try:
                with open_directory(path.rsplit('/', 1)[0], root_fd=self.host) as parent:
                    fd = os.open(path.rsplit('/', 1)[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent.fd)
                    try:
                        if item['after'] is None:
                            raise FolderError('A partial syscall needs administrator review.')
                        restore_fd(fd, item['before'], item['after'])
                    finally:
                        os.close(fd)
                item['undone'] = True
                self.write(op_name, old)
            except (OSError, ACLError, FolderError):
                conflicts.append(item['path'])
        self._undo_ancestors(old, message['operation_id'], conflicts)
        group_note = None
        if old.get('group') and not conflicts:
            group_note = self.lifecycle.remove_group_members(old['group'], account, lambda: self.write(op_name, old))
        state['revision'] += 1
        self.write(name, state)
        old['state'] = 'repair' if conflicts else 'undone'
        if not conflicts:
            index = self.read('grants-index.json', {})
            index.pop(message['operation_id'], None)
            self.write('grants-index.json', index)
        old['result'] = {'state': old['state'], 'revision': state['revision'], 'conflicts': len(conflicts),
                         'group_note': group_note, 'remaining_access': 'Recheck required; inherited/new files and other grants may retain access.'}
        self.write(op_name, old)
        return old['result']


def reload_broker(account, payload, *, socket_path=None):
    """Fixed mapped socket; the broker accepts this method only from root's peer UID."""
    import socket
    import struct
    if not re.fullmatch('[a-z_][a-z0-9_-]{0,31}', account):
        raise FolderError('Invalid provisioned account.')
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(5)
        channel.connect(socket_path or '/run/acc-broker/' + account + '.sock')
        peer = struct.unpack('3i', channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        if peer not in (0, payload['uid']):
            raise FolderError('The account broker identity changed.')
        channel.sendall(json.dumps({'id': 1, 'method': 'folder_policy_apply', 'params': payload}).encode() + b'\n')
        with channel.makefile('rb') as file:
            raw = file.readline(65537)
        if len(raw) > 65536:
            raise FolderError('Invalid broker acknowledgement.')
        reply = json.loads(raw)
        if not isinstance(reply, dict) or reply.get('id') != 1 or reply.get('ok') is not True:
            raise FolderError('The account broker cannot reload folder policy. Local setup is required.')
        return reply.get('result')


def main():
    """Socket-activated fixed worker; never installed/run by the development task."""
    import pwd
    import socket
    import struct
    import sys
    if os.geteuid() != 0 or sys.platform != 'linux' or len(sys.argv) != 1:
        raise SystemExit('The folder worker requires its reviewed Linux root service.')
    os.environ.clear()
    os.environ.update(PATH='/usr/sbin:/usr/bin:/sbin:/bin', LANG='C.UTF-8', TZ='UTC')
    with open_directory('/etc/acc/folders') as directory:
        fd = os.open('worker.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory.fd)
        try:
            trusted_file(fd)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                raise FolderError('The worker mapping must be private root configuration.')
            with os.fdopen(fd, 'rb', closefd=False) as file:
                raw = file.read(65537)
            if len(raw) > 65536:
                raise FolderError('The worker mapping is too large.')
            policy = json.loads(raw)
        finally:
            os.close(fd)
    service_uid = policy.get('service_uid')
    if type(service_uid) is not int or service_uid <= 0:
        raise FolderError('Provision the main/node service identity.')
    for account, coordinates in policy.get('accounts', {}).items():
        if not re.fullmatch('[a-z_][a-z0-9_-]{0,31}', account) or account in ('root', 'acc', 'acc-node'):
            raise FolderError('Invalid provisioned agent account.')
        record = pwd.getpwnam(account)
        if record.pw_uid != coordinates['uid'] or record.pw_uid < 1000 or record.pw_uid == service_uid:
            raise FolderError('The provisioned agent identity changed.')
        if type(coordinates.get('owner_uid')) is not int or coordinates['owner_uid'] < 1000:
            raise FolderError('Provision the associated unprivileged human identity.')
        human = pwd.getpwuid(coordinates['owner_uid'])
        coordinates['agent_name'], coordinates['owner_name'] = record.pw_name, human.pw_name
        coordinates['gid'] = record.pw_gid
        if coordinates.get('broker_unit') != f'acc-broker@{account}.service' or coordinates.get('broker_socket_unit') != f'acc-broker@{account}.socket':
            raise FolderError('Provision the fixed agent service and socket units.')
        from .folder_policy import literal_path
        socket_path = coordinates.get('broker_socket')
        literal_path(socket_path)
        with open_directory(socket_path.rsplit('/', 1)[0]) as parent:
            info = os.stat(socket_path.rsplit('/', 1)[1], dir_fd=parent.fd, follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid not in (0, record.pw_uid) or info.st_mode & 0o002:
                raise FolderError('The provisioned broker socket is not trusted.')
        coordinates['groups'] = sorted(os.getgrouplist(account, record.pw_gid))
    with socket.socket(fileno=0) as connection:
        connection.settimeout(40)
        uid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        if uid != service_uid:
            raise FolderError('Only the mapped main/node service may request a folder operation.')
        with connection.makefile('rb') as input:
            raw = input.readline(65537)
        try:
            if len(raw) > 65536 or not raw.endswith(b'\n'):
                raise FolderError('Invalid folder request size.')
            from .folder_host import LinuxFolderHost
            def reload(account, value):
                return reload_broker(account, value, socket_path=policy['accounts'][account]['broker_socket'])
            with FolderWorker('/var/lib/acc-folder-policy', policy, reload=reload) as worker:
                def reack(account):
                    state = worker.read('account-' + account['agent_name'] + '.json')
                    if state:
                        result = reload(account['agent_name'], {**state, 'uid': account['uid'],
                            'homes': account['homes'], 'protected': account['protected']})
                        if result.get('revision') != state['revision']:
                            raise FolderError('The restarted account service has not acknowledged folder policy.')
                worker.lifecycle = LinuxFolderHost(reack=reack)
                result = worker.handle(json.loads(raw))
            response = {'ok': True, 'result': result}
        except (OSError, ValueError, TypeError, KeyError):
            # No paths, ACL manifests, exception strings or internal names on wire.
            response = {'ok': False}
        raw = json.dumps(response).encode() + b'\n'
        connection.sendall(raw if len(raw) <= 64512 else b'{"ok":false}\n')
