"""Linux-only fixed root operations. Called only by the root-owned folder worker.

No project executable, shell, caller-supplied command, UID or unit is accepted.
Unprivileged tests substitute the OS boundary; real UID qualification is separate.
"""
from __future__ import annotations

import grp
import os
import re
import select
import signal
import stat
import subprocess
from contextlib import contextmanager

from .folder_policy import FolderError, open_directory

ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'TZ': 'UTC'}


class LinuxFolderHost:
    def __init__(self, *, host_root='/', reack=None):
        self.host_root, self.reack = str(host_root), reack

    @staticmethod
    def _run(argv):
        try:
            result = subprocess.run(argv, shell=False, env=ENV, stdin=subprocess.DEVNULL,
                capture_output=True, timeout=8, check=True)
            if len(result.stdout) > 4096:
                raise FolderError('The local service response exceeded its limit.')
            return result.stdout.decode('utf-8', errors='strict').strip()
        except (OSError, UnicodeError, subprocess.SubprocessError):
            raise FolderError('The local account or purpose-group operation failed. Review local setup before retrying.') from None

    @staticmethod
    def _groups():
        return [(g.gr_name, g.gr_gid, list(g.gr_mem)) for g in grp.getgrall()]

    @staticmethod
    def _names(account):
        names = [account.get('owner_name'), account.get('agent_name')]
        if (any(not isinstance(n, str) or not re.fullmatch('[a-z_][a-z0-9_-]{0,31}', n) for n in names)
                or any(n in ('root', 'acc', 'acc-node') for n in names) or names[0] == names[1]):
            raise FolderError('Provision two distinct unprivileged human and agent accounts.')
        return names

    def plan_group(self, operation, account):
        self._names(account)
        if not re.fullmatch('[0-9a-f]{32}', operation):
            raise FolderError('Invalid purpose-group operation.')
        name = 'ryn-repo-' + operation[:16]
        groups = self._groups()
        if any(n == name for n, _, _ in groups):
            raise FolderError('The purpose group already exists. Review a fresh grant.')
        used = {gid for _, gid, _ in groups}
        gid = next((i for i in range(60000, 65000) if i not in used), None)
        if gid is None:
            raise FolderError('No purpose-group ID is available. Ask the host administrator.')
        return {'name': name, 'gid': gid}

    def create_group(self, group, account, save):
        names = self._names(account)
        if (not re.fullmatch('ryn-repo-[0-9a-f]{16}', group.get('name', ''))
                or type(group.get('gid')) is not int or not 60000 <= group['gid'] < 65000
                or any(n == group['name'] or gid == group['gid'] for n, gid, _ in self._groups())):
            raise FolderError('The reviewed purpose-group name or ID is now in use.')
        group['creating'] = True
        save()  # a crash here is uncertain: never adopt/delete a pre-existing group
        self._run(['/usr/sbin/groupadd', '--gid', str(group['gid']), '--', group['name']])
        group['created'], group['creating'], group['members_added'] = True, False, []
        save()
        for name in names:
            group['membership_pending'] = name
            save()
            self._run(['/usr/sbin/usermod', '--append', '--groups', group['name'], '--', name])
            group['members_added'].append(name)
            group['membership_pending'] = None
            save()

    def remove_group_members(self, group, account, save):
        names = self._names(account)
        if not group.get('created') or group.get('creating') or group.get('membership_pending'):
            raise FolderError('Unconfirmed group changes need administrator review; no existing membership was removed.')
        current = next((g for g in self._groups() if g[0] == group['name'] and g[1] == group['gid']), None)
        if current is None or any(n not in names for n in current[2]):
            raise FolderError('Purpose-group membership changed. Ask the administrator to review it.')
        for name in list(group['members_added']):
            if name not in names:
                raise FolderError('The recorded membership does not match the assigned accounts.')
            if name in current[2]:
                self._run(['/usr/bin/gpasswd', '--delete', name, group['name']])
            group['members_added'].remove(name)
            save()
        # An external writer may have assigned this gid elsewhere. No global
        # filesystem crawl, deletion of an unknown inode, or guessed groupdel.
        return 'Memberships removed. Empty purpose group retained until the administrator confirms no other inode depends on it.'

    @staticmethod
    def _processes(uid):
        found = set()
        try:
            for name in os.listdir('/proc'):
                if not name.isdecimal():
                    continue
                try:
                    if os.stat('/proc/' + name, follow_symlinks=False).st_uid == uid:
                        found.add(int(name))
                except FileNotFoundError:
                    continue
        except OSError:
            raise FolderError('Account processes cannot be confirmed stopped.') from None
        return found

    @contextmanager
    def stopped(self, account):
        name = account.get('agent_name', '')
        if not re.fullmatch('[a-z_][a-z0-9_-]{0,31}', name):
            raise FolderError('The agent service mapping is missing.')
        unit, socket = account.get('broker_unit'), account.get('broker_socket_unit')
        if unit != f'acc-broker@{name}.service' or socket != f'acc-broker@{name}.socket':
            raise FolderError('The fixed account service mapping has not been qualified.')
        value = self._run(['/usr/bin/systemctl', 'show', '--property=MainPID', '--value', unit])
        if not value.isdecimal():
            raise FolderError('The account service process could not be verified.')
        main = int(value)
        if self._processes(account['uid']) - {main}:
            raise FolderError('Account processes are still running. Stop them before granting or undoing access.')
        try:
            self._run(['/usr/bin/systemctl', 'stop', socket, unit])
            if self._processes(account['uid']):
                raise FolderError('Account processes remain after stopping the service. No access was changed.')
            yield
        finally:
            self._run(['/usr/bin/systemctl', 'start', socket, unit])
            if self.reack is not None:
                self.reack(account)

    def verify(self, path, records, account, write, group=None):
        """Fork fixed built-in metadata/open checks as the target UID, no repo code.

        Open read/write descriptors without reading or writing contents. Restrictive
        new modes can later change access; this reports only the observed manifest.
        """
        import pwd
        uid = account['uid']
        agent = pwd.getpwuid(uid)
        groups = os.getgrouplist(agent.pw_name, agent.pw_gid)
        if group and group['gid'] not in groups:
            raise FolderError('The new purpose-group membership did not become effective.')
        return self.verify_identity(path, records, uid, agent.pw_gid, groups, write)

    def verify_identity(self, path, records, uid, gid, groups, write):
        read, send = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read)
            try:
                os.setgroups(groups)
                os.setgid(gid)
                os.setuid(uid)
                with open_directory(self.host_root, search_only=True) as host:
                    for record in records:
                        full = path + ('/' + record['path'] if record['path'] else '')
                        with open_directory(full.rsplit('/', 1)[0], root_fd=host.fd, search_only=True) as parent:
                            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                            fd = os.open(full.rsplit('/', 1)[1], flags, dir_fd=parent.fd)
                            try:
                                st = os.fstat(fd)
                                if [st.st_dev, st.st_ino] != record['before']['identity']:
                                    raise ValueError('Identity changed')
                                pinned = f'/proc/self/fd/{fd}'
                                required = os.R_OK | (os.X_OK if stat.S_ISDIR(st.st_mode) or record['before'].get('mode', 0) & 0o111 else 0)
                                if not os.access(pinned, required, effective_ids=True):
                                    raise PermissionError
                                if os.access(pinned, os.W_OK, effective_ids=True) is not write:
                                    raise PermissionError
                                if write and stat.S_ISREG(st.st_mode):
                                    # Reopen the held inode, never a mutable leaf name.
                                    writable = os.open(pinned, os.O_WRONLY | os.O_NONBLOCK)
                                    try:
                                        opened = os.fstat(writable)
                                        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                                            raise PermissionError
                                    finally:
                                        os.close(writable)
                            finally:
                                os.close(fd)
                os.write(send, b'1')
            except BaseException:
                os.write(send, b'0')
            os._exit(0)
        os.close(send)
        try:
            ready, _, _ = select.select([read], [], [], 10)
            if not ready:
                os.kill(pid, signal.SIGKILL)  # this worker's own verification child only
                raise FolderError('Agent access verification exceeded its time limit.')
            passed = os.read(read, 2) == b'1'
        finally:
            os.close(read)
            _, code = os.waitpid(pid, 0)
        if not passed or code != 0:
            raise FolderError('Agent access verification failed. Review partial access and Undo.')
        return {'read': True, 'write': write, 'checked': len(records)}
