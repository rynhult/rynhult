"""Linux descriptor ACLs. No setfacl/chmod fallback or mutable-path recursion.

Linux's documented POSIX ACL xattr wire format; os.*xattr(int fd) uses f*xattr.
The worker probes filesystem capability before starting a permission mutation.
"""
from __future__ import annotations

import base64
import errno
import os
import stat
import struct
from dataclasses import dataclass


class ACLError(ValueError):
    pass


@dataclass(eq=True)
class ACL:
    entries: list[tuple[int, int, int]]

    def __post_init__(self):
        self.entries.sort()

    @classmethod
    def decode(cls, raw):
        if len(raw) < 4 or (len(raw) - 4) % 8 or struct.unpack('<I', raw[:4])[0] != 2:
            raise ACLError('Unsupported filesystem ACL format.')
        return cls([(tag, perm, -1 if ident == 0xffffffff else ident)
                    for tag, perm, ident in struct.iter_unpack('<HHI', raw[4:])])

    def encode(self):
        return struct.pack('<I', 2) + b''.join(struct.pack('<HHI', t, p, 0xffffffff if i == -1 else i)
                                              for t, p, i in sorted(self.entries))

    def effective(self, tag, ident):
        entries = {(t, i): p for t, p, i in self.entries}
        rights = entries.get((tag, ident), 0)
        return rights & entries.get((16, -1), 7) if tag in (2, 4, 8) else rights

    def grant(self, tag, ident, rights):
        values = {(t, i): p for t, p, i in self.entries}
        previous = dict(values)
        values[tag, ident] = values.get((tag, ident), 0) | rights
        mask = values.get((16, -1), values.get((4, -1), 0)) | rights
        values[16, -1] = mask
        result = ACL([(t, p, i) for (t, i), p in values.items()])
        for (t, i), p in previous.items():
            if t in (2, 4, 8) and (t, i) != (tag, ident) and self.effective(t, i) != result.effective(t, i):
                raise ACLError('A wider mask would activate another masked grant. Ask the administrator to review it.')
        return result


def _read(fd, name):
    try:
        return os.getxattr(fd, name)
    except OSError as error:
        if error.errno == errno.ENODATA:
            return None
        raise ACLError('This filesystem does not support qualified ACL access. No mode fallback is allowed.') from None


def snapshot(fd):
    st = os.fstat(fd)
    if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)) or (stat.S_ISREG(st.st_mode) and st.st_nlink != 1) or st.st_mode & 0o4000 or (stat.S_ISREG(st.st_mode) and st.st_mode & 0o2000):
        raise ACLError('Links, special files and set-id files cannot receive automatic access.')
    return {'identity': [st.st_dev, st.st_ino], 'uid': st.st_uid, 'gid': st.st_gid,
            'mode': stat.S_IMODE(st.st_mode),
            'access': base64.b64encode(_read(fd, 'system.posix_acl_access') or b'').decode(),
            'default': base64.b64encode(_read(fd, 'system.posix_acl_default') or b'').decode() if stat.S_ISDIR(st.st_mode) else ''}


def _base(mode):
    return ACL([(1, mode >> 6 & 7, -1), (4, mode >> 3 & 7, -1), (32, mode & 7, -1)])


def reviewed_acl(acl, old_gid, ident, rights, *, group=False):
    if not group:
        return acl.grant(2, ident, rights)
    if ident == old_gid or any(tag == 8 and key == ident for tag, _, key in acl.entries):
        raise ACLError('The new group ID already has filesystem rights. Ask the administrator to review another reserved ID.')
    values = {(t, i): p for t, p, i in acl.entries}
    values[8, old_gid] = values.get((8, old_gid), 0) | acl.effective(4, -1)
    values[4, -1] = rights
    values[8, ident] = rights
    values[16, -1] = values.get((16, -1), acl.effective(4, -1)) | rights
    result = ACL([(t, p, i) for (t, i), p in values.items()])
    for tag, _, key in acl.entries:
        if tag in (2, 8) and key != ident and acl.effective(tag, key) != result.effective(tag, key):
            raise ACLError('A wider mask would activate another masked grant. Ask the administrator to review it.')
    return result


def proposed(before, uid, *, write, traverse=False, group=False, directory=False):
    mode = before['mode']
    raw = base64.b64decode(before['access'])
    acl = ACL.decode(raw) if raw else _base(mode)
    rights = 1 if traverse else (6 if write else 4) | (1 if directory or mode & 0o111 else 0)
    if traverse:
        return acl.grant(8 if group else 2, uid, rights).encode()
    return reviewed_acl(acl, before['gid'], uid, rights, group=group).encode()


def proposed_default(before, uid, *, write, group=False):
    raw = base64.b64decode(before['default'])
    acl = ACL.decode(raw) if raw else _base(before['mode'])
    return reviewed_acl(acl, before['gid'], uid, 7 if write else 5, group=group).encode()


def grant_fd(fd, uid, *, write, traverse=False, group=False):
    before = snapshot(fd)
    directory = stat.S_ISDIR(os.fstat(fd).st_mode)
    raw = proposed(before, uid, write=write, traverse=traverse, group=group, directory=directory)
    default = None
    if directory and not traverse:
        default = proposed_default(before, uid, write=write, group=group)
    # Both masks are validated before the first write. A ledger is still required
    # by the caller: failure of the second syscall is a partial operation.
    if group and not traverse:
        # Group ownership is an explicitly reviewed operation; never user ownership.
        os.fchown(fd, -1, uid)
        os.fchmod(fd, before['mode'] | (0o2000 if directory else 0))
    os.setxattr(fd, 'system.posix_acl_access', raw)
    if default is not None:
        os.setxattr(fd, 'system.posix_acl_default', default)
    return snapshot(fd)


def restore_fd(fd, before, after):
    if snapshot(fd) != after:
        raise ACLError('Permissions or identity changed after the grant. Undo needs administrator review.')
    for name, key in [('system.posix_acl_access', 'access'), ('system.posix_acl_default', 'default')]:
        if key == 'default' and not stat.S_ISDIR(os.fstat(fd).st_mode):
            continue
        value = base64.b64decode(before[key])
        if value:
            os.setxattr(fd, name, value)
        else:
            try:
                os.removexattr(fd, name)
            except OSError as error:
                if error.errno != errno.ENODATA:
                    raise
    if os.fstat(fd).st_gid != before['gid']:
        os.fchown(fd, -1, before['gid'])
    os.fchmod(fd, before['mode'])


def effective_fd(fd, uid, groups=()):
    """POSIX ACL selection, including the named-user and matched-group precedence."""
    st = os.fstat(fd)
    before = snapshot(fd)
    raw = base64.b64decode(before['access'])
    acl = ACL.decode(raw) if raw else _base(before['mode'])
    values = {(tag, ident): rights for tag, rights, ident in acl.entries}
    if st.st_uid == uid:
        return values[1, -1]
    if (2, uid) in values:
        return acl.effective(2, uid)
    matched, rights = False, 0
    if st.st_gid in groups:
        matched, rights = True, acl.effective(4, -1)
    for tag, perm, gid in acl.entries:
        if tag == 8 and gid in groups:
            matched, rights = True, rights | acl.effective(tag, gid)
    return rights if matched else values[32, -1]
