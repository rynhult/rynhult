"""Guardrails: bin/acc-guard (each CLI's pre-tool hook) and the owner's settings."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from helpers import control, wait_for
from test_supervisor import get, launch, state_is

GUARD = Path(__file__).resolve().parents[1] / "bin" / "acc-guard"
ALL = {"rules": {"force_push": True, "push_default": True, "rm_outside": True, "secrets": True, "merge_pr": True}, "custom": [],
       "workspace": "/srv/ws"}


def _module():
    loader = importlib.machinery.SourceFileLoader("acc_guard", str(GUARD))
    spec = importlib.util.spec_from_loader("acc_guard", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


guard = _module()


def test_synthetic_shell_words_keep_quote_metadata_for_tool_approval():
    policy = {**ALL, "tool_approval": True}
    assert guard.check(policy, "Bash", {"command": "su -c 'cat README.md'"}, "/srv/ws") is None
    assert guard.check(policy, "Bash", {"command": "su -c 'pip install --user demo'"}, "/srv/ws")[0] == "tool_approval"


def rule(command=None, path=None, policy=ALL, cwd="/srv/ws"):
    args = {"command": command} if command else {"file_path": path}
    verdict = guard.check(policy, "Bash" if command else "Read", args, cwd)
    return verdict[0] if verdict else None


@pytest.mark.parametrize("command,expected", [
    ("git push --force origin feature", "force_push"),
    ("git push -f", "force_push"),
    ("git push origin +feature", "force_push"),
    ("cd x && git push --force-with-lease", "force_push"),
    ("git push origin main", "push_default"),
    ("git push origin HEAD:master", "push_default"),
    ("git -C sub push origin main", "push_default"),
    ("git push --all origin", "push_default"),
    ("git push origin feature/x", None),
    ("git push -u origin acc/fix-login", None),
    ("rm -rf /", "rm_outside"),
    ("rm -rf ~/projects", "rm_outside"),
    ("sudo rm -r /etc/nginx", "rm_outside"),
    ("rm -rf ../other", "rm_outside"),
    ("rm -rf $HOME/.cache", "rm_outside"),
    ("rm -rf build dist node_modules", None),
    ("rm -rf /srv/ws/build", None),
    ("rm -rf /tmp/scratch", None),
    ("rm /etc/hosts", None),  # not recursive: the CLI's own permission rules apply
    ("cat .env", "secrets"),
    ("cp .env.production /tmp/x", "secrets"),
    ("cat ~/.ssh/id_ed25519", "secrets"),
    ("cat .env.example", None),
    ("grep -r TODO src", None),
    ("echo 'git push origin main' > notes.txt", None),  # quoted text, not a push
])
def test_command_rules(command, expected):
    assert rule(command) == expected


@pytest.mark.parametrize("command,expected", [
    # scripts a shell reads from a heredoc, a here-string or a pipe; eval
    ("cat <<EOF | sh\nrm -rf /opt\nEOF", "rm_outside"),
    ("echo 'rm -rf /opt' | bash", "rm_outside"),
    ("bash <<< 'git push -f'", "force_push"),
    ("eval 'git push -f'", "force_push"),
    ("bash -o pipefail -c 'git push origin main'", "push_default"),
    # an unquoted heredoc's substitutions run; a quoted one's are text
    ("cat <<EOF > notes.md\n$(cat .env)\nEOF", "secrets"),
    ("cat <<'EOF' > notes.md\n$(cat .env)\nEOF", None),
    ('echo "${X:-$(git push -f)}"', "force_push"),
    ("function f { git push -f; }", "force_push"),
    ("f() { git push -f; }", "force_push"),
    ("files=(.env.local .env); echo ok", None),  # an array's words aren't commands
    ("diff <(cat .env) b", "secrets"),
    # directories: wrappers' own, cd in groups (kept), subshells and pipelines (not kept), unknown targets
    ("sudo -D /opt rm -rf x", "rm_outside"),
    ("env -C /opt rm -rf x", "rm_outside"),
    ("{ cd /opt; }; rm -rf example", "rm_outside"),
    ("(cd /opt); rm -rf example", None),
    ("cd /tmp/x | rm -rf ../y", "rm_outside"),  # rm runs in /srv/ws: ../y is /srv/y
    ('cd "$X" && rm -rf build', "rm_outside"),
    ("cd ~/.ssh && cat config", "secrets"),
    ("timeout -s KILL 30s rm -rf /opt", "rm_outside"),
    ("command -v git", None),
    # text and filters are not file access
    ("git commit -m 'load .env lazily' && git push -u origin feature", None),
    ("rsync -a --exclude=.env src/ dst/", None),
    ("rg -g '!*.env' token src", None),
    ("rg -f .env src", "secrets"),
    ("echo hi > .env", "secrets"),
    ("git push origin 'refs/tags/*:refs/tags/*'", None),
    ("git push origin $BRANCH", "push_default"),  # only known when it runs
])
def test_shell_structure(command, expected):
    assert rule(command) == expected


def test_file_tools_and_patches():
    assert rule(path="/srv/ws/.env") == "secrets"
    assert rule(path="/home/a/.copilot/config.json") == "secrets"
    assert rule(path="/srv/ws/src/app.py") is None
    patch = {"input": "*** Begin Patch\n*** Update File: .env.local\n@@\n-A=1\n+A=2\n*** End Patch"}
    assert guard.check(ALL, "apply_patch", patch, "/srv/ws")[0] == "secrets"  # Codex names files inside the patch
    assert guard.check(ALL, "run_command", {"CommandLine": "git push -f"}, "/srv/ws")[0] == "force_push"  # Antigravity


def test_rules_off_and_custom_patterns():
    off = {"rules": {}, "custom": [], "workspace": "/srv/ws"}
    assert rule("git push --force", policy=off) is None and rule("cat .env", policy=off) is None
    custom = {**off, "custom": [{"pattern": r"\bterraform\s+destroy\b", "reason": "no destroying infrastructure"}]}
    assert guard.check(custom, "Bash", {"command": "cd infra && terraform destroy -auto-approve"}, "/srv/ws") == (
        "custom", "no destroying infrastructure")
    assert rule("terraform plan", policy=custom) is None


def run_guard(provider, payload, run_dir=None, tmp=None, extra=None):
    env = {"PATH": "/usr/bin:/bin", **(extra or {})}
    if run_dir:
        env |= {"ACC_RUN_DIR": str(run_dir), "ACC_SESSION_ID": "abc"}
    return subprocess.run([str(GUARD), provider, "PreToolUse"], input=json.dumps(payload), text=True,
                          capture_output=True, env=env)


@pytest.mark.parametrize("provider,check", [
    ("claude", lambda r: r.returncode == 2 and json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"),
    ("codex", lambda r: r.returncode == 2 and "force pushes" in r.stderr),
    ("copilot", lambda r: r.returncode == 0 and json.loads(r.stdout)["permissionDecision"] == "deny"),
    ("cursor", lambda r: r.returncode == 0 and json.loads(r.stdout)["permission"] == "deny"),
    ("antigravity", lambda r: r.returncode == 0 and json.loads(r.stdout)["decision"] == "deny"),
    ("opencode", lambda r: r.returncode == 2 and r.stderr.strip().splitlines()[-1].endswith("Rynhult's guardrails")),
])
def test_each_cli_gets_its_own_deny(tmp_path, provider, check):
    (tmp_path / "guardrails.json").write_text(json.dumps(ALL))
    result = run_guard(provider, {"tool_name": "Bash", "tool_input": {"command": "git push --force"}, "cwd": "/srv/ws"}, tmp_path)
    assert check(result), (result.returncode, result.stdout, result.stderr)
    record = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert record["guard"]["rule"] == "force_push" and record["event"] == "PreToolUse"


def test_allows_quietly_and_outside_acc(tmp_path):
    (tmp_path / "guardrails.json").write_text(json.dumps(ALL))
    ok = run_guard("claude", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, tmp_path)
    assert ok.returncode == 0 and ok.stdout == ""
    assert "guard" not in json.loads((tmp_path / "events.jsonl").read_text())  # still recorded (the "doing now" line)
    # Antigravity: no output is "no opinion" (its own permission rules apply); `{}` would deny the call (seen live)
    assert run_guard("antigravity", {"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}, tmp_path).stdout == ""
    outside = run_guard("claude", {"tool_name": "Bash", "tool_input": {"command": "git push -f"}})  # no ACC_RUN_DIR
    assert outside.returncode == 0
    garbage = subprocess.run([str(GUARD), "claude"], input="not json", text=True, capture_output=True,
                             env={"ACC_RUN_DIR": str(tmp_path)})
    assert garbage.returncode == 0


FORCE = {"tool_name": "Bash", "tool_input": {"command": "git push -f origin feature"}, "cwd": "/srv/ws"}


def test_agent_cannot_turn_off_its_guard_policy(tmp_path):
    """X6/D2: overwriting, emptying or removing guardrails.json used to turn every rule off. Inside a session the
    broker puts the launch files' hashes in the CLI's own environment; a policy that is missing, unreadable or changed
    blocks everything, and the guard refuses the write in the first place."""
    import hashlib
    text = json.dumps(ALL)
    (tmp_path / "guardrails.json").write_text(text)
    hashes = {"ACC_GUARD_FILES": f"guardrails.json={hashlib.sha256(text.encode()).hexdigest()}"}
    assert run_guard("claude", FORCE, tmp_path, extra=hashes).returncode == 2
    overwrite = {"tool_name": "Bash", "tool_input": {"command": "printf '{}' > \"$ACC_RUN_DIR/guardrails.json\""}}
    refused = run_guard("claude", overwrite, tmp_path, extra=hashes)
    assert refused.returncode == 2 and "guardrails" in refused.stderr
    for damage in ("{}", "not json", "[]", None):  # what the write would have done, or a removal
        if damage is None:
            (tmp_path / "guardrails.json").unlink()
        else:
            (tmp_path / "guardrails.json").write_text(damage)
        result = run_guard("claude", FORCE, tmp_path, extra=hashes)
        assert result.returncode == 2 and "restart the session" in result.stderr, damage
        assert run_guard("claude", {"tool_name": "Read", "tool_input": {"file_path": "/srv/ws/a.py"}}, tmp_path,
                         extra=hashes).returncode == 2  # everything, not only what the rules would block
    # launched by a broker that set no hashes: a missing or broken policy still blocks
    assert run_guard("claude", FORCE, tmp_path).returncode == 2
    (tmp_path / "guardrails.json").write_text("{}")
    assert run_guard("claude", FORCE, tmp_path).returncode == 0  # an empty but valid policy has no rules
    # a session launched without rules (the broker lists no guardrails.json) has nothing to check
    assert run_guard("claude", FORCE, tmp_path, extra={"ACC_GUARD_FILES": ""}).returncode == 0


def test_changed_hook_settings_block_everything(tmp_path):
    import hashlib
    (tmp_path / "guardrails.json").write_text(json.dumps(ALL))
    (tmp_path / "settings.json").write_text('{"hooks": {}}')
    digest = lambda name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
    hashes = {"ACC_GUARD_FILES": f"guardrails.json={digest('guardrails.json')} settings.json={digest('settings.json')}"}
    ls = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert run_guard("claude", ls, tmp_path, extra=hashes).returncode == 0
    (tmp_path / "settings.json").write_text('{"hooks": {}, "disableAllHooks": true}')
    assert run_guard("claude", ls, tmp_path, extra=hashes).returncode == 2


async def test_settings_defaults_and_validation(api):
    from test_admin_features import owner_browser

    from acc.server import auth
    from acc.server.models import AuthSession
    headers = await owner_browser(api)
    with api.app.state.db() as db:
        db.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = auth.now()
        db.commit()
    state = (await api.get("/api/guardrails", headers=headers)).json()
    assert all(state["rules"].values()) and state["custom"] == [] and len(state["available"]) == 5
    assert "merge_pr" in {r["id"] for r in state["available"]}
    bad = await api.put("/api/guardrails", headers=headers, json={"revision": 0, "rules": {}, "custom": [{"pattern": "(unclosed"}]})
    assert bad.status_code == 422
    saved = (await api.put("/api/guardrails", headers=headers, json={"revision": 0, "rules": {"secrets": False},
                                                    "custom": [{"pattern": "terraform destroy", "reason": "no"}]})).json()
    assert saved["rules"]["secrets"] is False and saved["rules"]["force_push"] is True and saved["custom"][0]["reason"] == "no"


async def test_session_is_blocked_and_owner_told(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    detail = await get(api, session["id"])
    policy = json.loads(detail["launch_spec"]["files"]["guardrails.json"])  # frozen into the launch files
    assert policy["workspace"].endswith("/demo") and policy["rules"]["push_default"] and policy["rules"]["merge_pr"]
    settings = json.loads(detail["launch_spec"]["files"]["settings.json"])
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith("acc-guard claude PreToolUse")
    control(env, session["id"], "guarded git push origin main")

    async def blocked():
        items = [i for i in (await api.get("/api/attention")).json()
                 if i["kind"] == "guardrail_blocked" and i["session_id"] == session["id"]]
        return items[0] if items else None
    item = await wait_for(blocked)
    assert "main/master" in item["detail"] and "git push origin main" in item["title"]
    s = await get(api, session["id"])
    assert s["activity"] == "Blocked: Running git push origin main"
    control(env, session["id"], "guarded gh pr merge 3 --squash")  # on by default for every session

    async def merge_blocked():
        items = [i for i in (await api.get("/api/attention")).json()
                 if i["kind"] == "guardrail_blocked" and i["session_id"] == session["id"] and "gh pr merge" in i["title"]]
        return items[0] if items else None
    assert "a person merges" in (await wait_for(merge_blocked))["detail"]



def test_edit_tool_text_is_not_a_command():
    """Copilot's Edit passes a raw patch string: test data in it must not read as a force push (seen live)."""
    patch = "*** Begin Patch\n*** Add File: /srv/ws/tests/t.py\n+CASES = ['git push --force origin main', 'rm -rf /']\n*** End Patch"
    name, args = guard.tool_call({"tool_name": "Edit", "tool_input": patch})
    assert guard.check(ALL, name, args, "/srv/ws") is None
    secret = "*** Begin Patch\n*** Update File: /srv/ws/.env\n-A=1\n+A=2\n*** End Patch"
    name, args = guard.tool_call({"tool_name": "Edit", "tool_input": secret})
    assert guard.check(ALL, name, args, "/srv/ws")[0] == "secrets"  # its file names still count
    name, args = guard.tool_call({"tool_name": "bash", "tool_input": "git push -f"})
    assert guard.check(ALL, name, args, "/srv/ws")[0] == "force_push"  # a shell tool's string is the command



