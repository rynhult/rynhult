"""Permission regressions must execute without root's DAC bypass, even in root-run merge gates."""
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from acc.broker import ops
from acc.broker.config import BrokerConfig


def broker_config(base, **overrides):
    values = dict(socket_path=base / 'broker.sock', run_dir=base / 'run', allowed_roots=(base,),
                  tmux_socket='acc-test-file-probe', providers={}, hook_path=str(base / 'bin/acc-hook'),
                  catalog_dir=base / 'catalog', catalog_local_dir=base / 'catalog-local',
                  containers_socket=base / 'containers.sock', containers_attach_root=base / 'containers-attach')
    return BrokerConfig(**(values | overrides))


def _materialize_run(base):
    run = base / "acct" / "abcd1234"
    ops._materialize_run(run, {"settings.json": "{}", "nested/input.json": "first"}, {"DEMO": "hello world"})
    assert (run / "settings.json").read_text() == "{}"
    assert stat.S_IMODE((run / "settings.json").stat().st_mode) == 0o440
    assert (run / "nested/input.json").read_text() == "first"
    assert (run / "session.env").read_text() == "export DEMO='hello world'\n"
    assert stat.S_IMODE((run / "session.env").stat().st_mode) == 0o600
    assert (run / "events.jsonl").read_bytes() == b""
    config = broker_config(base, run_dir=run.parent)
    assert ops.read_spool(config, run.name, 0) == {"data": ""}
    assert ops.team_reply(config, run.name, "a" * 32, {"ok": True}) == {"written": True}
    ops._materialize_run(run, {"settings.json": '{"resumed":true}', "nested/input.json": "second"}, {})
    assert (run / "settings.json").read_text() == '{"resumed":true}'
    assert (run / "nested/input.json").read_text() == "second"
    assert not (run / "session.env").exists()


def _workspace_files(base):
    root = base / "acct" / "project"
    root.mkdir()
    (root / "note.md").write_text("before")
    config = broker_config(base, allowed_roots=(base / "acct",), run_dir=base / "run")
    assert ops.files(config, str(root))["entries"][0]["name"] == "note.md"
    opened = ops.files(config, str(root), action="read", rel="note.md")
    assert opened["content"] == "before"
    ops.files(config, str(root), action="write", rel="note.md", content="after", revision=opened["revision"])
    assert (root / "note.md").read_text() == "after"


def _exclude_artifacts(base):
    root = base / "acct" / ".worktrees" / "session"
    admin = root / ".git"
    admin.mkdir(parents=True)
    ops.exclude_artifacts(root)
    assert ".acc/" in (admin / "info/exclude").read_text().splitlines()


def _exercise(case, scratch):
    assert os.geteuid() != 0, "permission regression must not execute as root"
    base = scratch / "traverse-only"
    (base / "acct").mkdir(parents=True)
    base.chmod(0o311)
    try:
        assert base.stat().st_uid == os.geteuid()
        assert not os.access(base, os.R_OK), "test parent must really deny directory reads"
        globals()["_" + case](base)
    finally:
        base.chmod(0o700)


@pytest.mark.parametrize("case", ["materialize_run", "workspace_files", "exclude_artifacts"])
def test_launch_paths_as_agent(case):
    if not sys.platform.startswith("linux"):
        pytest.skip("requires Linux O_PATH traversal semantics")
    command = []
    account = None
    if os.geteuid() == 0:
        try:
            account = pwd.getpwnam("nobody")
        except KeyError:
            pytest.skip("root needs an existing unprivileged nobody account")
        if account.pw_uid == 0:
            pytest.skip("nobody must be an unprivileged account")
        if tool := shutil.which("runuser"):
            command = [tool, "-u", account.pw_name, "--"]
        elif tool := shutil.which("setpriv"):
            command = [tool, f"--reuid={account.pw_uid}", f"--regid={account.pw_gid}", "--clear-groups"]
        else:
            pytest.skip("root needs runuser or setpriv to drop privileges")
        probe = subprocess.run(
            [*command, sys.executable, "-c", "import os; assert os.geteuid() != 0"],
            capture_output=True, text=True, cwd="/tmp", timeout=30,
        )
        if probe.returncode:
            pytest.skip(f"cannot re-execute Python as nobody: {probe.stderr.strip()}")
    with tempfile.TemporaryDirectory(prefix="acc-launch-agent-", dir="/tmp") as temporary:
        scratch = Path(temporary)
        if account is None:
            _exercise(case, scratch)
            return
        # Root's checkout (and pytest's temporary parents) may be unreadable to nobody.
        # Copy this checkout's source, never the interpreter's other editable ACC install.
        shutil.copytree(Path(__file__).resolve().parents[1] / "src/acc", scratch / "src/acc",
                        ignore=shutil.ignore_patterns("__pycache__"))
        script = scratch / Path(__file__).name
        shutil.copyfile(__file__, script)
        for path in [scratch, *scratch.rglob("*")]:
            os.chown(path, account.pw_uid, account.pw_gid)
        result = subprocess.run(
            [*command, sys.executable, str(script), case, str(scratch)],
            env={**os.environ, "PYTHONPATH": str(scratch / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
            cwd=scratch, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    _exercise(sys.argv[1], Path(sys.argv[2]))
