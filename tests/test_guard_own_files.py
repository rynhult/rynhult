"""Guardrails: agents may clean up their own temporary files, and only their own (Casey, 2026-10-22).

A recursive delete outside the workspace is allowed in a temp folder when everything it would remove belongs to the
agent's account. A variable assigned earlier in the same shell counts as its value, scoped like the shell scopes it
(a subshell, pipeline member, background job or new shell doesn't pass it back); $(mktemp ...) is a fresh temp of the
agent's own.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "bin" / "acc-guard"
loader = importlib.machinery.SourceFileLoader("acc_guard_own", str(GUARD))
spec = importlib.util.spec_from_loader("acc_guard_own", loader)
guard = importlib.util.module_from_spec(spec)
loader.exec_module(guard)

POLICY = {"workspace": "/srv/ws", "rules": {"rm_outside": True}, "custom": []}


def verdict(command: str):
    found = guard.check(POLICY, "Bash", {"command": command}, "/srv/ws")
    return found[0] if found else None


@pytest.fixture
def scratch(tmp_path_factory):
    folder = Path(guard.TEMP_ROOTS[0]) / f"acc-guard-test-{os.getpid()}"
    folder.mkdir(exist_ok=True)
    (folder / "sub").mkdir(exist_ok=True)
    (folder / "sub" / "file").write_text("x")
    yield str(folder)
    import shutil
    shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.parametrize("command", [
    "S={s}; rm -rf $S",
    "S={s}; rm -rf \"$S\"",
    "S={s}; rm -rf ${{S}}/sub",
    "S={s}; P=$S/sub; rm -rf $P",
    "S={s} && cd /srv/ws && rm -rf $S/*",
    "rm -rf {s}",
    "S=$(mktemp -d); trap 'true' EXIT; rm -rf $S",
    "S=$(mktemp -d -t acc.XXXXXX); rm -rf \"$S\"",
])
def test_own_temp_files_may_be_deleted(scratch, command):
    assert verdict(command.format(s=scratch)) is None


@pytest.mark.parametrize("command", [
    "(S={s}); rm -rf $S/*",                    # set in a subshell: empty here, so /*
    "S={s} | true; rm -rf $S/*",               # a pipeline member is a subshell
    "S={s} & rm -rf $S/*",                     # so is a background job
    "S={s}; sh -c 'rm -rf $S/*'",              # a new shell doesn't see it (not exported)
    "S={s}; unset S; rm -rf $S/*",
    "S={s}; read S; rm -rf $S/*",
    "S={s}; for S in a; do :; done; rm -rf $S/*",
    "S='/tmp/a b'; rm -rf $S",                 # word splitting: not one path
    "S=/tmp/*; rm -rf $S",                     # glob characters
    "S=$(pwd); rm -rf $S",                     # any other substitution is unknown
    "S=$(mktemp -p /home/x); rm -rf $S",
    "rm -rf ${{S:-/}}",
    "rm -rf $1",
    "S={s}; rm -rf $S/../..",                  # resolves to /
    "rm -rf /tmp",
    "rm -rf /tmp/*",                           # other people's temp files too
    "S=$(mktemp -d); rm -rf $S/../*",          # the rest of /tmp
])
def test_anything_else_stays_blocked(scratch, command):
    assert verdict(command.format(s=scratch)) == "rm_outside"


@pytest.mark.skipif(os.getuid() != 0, reason="needs root to give a file to another account")
def test_someone_elses_file_in_a_temp_folder_is_not_ours(scratch):
    os.chown(Path(scratch) / "sub" / "file", 65534, 65534)
    assert verdict(f"rm -rf {scratch}") == "rm_outside"
    assert verdict(f"S={scratch}; rm -rf $S/sub") == "rm_outside"