def test_absurd_nesting_is_refused_not_waved_through():
    deep = "(" * 3000 + "rm -rf /" + ")" * 3000
    assert guard.check(ALL, "Bash", {"command": deep}, "/srv/ws")[0] in ("too_complex", "rm_outside")
    sub = "echo " + "$(" * 2000 + "git push -f" + ")" * 2000
    assert guard.check(ALL, "Bash", {"command": sub}, "/srv/ws") is not None


def test_a_slow_check_is_refused_not_let_through(tmp_path, monkeypatch):
    """D13: each HEAD push target ran git with a 5 s timeout, so a FIFO .git/HEAD held a check past the CLIs' 10 s hook
    timeout, and some CLIs then let the command through. HEAD is now read directly (non-blocking, per folder) and the
    whole check has one deadline that refuses."""
    import os
    import time
    fifo = tmp_path / "fifo"
    (fifo / ".git").mkdir(parents=True)
    os.mkfifo(fifo / ".git" / "HEAD")
    started = time.monotonic()
    command = "git -C ./fifo push origin HEAD HEAD HEAD; git push origin feature"
    assert guard.check(ALL, "Bash", {"command": command}, str(tmp_path))[0] == "push_default"  # unknown counts as main
    assert time.monotonic() - started < 1

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n")
    assert guard.check(ALL, "Bash", {"command": "git push origin HEAD"}, str(repo / "src")) is None
    worktree = tmp_path / "wt"
    (repo / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (repo / ".git" / "worktrees" / "wt" / "HEAD").write_text("ref: refs/heads/main\n")
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {repo}/.git/worktrees/wt\n")
    assert guard.check(ALL, "Bash", {"command": "git push"}, str(worktree))[0] == "push_default"

    monkeypatch.setattr(guard, "current_branch", lambda directory: time.sleep(5) or "feature")
    started = time.monotonic()
    verdict, error = guard.decide(ALL, None, "Bash", {"command": "git push origin HEAD"}, str(repo), "", deadline=0.3)
    assert verdict[0] == "too_slow" and error is None and time.monotonic() - started < 2


@pytest.mark.parametrize("tool,args,expected", [
    ("shell", {"command": "git push --force", "workdir": "sub"}, "force_push"),
    ("shell", {"command": "rm -rf ../other", "workdir": "pkg"}, None),  # /srv/ws/pkg/../other is inside the workspace
    ("shell", {"command": "rm -rf ../../other", "workdir": "pkg"}, "rm_outside"),
    ("read", {"path": ".env"}, "secrets"),
    ("read", {"path": "/home/a/.local/share/opencode/opencode.db"}, "secrets"),  # its saved keys and tokens
    ("grep", {"pattern": "KEY", "path": "/home/a/.local/share/opencode/auth.json"}, "secrets"),
    ("edit", {"path": "src/app.ts", "oldString": "a", "newString": "b"}, None),
    ("write", {"path": ".opencode/plugins/x.ts", "content": "export default {}"}, "own_files"),  # runs inside OpenCode
    ("patch", {"patchText": "*** Begin Patch\n*** Update File: src/a.ts\n*** Move to: .env\n*** End Patch"}, "secrets"),
    ("patch", {"patchText": "*** Begin Patch\n*** Add File: .opencode/plugins/y.js\n+x\n*** End Patch"}, "own_files"),
    ("shell", {"command": "opencode auth export"}, "secrets"),  # prints every saved credential
    ("shell", {"command": "opencode auth list"}, None),
    ("shell", {"command": "cp evil.js .opencode/plugins/"}, "own_files"),
    ("execute", {"code": "await tools.shell({command: 'rm -rf /'})"}, None),  # Code Mode: its nested calls are checked
])
def test_opencode_tool_calls(tool, args, expected):
    """OpenCode 2's tools and arguments (docs: tools; seen live with 2.1.3), as Rynhult's plugin passes them."""
    name, parsed = guard.tool_call({"tool_name": tool, "tool_input": args})
    verdict = guard.check(ALL, name, parsed, "/srv/ws")
    assert (verdict[0] if verdict else None) == expected


def test_opencode_mcp_file_in_the_run_folder_is_a_secret(tmp_path):
    guard.RUN_DIR = str(tmp_path)
    try:
        assert guard.secret_path(str(tmp_path / "opencode-mcp.json"))
        assert guard.guarded(str(tmp_path / "opencode/plugins/acc/index.js"))  # and can't be changed
    finally:
        guard.RUN_DIR = ""


def test_new_run_dir_spelling_resolves_to_the_same_protected_files(monkeypatch):
    monkeypatch.setattr(guard, 'RUN_DIR', '/tmp/example-run')
    assert guard.session_vars()['RYNHULT_RUN_DIR'] == '/tmp/example-run'
    assert guard.secret_path('${RYNHULT_RUN_DIR}/session.env', '/srv/ws')


@pytest.mark.parametrize('command', [
    'printf unsafe > "$ACC_RUN_DIR/launch-command.sh"',
    'cp /tmp/other "$ACC_RUN_DIR/launch-command.sh"',
    'chmod 777 "$ACC_RUN_DIR/launch-command.sh"',
])
def test_long_launch_script_stays_inside_guard_protected_run_folder(tmp_path, command):
    import hashlib
    text = json.dumps(ALL)
    (tmp_path / 'guardrails.json').write_text(text)
    hashes = {'ACC_GUARD_FILES': f'guardrails.json={hashlib.sha256(text.encode()).hexdigest()}'}
    refused = run_guard('codex', {'tool_name': 'exec_command', 'tool_input': {'cmd': command}}, tmp_path, extra=hashes)
    assert refused.returncode == 2
    record = json.loads((tmp_path / 'events.jsonl').read_text().splitlines()[-1])
    assert record['guard']['rule'] == 'own_files'


def test_team_publish_help_pipeline_does_not_dump_secrets():
    command = "git log -1 --format='%h %s'; npm test 2>&1 | grep -E '^ℹ (tests|pass|fail)'; npm run contrast -- --check 2>&1 | tail -1; acc-request publish --help 2>&1 | head -30"
    assert rule(command) is None


@pytest.mark.parametrize('command', ['env', 'printenv', 'cat .env', 'cat /proc/self/environ', 'cat /srv/acc/data/secrets/key'])
def test_team_unblock_keeps_actual_secret_reads_blocked(command):
    assert rule(command) == 'secrets'
