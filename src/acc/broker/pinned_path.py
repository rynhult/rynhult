"""Request-lifetime directory handles for reviewed folder-policy operations.

Keep logical names for display/Git metadata, but never reopen those names for IO.
Linux's parent-process fd namespace lets subprocesses use the same pinned inode
without inheriting a request-selected descriptor or changing process-wide cwd.
"""
import os
import stat
from pathlib import Path

from acc import folder_policy
from acc.folder_policy import FolderError, inode, open_directory, reject_nested_mounts


class Handles:
    def __init__(self, config, path, fd):
        self.config, self.path, self.fd = config, Path(path), fd
        self.fds = [fd]
        self.others = []

    def __del__(self):
        for fd in self.fds:
            os.close(fd)

    def checked(self, fd):
        location = os.readlink(f'/proc/{os.getpid()}/fd/{fd}')
        if location.endswith(' (deleted)'):
            raise FolderError('The checked folder was removed. Review again.')
        folder_policy.broker_path(self.config, location)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            reject_nested_mounts(fd)
        actual = Path(location)
        if self.config.allowed_folders and not any(actual == root or root in actual.parents for root in self.config.allowed_roots):
            raise FolderError('The checked folder moved outside the allowed roots. Review again.')
        return f'/proc/{os.getpid()}/fd/{fd}'


class PinnedPath(type(Path())):
    def with_segments(self, *segments):
        result = type(self)(*segments)
        result.handles = self.handles
        return result

    def resolve(self, strict=False):
        # Initial acquisition was a no-follow absolute walk. Resolving the
        # display name again would replace the pinned authority with a pathname.
        return self

    def open(self, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        flags = os.O_RDWR if '+' in mode else os.O_WRONLY if any(c in mode for c in 'wax') else os.O_RDONLY
        if 'w' in mode:
            flags |= os.O_CREAT | os.O_TRUNC
        if 'a' in mode:
            flags |= os.O_CREAT | os.O_APPEND
        if 'x' in mode:
            flags |= os.O_CREAT | os.O_EXCL
        fd = self.open_fd(flags)
        try:
            return os.fdopen(fd, mode, buffering, encoding, errors, newline)
        except BaseException:
            os.close(fd)
            raise

    def open_fd(self, flags, mode=0o666):
        path = os.fspath(self)
        if path.rpartition('/')[0] == f'/proc/{os.getpid()}/fd':
            flags &= ~os.O_NOFOLLOW  # only this process's held, checked inode
        else:
            flags |= os.O_NOFOLLOW  # a not-yet-created leaf in a held parent
        return os.open(path, flags, mode)

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        try:
            os.mkdir(os.fspath(self), mode)
        except FileNotFoundError:
            if not parents:
                raise
            self.parent.mkdir(parents=True, exist_ok=True)
            self.mkdir(mode, exist_ok=exist_ok)
        except FileExistsError:
            if not exist_ok or not self.is_dir():
                raise

    def unlink(self, missing_ok=False):
        fd = self.parent.directory_fd()
        try:
            try:
                os.unlink(self.name, dir_fd=fd)
            except FileNotFoundError:
                if not missing_ok:
                    raise
        finally:
            os.close(fd)

    def stat(self, *, follow_symlinks=True):
        # fspath returns only a held inode or a non-existent creation slot.
        # lstat must inspect that inode, not the procfs link used to reach it.
        return os.stat(os.fspath(self))

    def directory_fd(self):
        return os.open(os.fspath(self), os.O_RDONLY | os.O_DIRECTORY)

    def logical(self):
        # Persistent trust/settings store a logical path, never an ephemeral fd.
        # Refuse if that name no longer identifies the held directory.
        fd = self.directory_fd()
        try:
            with open_directory(str(self), expected=inode(fd), same_mount=True):
                self.handles.checked(fd)
        finally:
            os.close(fd)
        return str(self)

    def __fspath__(self):
        handles = self.handles
        handles.checked(handles.fd)
        try:
            parts = self.relative_to(handles.path).parts
        except ValueError:
            # Sibling Git metadata (a worktree's purpose marker, for example)
            # needs its own full mandatory/root validation and held descriptor.
            other = pin(handles.config, str(self.parent))
            handles.others.append(other)
            return os.fspath(other / self.name)
        if not parts:
            return handles.checked(handles.fd)
        parent = '/' + '/'.join(parts[:-1]) if len(parts) > 1 else None
        if parent:
            with open_directory(parent, root_fd=handles.fd, search_only=True, same_mount=True) as opened:
                parent_fd = os.dup(opened.fd)
                handles.fds.append(parent_fd)
        else:
            parent_fd = handles.fd
        parent_path = handles.checked(parent_fd)
        try:
            fd = os.open(parts[-1], os.O_PATH | os.O_NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError:
            # Creation is relative to a held parent. Existing symlinks are never
            # followed; mkdir/open callers retain their normal exclusive checks.
            return parent_path + '/' + parts[-1]
        handles.fds.append(fd)
        if stat.S_ISLNK(os.fstat(fd).st_mode) or inode(fd)[2] != inode(parent_fd)[2]:
            raise FolderError('Links and mount crossings cannot replace a checked target.')
        return handles.checked(fd)


def pin(config, path):
    folder_policy.broker_path(config, path)
    with open_directory(path, same_mount=True) as directory:
        fd = os.dup(directory.fd)
    result = PinnedPath(path)
    result.handles = Handles(config, path, fd)
    result.handles.checked(fd)
    return result
