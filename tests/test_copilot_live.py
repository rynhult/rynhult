"""The real Copilot CLI against a scripted model (tests/fake_llm.py) through its bring-your-own-model settings, so no
GitHub account is used: run it before moving the Copilot release line, with the candidate binary:

    ACC_COPILOT_BIN=/path/to/copilot PYTHONPATH=src python -m pytest -q tests/test_copilot_live.py

Skipped without ACC_COPILOT_BIN. It uses its own HOME, so neither the account's Copilot sign-in nor the user-level hooks
in ~/.copilot/hooks (which call the installed acc-hook) are loaded. Checked with 1.0.103 on 2026-10-25.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest
from test_opencode_live import model  # noqa: F401  (the scripted model fixture)

BINARY = os.environ.get("ACC_COPILOT_BIN")
pytestmark = pytest.mark.skipif(not BINARY, reason="set ACC_COPILOT_BIN to a real Copilot CLI binary")


def bash(command: str) -> dict:
    return {"tool": "bash", "args": {"command": command, "description": "x", "sessionId": "probe", "mode": "sync",
                                     "initial_wait": 10}}


def test_shell_commands_run_without_alias_expansion(tmp_path, model):  # noqa: F811
    """The guard checks one tool call at a time. That is complete only while the shell neither keeps aliases from an
    earlier call nor expands them in the same one. acc-guard follows aliases defined in a call anyway (security review of
    guard-dynamic-command-reason, GR-7). Checked with 1.0.103: each call is a new `/bin/bash --norc --noprofile -c` with
    expand_aliases off, even in the same sessionId."""
    url = model([bash("alias accprobe='echo EXPANDED'; echo defined > defined.txt"),
                 bash("alias accsame='echo EXPANDED'\naccsame same > same.txt 2>&1"),
                 bash("accprobe later > later.txt 2>&1; echo \"$- $(shopt expand_aliases)\" > flags.txt"),
                 {"text": "done"}])
    workspace, home = tmp_path / "ws", tmp_path / "home"
    workspace.mkdir()
    (home / ".copilot").mkdir(parents=True)
    (home / ".copilot/config.json").write_text(json.dumps({"trustedFolders": [str(workspace)]}))
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "TERM": "xterm", "COPILOT_PROVIDER_BASE_URL": url,
           "COPILOT_PROVIDER_TYPE": "openai", "COPILOT_MODEL": "m"}
    result = subprocess.run([BINARY, "-p", "go", "--allow-all-tools", "--no-auto-update", "--model", "m"], cwd=workspace,
                            env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (workspace / "defined.txt").read_text() == "defined\n"  # the alias was defined
    flags, expand = (workspace / "flags.txt").read_text().split(None, 1)
    assert "i" not in flags and expand.split() == ["expand_aliases", "off"]
    assert "EXPANDED" not in (workspace / "same.txt").read_text()
    assert "EXPANDED" not in (workspace / "later.txt").read_text()
