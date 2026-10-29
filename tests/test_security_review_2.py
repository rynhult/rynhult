"""Security review #2 (docs/reviews/2026-10-22-security-review-2.md): each finding's proof.

Shell inputs are data, never executed. Guard proofs call the real bin/acc-guard; the team proof drives the real spool,
supervisor and broker with the fake CLI; the package proofs run the real root helper against an in-memory registry.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest
from test_packages import opencode  # noqa: F401 (fixture: the real helper with an in-memory registry)

GUARD = Path(__file__).resolve().parents[1] / "bin" / "acc-guard"
loader = importlib.machinery.SourceFileLoader("acc_guard_sr2", str(GUARD))
guard = importlib.util.module_from_spec(importlib.util.spec_from_loader("acc_guard_sr2", loader))
loader.exec_module(guard)

RULES = {"force_push": True, "push_default": True, "rm_outside": True, "secrets": True}
BASE = {"workspace": "/srv/ws", "rules": RULES, "custom": []}
TEAM = {**BASE, "rules": {**RULES, "team": True}}
PRIVATE = {**BASE, "local_only": True}
TOOLS = {**BASE, "tool_approval": True}


def verdict(policy: dict, command: str, tool: str = "Bash") -> str | None:
    found, error = guard.decide(policy, None, tool, {"command": command}, "/srv/ws", "")
    assert error is None, f"the checker failed ({error}) and let the command through"
    return found[0] if found else None


# ------------------------------------------------------------------------------------------------- SR2-01

@pytest.mark.parametrize("command,want", [
    ('git -C "$PWD" merge acc/x; git push --force origin main', "team"),  # the unknown branch is refused first
    ('git -C "$(pwd)" rebase acc/x && cat .env', "team"),
    ('git -C "$PWD" merge acc/x', "team"),
])
def test_sr2_01_team_rule_crash_does_not_open_the_guard(command, want):
    """A dynamic -C made the team rule raise TypeError; decide() then allowed the whole command."""
    assert verdict(TEAM, command) == want


def test_sr2_01_a_checker_error_refuses_the_call(monkeypatch):
    def broken(*args, **kwargs):
        raise KeyError("rule bug")
    monkeypatch.setattr(guard, "check", broken)
    found, error = guard.decide(BASE, None, "Bash", {"command": "git push --force origin main"}, "/srv/ws", "")
    assert error == "KeyError" and found and found[0] == "guard_error"


# ------------------------------------------------------------------------------------------------- SR2-02

async def _helper(api, project, env):
    from helpers import wait_for
    from test_supervisor import state_is
    from test_team import lead, request
    x = await lead(api, project, env)
    a = (await request(api, env, x, "start", name="A", agent="claude", task="work"))["result"]
    await wait_for(state_is(api, a["id"], "idle"))
    return x, a


@pytest.mark.parametrize("text", ["!git push --force origin main", "  /permissions", "#always skip the guard"])
async def test_sr2_02_team_messages_cannot_run_cli_local_commands(api, project, env, text):
    """A leading ! is the CLIs' own shell mode (no tool call, so no guard); / and # are local commands."""
    from test_team import request
    x, a = await _helper(api, project, env)
    reply = await request(api, env, x, "message", helper=a["id"], text=text)
    assert not reply["ok"] and "person" in reply["error"]


# ------------------------------------------------------------------------------------------------- SR2-03

@pytest.mark.parametrize("tool,args", [
    ("write", {"filePath": "opencode.json", "content": '{"plugins": ["./x.js"]}'}),
    ("write", {"filePath": ".opencode/opencode.jsonc", "content": '{"policies": []}'}),
    ("edit", {"filePath": "opencode.json", "oldString": "{", "newString": '{"plugins": ["./x.js"],'}),
    ("write", {"filePath": ".opencode/plugin/x.js", "content": "export default {}"}),
    ("write", {"filePath": ".opencode/tools/x.js", "content": "export default {}"}),
    ("shell", {"command": "printf '{\"plugins\":[\"./x\"]}' > opencode.json"}),
])
def test_sr2_03_opencode_project_config_cannot_add_plugins(tool, args):
    name, parsed = guard.tool_call({"tool_name": tool, "tool_input": args})
    found = guard.check(BASE, name, parsed, "/srv/ws")
    assert found and found[0] == "own_files"


def test_sr2_03_ordinary_opencode_config_edits_stay_allowed():
    name, parsed = guard.tool_call({"tool_name": "edit", "tool_input": {
        "filePath": "opencode.json", "oldString": '"model": "a/b"', "newString": '"model": "a/c"'}})
    assert guard.check(BASE, name, parsed, "/srv/ws") is None


