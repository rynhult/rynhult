"""Authenticated original-binding evidence channel. No owner or input methods."""
import os
import pwd
import stat
from pathlib import Path

from acc.session_relay import protocol as p

from . import ops
from .claude_admission import _open_directory
from .claude_decision_journal import DecisionEvidenceJournal, reconcile


def native_root():
    # Same account-derived root as ClaudeOwnerFactory.environment; never accept
    # an arbitrary root/path serialized by main or the journal.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / '.claude' / 'projects'


def check_guards(directory, binding):
    guards = {}
    directory_fd = _open_directory(directory)
    try:
        for name in (*ops.GUARD_FILES, 'mcp.json'):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o222 or info.st_size > p.MAX_FRAME_BYTES):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                guards[name] = p.sha256_digest(os.read(fd, p.MAX_FRAME_BYTES + 1))
            finally:
                os.close(fd)
    finally:
        os.close(directory_fd)
    if 'guardrails.json' not in guards or p.sha256_digest(p.canonical_json(guards)) != binding.launch_policy_digest:
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)


class ReadOnlyEvidence:
    def __init__(self, factory, enrollment):
        self.factory, self.enrollment = factory, enrollment
        self.peer = self.journal = None
        self.scope = None

    def lost(self):
        pass  # No execution authority, lease or owner exists on this channel.

    async def handle(self, method, params):
        if method != 'reconcile' or params != {} or self.scope is not None:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        value = await self.peer.call('evidence.enroll', {'enrollment': self.enrollment})
        if value.keys() != {'binding','sessionId','runId','runDir'}:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        binding = p.Binding(**value['binding'])
        cfg = self.factory.config
        if (binding.provider != 'claude' or binding.account != pwd.getpwuid(os.getuid()).pw_name
                or not ops.ACC_ID.fullmatch(value['sessionId']) or not p.valid_opaque(value['runId'])
                or (cfg.folder_policy_required and not cfg.folder_revision)):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        ops._under_roots(cfg, binding.workspace)
        directory = cfg.run_dir / value['sessionId']
        if value['runDir'] != str(directory):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        # Frozen guard content is checked without loading credential env/settings
        # or constructing a ClaudeOwnerFactory/PrivateRuntime/writer.
        check_guards(directory, binding)
        self.scope = value['binding']
        root = directory / 'relay' / 'decision-evidence'
        if not root.exists():
            return {'reconciled': 0}
        self.journal = DecisionEvidenceJournal(root)
        async def publish(event):
            current = self.factory.config
            ops._under_roots(current, binding.workspace)
            if current.run_dir != cfg.run_dir or current.folder_revision != cfg.folder_revision:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            check_guards(directory, binding)
            if event['runId'] != value['runId']:
                return False
            return await self.peer.call('evidence.publish', event)
        count = await reconcile(self.journal, self.scope, native_root(), publish, run_id=value['runId'])
        return {'reconciled': count}

    async def close(self):
        if self.journal:
            self.journal.close()
