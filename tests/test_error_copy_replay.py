"""Owner-facing error copy must not add local executable paths to a refusal."""
from types import SimpleNamespace

import pytest

from acc import backup_server, restic_process


def test_wrong_backup_tool_version_does_not_echo_its_executable_path(tmp_path, monkeypatch):
    private_binary = tmp_path / 'private-machine-canary' / 'restic'
    monkeypatch.setattr(restic_process, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=b'restic 0.1.0\n'))
    with pytest.raises(backup_server.Refused) as caught:
        backup_server.LocalTarget(tmp_path / 'repo', tmp_path / 'password', binary=private_binary)
    message = str(caught.value)
    assert 'private-machine-canary' not in message
    assert str(tmp_path) not in message
    assert backup_server.RESTIC_VERSION in message
    assert 'Install' in message
