from __future__ import annotations

import json
from pathlib import Path

import pytest

from acc.server.compiler import CompileError, LaunchInputs, build_args, compile_files

HOOK, STATUS = "/srv/acc/bin/acc-hook", "/srv/acc/bin/acc-statusline"
SID = "11111111-2222-3333-4444-555555555555"


def test_claude_launch_and_resume_share_settings():
    inputs = LaunchInputs(provider="claude", model="opus", remote_control=True, initial_prompt="hi")
    first = build_args(inputs, run_dir="/r/x", tmux_name="acc-a-x", native_id=SID, resume=False)
    again = build_args(inputs, run_dir="/r/x", tmux_name="acc-a-x", native_id=SID, resume=True)
    assert first == ["--settings", "/r/x/settings.json", "--session-id", SID, "--permission-mode", "acceptEdits",
                     "--model", "opus", "--remote-control", "acc-a-x", "hi"]
    assert again == ["--settings", "/r/x/settings.json", "--resume", SID, "--permission-mode", "acceptEdits",
                     "--model", "opus", "--remote-control", "acc-a-x"]  # no initial prompt on resume


def test_claude_settings_only_contain_acc_hooks():
    files = compile_files(LaunchInputs(provider="claude"), HOOK, STATUS).files
    settings = json.loads(files["settings.json"])
    commands = {h["command"] for groups in settings["hooks"].values() for g in groups for h in g["hooks"]}
    guard = str(Path(HOOK).with_name("acc-guard"))
    assert commands == {f"{HOOK} claude", f"{guard} claude PreToolUse"}  # the pre-tool hook is the guardrails
    assert settings["statusLine"]["command"] == STATUS


def test_codex_args():
    inputs = LaunchInputs(provider="codex", model="gpt-6.1-sol", effort="high")
    assert build_args(inputs, run_dir="/r", tmux_name="t", native_id=None, resume=False) == [
        "--sandbox", "workspace-write", "--ask-for-approval", "on-request",
        "-c", "check_for_update_on_startup=false",  # ACC owns Codex updates; its prompt would eat the first Enter
        "--model", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"',
        "-c", "features.hooks=true"]  # last: a repository's .codex/config.toml can't turn the guard off (CT-01)
    assert build_args(inputs, run_dir="/r", tmux_name="t", native_id=SID, resume=True)[:2] == ["resume", SID]
    with pytest.raises(CompileError):
        build_args(inputs, run_dir="/r", tmux_name="t", native_id=None, resume=True)


def test_digest_is_stable_and_input_sensitive():
    a = compile_files(LaunchInputs(provider="claude"), HOOK, STATUS).digest
    assert a == compile_files(LaunchInputs(provider="claude"), HOOK, STATUS).digest
    assert a != compile_files(LaunchInputs(provider="claude", model="opus"), HOOK, STATUS).digest


@pytest.mark.parametrize("inputs", [
    LaunchInputs(provider="claude", permission_mode="yolo"),
    LaunchInputs(provider="codex", sandbox="none"),
    LaunchInputs(provider="codex", remote_control=True),
    LaunchInputs(provider="bash"),  # not a provider
])
def test_invalid_inputs_rejected(inputs):
    with pytest.raises(CompileError):
        compile_files(inputs, HOOK, STATUS)


@pytest.mark.parametrize("provider,expected", [
    ("claude", ["fix it"]), ("codex", ["fix it"]), ("cursor", ["fix it"]),
    ("copilot", ["-i", "fix it"]), ("antigravity", ["-i", "fix it"]),  # these two reject a bare prompt argument
])
def test_first_prompt_in_each_clis_form(provider, expected):
    inputs = LaunchInputs(provider=provider, initial_prompt="fix it")
    args = build_args(inputs, run_dir="/r", tmux_name="t", native_id="n", resume=False)
    assert args[-len(expected):] == expected
    assert "fix it" not in build_args(inputs, run_dir="/r", tmux_name="t", native_id="n", resume=True)  # never on resume


def test_confirmed_bypass_launch_skips_claudes_own_dialog():
    bypass = json.loads(compile_files(LaunchInputs(provider="claude", permission_mode="bypassPermissions"), HOOK, STATUS).files["settings.json"])
    assert bypass["skipDangerousModePermissionPrompt"] is True
    normal = json.loads(compile_files(LaunchInputs(provider="claude"), HOOK, STATUS).files["settings.json"])
    assert "skipDangerousModePermissionPrompt" not in normal


def test_ct01_codex_hooks_cannot_be_disabled_by_repository_config():
    # Real Codex 0.163/0.164 honor [features] hooks=false (or codex_hooks=false) from an untrusted project's
    # .codex/config.toml, which removes the guard. Only a command-line override outranks that layer.
    files = {"codex-overrides.json": json.dumps(["features.hooks=false"])}
    for resume in (False, True):
        args = build_args(LaunchInputs(provider="codex"), run_dir="/r", tmux_name="t", native_id=SID, resume=resume,
                          files=files)
        assert args[-2:] == ["-c", "features.hooks=true"]  # last: no ACC or catalog override can follow it