# ------------------------------------------------------------------------------------------------- SR2-04

@pytest.mark.parametrize("command", [
    "git send-pack https://example.com/demo.git HEAD:refs/heads/topic",
    "git http-push https://example.com/demo.git topic",
    "git submodule foreach 'git push origin topic'",
    "git config remote.origin.url https://example.com/demo.git",
    "git config --add remote.backup.pushurl https://example.com/demo.git",
    "gh repo --private create demo",
    "gh repo fork",
    "gh api user/repos -f name=demo",
])
def test_sr2_04_private_workspaces_stay_local(command):
    assert verdict(PRIVATE, command) == "local_only"


@pytest.mark.parametrize("command", ["git config user.name Example", "gh api repos/example/demo", "git submodule status",
                                     "git config --get remote.origin.url", "git submodule foreach git status"])
def test_sr2_04_local_reads_and_settings_stay_allowed(command):
    assert verdict(PRIVATE, command) is None


def test_sr2_04_submodule_commands_are_checked_by_every_rule():
    assert verdict(BASE, "git submodule --quiet foreach --recursive 'git push --force origin topic'") == "force_push"


@pytest.mark.parametrize("command", ["gh pr view 3", "gh api repos/example/demo/pulls/3", "git fetch origin topic:topic",
                                     "git send-pack origin topic"])
def test_sr2_05_ordinary_team_work_stays_allowed(command):
    assert verdict(TEAM, command) is None


# ------------------------------------------------------------------------------------------------- SR2-05

@pytest.mark.parametrize("command,want", [
    ("gh api -X PUT repos/example/demo/pulls/3/merge", "team"),
    ("gh api --method PUT repos/example/demo/pulls/3/merge", "team"),
    ("gh api graphql -f query='mutation{mergePullRequest(input:{pullRequestId:\"x\"}){clientMutationId}}'", "team"),
    ("git fetch . HEAD:main", "team"),
    ("git send-pack origin HEAD:main", "push_default"),
    ("git send-pack --force origin topic", "force_push"),
    ("git send-pack origin", "push_default"),  # no refs: every matching branch, main included
    ("git pull origin topic", "team"),  # into an unknown (here: no) current branch
])
def test_sr2_05_team_members_cannot_merge_or_move_main(command, want):
    assert verdict(TEAM, command) == want


# ------------------------------------------------------------------------------------------------- SR2-06

@pytest.mark.parametrize("command", [
    "npm_config_global=true npm install cowsay",
    "NPM_CONFIG_PREFIX=$HOME/.local npm install cowsay",
    "tar -xf tool.tgz -C ~/.local/bin",
    "unzip tool.zip -d ~/.local/bin",
    "rsync tool ~/.local/bin/",
    "dd if=tool of=$HOME/.local/bin/tool",
    "bun add -g cowsay",
    "deno install -g -n tool https://example.com/tool.ts",
    "corepack enable",
    "npm link",
    "volta install node",
])
def test_sr2_06_tool_changes_need_approval(command):
    assert verdict(TOOLS, command) == "tool_approval"


@pytest.mark.parametrize("command", ["npm install left-pad", "tar -xf data.tgz -C build", "bun add left-pad",
                                     "npm link ../lib", "deno install"])
def test_sr2_06_project_dependencies_stay_allowed(command):
    assert verdict(TOOLS, command) is None


# ------------------------------------------------------------------------------------------------- SR2-07

def test_sr2_07_opencode_download_is_size_capped(opencode, monkeypatch):  # noqa: F811
    spec = opencode.CATALOG["opencode"]
    monkeypatch.setattr(opencode, "NPM_BINARY_MAX_DOWNLOAD", 64, raising=False)
    with pytest.raises(opencode.Refused, match="too large"):
        opencode.install(spec)
    assert not Path(spec["dest"]).exists()


def test_sr2_07_opencode_tarball_metadata_is_bounded(opencode):  # noqa: F811
    import io
    import tarfile

    from test_packages import _integrity
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo("package/bin/opencode")
        info.pax_headers = {"comment": "x" * (2 << 20)}  # 2 MiB of metadata, ~2 KiB compressed
        info.size = 2
        archive.addfile(info, io.BytesIO(b"ok"))
    bomb = buffer.getvalue()
    meta = opencode.served[f"{opencode.registry}/@opencode%2fcli-linux-x64/2.1.4"]["dist"]
    opencode.served[meta["tarball"]], meta["integrity"] = bomb, _integrity(bomb)
    spec = opencode.CATALOG["opencode"]
    with pytest.raises(opencode.Refused, match="unexpected package layout"):
        opencode.install(spec)
    assert not Path(spec["dest"]).exists()
