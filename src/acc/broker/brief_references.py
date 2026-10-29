"""Private, chunked reference delivery inside the session's git-ignored worktree."""
import base64
import hashlib
import hmac
import json
import os
import re
import stat
from pathlib import PurePosixPath

from .ops import ACC_ID, OpError, _hardened_git, _under_roots, _walk_dirs


def signature(body, key):
    message=json.dumps(body,sort_keys=True,separators=(',',':')).encode()
    return hmac.new(key,message,hashlib.sha256).hexdigest()


def seal(session, cwd, key):
    info=os.stat(cwd,follow_symlinks=False)
    body={'v':1,'session':session,'cwd':cwd,'device':info.st_dev,'inode':info.st_ino}
    return json.dumps({**body,'mac':signature(body,key)})


def receive(config, session, reference, extension, offset, data, *, binding_key=None):
    if (not isinstance(session, str) or not ACC_ID.fullmatch(session)
            or not isinstance(reference, str) or not re.fullmatch(r'REF-[1-9][0-9]{0,5}', reference)
            or extension not in ('png', 'jpg', 'webp', 'pdf', 'md', 'txt', 'html', 'rtf', 'docx', 'odt') or type(offset) is not int or not 0 <= offset <= 10 << 20
            or not isinstance(data, str) or len(data) > 4 * -(-(256 << 10) // 3)):
        raise OpError('invalid reference delivery')
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError:
        raise OpError('invalid reference encoding') from None
    if offset + len(raw) > 10 << 20:
        raise OpError('reference exceeds 10 MiB')
    # The broker recorded this binding at launch, never a path supplied by an agent.
    try:
        parent = _walk_dirs([*config.run_dir.absolute().parts[1:], session])
        try:
            binding = os.open('private-workspace.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
    except OSError:
        raise OpError('session workspace binding unavailable; start the helper again') from None
    try:
        info = os.fstat(binding)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            raise OpError('unsafe session workspace binding')
        document=json.loads(os.read(binding,8193))
        if (binding_key is None or not isinstance(document,dict) or document.get('session')!=session
                or document.get('v')!=1 or not isinstance(document.get('cwd'),str)):
            raise OpError('session workspace binding unavailable; restart the session')
        body={k:document.get(k) for k in ('v','session','cwd','device','inode')}
        expected=signature(body,binding_key)
        if (not isinstance(document.get('mac'),str) or not re.fullmatch('[0-9a-f]{64}',document['mac'])
                or not hmac.compare_digest(expected,document['mac'])):
            raise OpError('session workspace binding changed; restart the session')
        cwd = _under_roots(config, document['cwd'])
        if str(cwd)!=document['cwd']:
            raise OpError('session workspace path changed; start the helper again')
    finally:
        os.close(binding)
    # No symlinks at any component, including .acc, are followed.
    parent = _walk_dirs(list(cwd.absolute().parts[1:]))
    try:
        info=os.fstat(parent)
        if (info.st_dev,info.st_ino)!=(document.get('device'),document.get('inode')):
            raise OpError('session workspace replaced; start the helper again')
        if _hardened_git(cwd, 'check-ignore', '--quiet', '.acc/private-references/').returncode:
            raise OpError('private references require a git-ignored session worktree')
        for component in ('.acc', 'private-references', session):
            try:
                os.mkdir(component, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            if os.fstat(child).st_uid != os.geteuid():
                os.close(child)
                raise OpError('private reference folder is not owned by the session account')
            os.fchmod(child, 0o700)
            os.close(parent)
            parent = child
    except BaseException:
        os.close(parent)
        raise
    folder = output = None
    try:
        if not stat.S_ISDIR(os.fstat(parent).st_mode):
            raise OpError('missing session run directory')
        try:
            os.mkdir('brief-references', 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        folder = os.open('brief-references', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        name = reference + '.' + extension
        output = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=folder)
        info = os.fstat(output)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise OpError('unsafe reference destination')
        if info.st_size != offset:
            if info.st_size >= offset + len(raw) and os.pread(output, len(raw), offset) == raw:
                return {'path': str(PurePosixPath('.acc') / 'private-references' / session / 'brief-references' / name), 'worktree_relative': True, 'received': info.st_size}
            raise OpError('reference delivery offset changed')
        os.lseek(output, offset, os.SEEK_SET)
        remaining = memoryview(raw)
        while remaining:
            written = os.write(output, remaining)
            if written <= 0:
                raise OpError("reference write did not complete")
            remaining = remaining[written:]
        os.fsync(output)
        return {'path': str(PurePosixPath('.acc') / 'private-references' / session / 'brief-references' / name), 'worktree_relative': True, 'received': offset + len(raw)}
    finally:
        for fd in (output, folder, parent):
            if fd is not None:
                os.close(fd)
