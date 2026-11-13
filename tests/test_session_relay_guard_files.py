"""Production guard checks reject tampered and special files before admission."""
import os
from types import SimpleNamespace

import pytest

from acc.broker.claude_owner import ClaudeOwnerFactory
from acc.broker.relay_runtime import BrokerRuntime
from acc.session_relay import protocol as p


@pytest.mark.parametrize('owner', ['codex', 'claude'])
@pytest.mark.parametrize('replacement', ['original', 'content', 'writable', 'symlink', 'fifo', 'directory'])
def test_production_guard_revalidation_requires_frozen_regular_file(tmp_path, owner, replacement):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    path = tmp_path / 'guardrails.json'
    path.write_text('{}')
    path.chmod(0o400)
    config = SimpleNamespace(folder_policy_required=False, folder_revision=None, allowed_folders=True, allowed_roots=(workspace,))
    binding = SimpleNamespace(workspace=str(workspace))
    guards = {'guardrails.json': p.sha256_digest(b'{}')}
    if owner == 'codex':
        instance = object.__new__(BrokerRuntime)
        instance.factory = SimpleNamespace(config=config)
        instance.binding, instance.guards, instance.run_dir = binding, guards, tmp_path
    else:
        instance = object.__new__(ClaudeOwnerFactory)
        instance.lease = SimpleNamespace(validate=lambda: None)
        instance.transport = SimpleNamespace(config=config)
        instance.base = SimpleNamespace(binding=binding, spec={'run_dir': str(tmp_path)})
        instance.guards = guards
        instance.executable = tmp_path / 'fixture-executable'
        instance.executable.write_text('never executed')
        info = instance.executable.stat()
        instance.executable_identity = info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size
    if replacement != 'original':
        path.unlink()
        if replacement == 'content':
            path.write_text('{"tampered":true}')
            path.chmod(0o400)
        elif replacement == 'writable':
            path.write_text('{}')
        elif replacement == 'symlink':
            target = tmp_path / 'other'
            target.write_text('{}')
            target.chmod(0o400)
            path.symlink_to(target)
        elif replacement == 'fifo':
            os.mkfifo(path, 0o400)
        else:
            path.mkdir(mode=0o500)
        with pytest.raises((p.RelayError, OSError)):
            instance.guard_check()
    else:
        instance.guard_check()
