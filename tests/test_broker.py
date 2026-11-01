"""Broker input validation and peer-credential gating."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess

import pytest
from helpers import wait_for

from acc.broker import ops
from acc.broker.server import serve


@pytest.mark.parametrize(("status", "signal"), [(0, None), (1, None), (None, 15), (None, None)])
def test_list_collects_pending_exit_result(env, monkeypatch, status, signal):
    # Older tmux/utempter can lose SIGCHLD, leaving a dead pane's child unreaped.
    # Model that external state: another tmux job exiting lets it reap all children.
    collected = False

    def tmux(config, *args):
        nonlocal collected
        assert config is env["broker"]
        if args == ("run-shell", "true"):
            collected = True
            output = ""
        else:
            assert args == ("list-panes", "-a", "-F", ops.PANE_FORMAT)
            code = str(status) if collected and status is not None else ""
            sig = str(signal) if collected and signal is not None else ""
            output = f"acc-work-abcdef12\t1\t{code}\t{sig}\t123\t100\t101\n"
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(ops, "tmux", tmux)
    pane, = ops.list_sessions(env["broker"])
    assert (pane["exit_status"], pane["exit_signal"]) == (status, signal)


def base_params(world, **overrides):
    params = {"acc_id": "abcdef123456", "tmux_name": "acc-t-abcdef12", "provider": "claude",
              "args": ["--session-id", "11111111-2222-3333-4444-555555555555"],
              "cwd": str(world["workspaces"] / "demo"), "files": {}, "env": {}}
    params.update(overrides)
    return params


@pytest.mark.parametrize("override, message", [
    ({"cwd": "/etc"}, "outside this account.s workspace folders"),
    ({"tmux_name": "cc-roost"}, "invalid session terminal name"),
    ({"tmux_name": "acc-x; rm -rf"}, "invalid session terminal name"),
    ({"provider": "bash"}, "isn.t an agent CLI this machine knows"),
    ({"acc_id": "../../etc"}, "invalid session ID"),
    ({"files": {"../escape": "x"}}, "won.t write a session file named"),
    ({"env": {"LD_PRELOAD": "/tmp/x.so"}}, "not allowed"),
    ({"env": {"PATH": "/tmp"}}, "not allowed"),
])
def test_launch_rejects_bad_input(env, override, message):
    with pytest.raises(ops.OpError, match=message):
        ops.launch(env["broker"], base_params(env, **override))


def test_workspace_symlink_escape_rejected(env, tmp_path):
    (env["workspaces"] / "sneaky").symlink_to("/etc")
    with pytest.raises(ops.OpError, match="outside"):
        ops.launch(env["broker"], base_params(env, cwd=str(env["workspaces"] / "sneaky")))


def test_duplicate_launch_refused(env):
    ops.launch(env["broker"], base_params(env))
    with pytest.raises(ops.OpError, match="already running"):
        ops.launch(env["broker"], base_params(env))


def test_guard_files_are_read_only_and_hashed_into_the_session(env):
    """X6/D2: the guardrails and hook settings are written read-only, and their hashes go into the CLI's environment
    (which a tool call can't change), so acc-guard can tell when they were changed. A relaunch rewrites them."""
    import hashlib
    files = {"guardrails.json": '{"rules": {}}\n', "settings.json": '{"hooks": {}}\n', "append-prompt.md": "hi\n"}
    params = base_params(env, files=files)
    ops.launch(env["broker"], params)
    run_dir = env["broker"].run_dir / params["acc_id"]
    assert (run_dir / "guardrails.json").stat().st_mode & 0o777 == 0o440
    assert (run_dir / "settings.json").stat().st_mode & 0o777 == 0o440
    assert (run_dir / "append-prompt.md").stat().st_mode & 0o777 == 0o640
    shown = ops.tmux(env["broker"], "show-environment", "-t", f"={params['tmux_name']}", "ACC_GUARD_FILES").stdout.strip()
    sha = lambda text: hashlib.sha256(text.encode()).hexdigest()
    assert shown == (f"ACC_GUARD_FILES=guardrails.json={sha(files['guardrails.json'])} "
                     f"settings.json={sha(files['settings.json'])}")
    ops.kill(env["broker"], params["tmux_name"])
    ops.launch(env["broker"], {**params, "files": {**files, "guardrails.json": "{}\n"}})  # over the read-only file
    assert (run_dir / "guardrails.json").read_text() == "{}\n"
    with pytest.raises(ops.OpError, match="not allowed"):
        ops.launch(env["broker"], base_params(env, tmux_name="acc-t-other", env={"ACC_GUARD_FILES": ""}))


def test_list_ignores_non_acc_sessions(env):
    ops.prepare_tmux(env["broker"])
    ops.tmux(env["broker"], "new-session", "-d", "-s", "cc-other", "sleep 30", check=True)
    assert all(s["tmux_name"].startswith("acc-") for s in ops.list_sessions(env["broker"]))


def test_transcript_id_must_be_a_uuid(env):
    with pytest.raises(ops.OpError):
        ops.transcript_exists("claude", "*")


async def test_peer_uid_not_allowed_is_disconnected(env, monkeypatch):
    # Root has a separate policy-publication channel; exercise an ordinary denied peer
    # regardless of the account running the gates.
    monkeypatch.setattr('acc.broker.server.peer_uid', lambda writer: 999998)
    config = dataclasses.replace(env["broker"], allowed_uids=frozenset({999999}))
    task = asyncio.create_task(serve(config))
    await wait_for(config.socket_path.exists)
    reader, writer = await asyncio.open_unix_connection(str(config.socket_path))
    writer.write(json.dumps({"id": 1, "method": "ping", "params": {}}).encode() + b"\n")
    await writer.drain()
    try:
        answer = await reader.readline()
    except ConnectionResetError:
        answer = b""
    assert answer == b""  # closed without an answer
    writer.close()
    task.cancel()


def test_delete_workspace_only_inside_local(env, tmp_path):
    local = env["workspaces"] / "local"
    ops.make_workspace(env["broker"], str(local), "gone-20261019", "Gone")
    (local / "gone-20261019" / "outside-link").symlink_to(tmp_path)  # rmtree must not follow it
    keep = tmp_path / "keep.txt"
    keep.write_text("x")
    assert ops.delete_workspace(env["broker"], str(local), "gone-20261019")["deleted"].endswith("gone-20261019")
    assert not (local / "gone-20261019").exists() and keep.exists()
    (env["workspaces"] / "project-x").mkdir()
    for parent, name, message in [
        (str(env["workspaces"]), "project-x", "not a local workspaces folder"),  # a project checkout, not local/
        (str(local), "../project-x", "Use a folder name"),
        ("/etc/local", "x", "not a local workspaces folder"),
    ]:
        with pytest.raises(ops.OpError, match=message):
            ops.delete_workspace(env["broker"], parent, name)
    (local / "linked-20261019").symlink_to(tmp_path)
    with pytest.raises(ops.OpError, match="not a workspace folder"):
        ops.delete_workspace(env["broker"], str(local), "linked-20261019")
    assert keep.exists()


def test_symlinked_docs_folders_cannot_lead_outside_the_workspace(env, tmp_path):
    outside = tmp_path / "outside"
    (outside / "wiki/.attachments").mkdir(parents=True)
    (outside / "secret.md").write_text("# secret")
    (outside / "wiki/Home.md").write_text("# home")
    (outside / "wiki/.attachments/x.png").write_bytes(b"\x89PNG")
    for name, link_at, target in (("docs-link", "docs", outside), ("wiki-link", "docs/wiki", outside / "wiki")):
        workspace = env["workspaces"] / name
        (workspace / link_at).parent.mkdir(parents=True, exist_ok=True)
        (workspace / link_at).symlink_to(target)
        assert ops.wiki_tree(env["broker"], str(workspace)) == {"exists": False, "pages": []}
        for call in (lambda: ops.wiki_page(env["broker"], str(workspace), "Home.md"),
                     lambda: ops.wiki_attachment(env["broker"], str(workspace), ".attachments/x.png")):
            with pytest.raises(ops.OpError):
                call()
    with pytest.raises(ops.OpError):
        ops.read_doc(env["broker"], str(env["workspaces"] / "docs-link"), "docs/secret.md")
    assert all(entries == [] for entries in ops.scan_docs(env["broker"], str(env["workspaces"] / "docs-link")).values())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_run_folder_under_a_traverse_only_parent(tmp_path):
    """/srv/acc/run is traverse-only (0711) for agent accounts: the run folder is still materialized (regression,
    every launch on the main host failed with "unsafe or changing session run folder")."""
    from acc.broker import ops
    parent = tmp_path / "run"
    (parent / "acct").mkdir(parents=True)
    parent.chmod(0o311)
    try:
        ops._materialize_run(parent / "acct" / "s1", {"settings.json": "{}", "nested/x.json": "{}"}, {"A": "1"})
        assert (parent / "acct" / "s1" / "nested" / "x.json").read_text() == "{}"
    finally:
        parent.chmod(0o755)


@pytest.mark.parametrize("where", ["demo", "."])  # the session folder, or a folder above it (its repository)
@pytest.mark.parametrize("name", [".github/copilot/settings.json", ".github/copilot/settings.local.json",
                                  ".claude/settings.json", ".claude/settings.local.json"])  # Claude-compatible config
def test_copilot_launch_refused_where_repository_settings_switch_hooks_off(env, where, name):
    """Owner decision (option a, 2026-10-26): in a trusted folder these files switch every Copilot hook off, the guard
    included. The broker refuses every Copilot launch there (first launch, resume and relaunch all come here), before
    anything is written or started."""
    from acc.providers import copilot_settings

    target = env["workspaces"] / where / name
    target.parent.mkdir(parents=True)
    target.write_text('{"disableAllHooks": true}')
    params = base_params(env, provider="copilot", files={"guardrails.json": "{}\n"})
    with pytest.raises(ops.OpError) as refused:
        ops.launch(env["broker"], params)
    message = str(refused.value)
    assert message.startswith(copilot_settings.REFUSAL) and str(target) in message and "disableAllHooks" in message
    assert ops.tmux(env["broker"], "has-session", "-t", f"={params['tmux_name']}").returncode != 0
    assert not (env["broker"].run_dir / params["acc_id"]).exists()
    ops.launch(env["broker"], base_params(env, files={"guardrails.json": "{}\n"}))  # another CLI isn't affected
    target.write_text('{"disableAllHooks": false, "model": "gpt-4o"}')
    ops.launch(env["broker"], base_params(env, provider="copilot", tmux_name="acc-t-copilot1"))


def test_copilot_launch_refused_on_a_malformed_settings_file(env):
    target = env["workspaces"] / "demo/.github/copilot/settings.local.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"disableAllHooks": true,}')
    with pytest.raises(ops.OpError, match="isn't plain JSON"):
        ops.launch(env["broker"], base_params(env, provider="copilot"))


def test_copilot_launch_refused_where_git_moves_the_working_tree(env, tmp_path):
    """Copilot reads repository settings at the git top level, and core.worktree puts that anywhere: a folder this
    check would otherwise never see (independent review, 2026-10-26)."""
    import subprocess
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".github/copilot").mkdir(parents=True)
    (elsewhere / ".github/copilot/settings.json").write_text('{"disableAllHooks": true}')
    demo = env["workspaces"] / "demo"
    subprocess.run(["git", "init", "-q", str(demo)], check=True)
    subprocess.run(["git", "-C", str(demo), "config", "core.worktree", str(elsewhere)], check=True)
    params = base_params(env, provider="copilot")
    with pytest.raises(ops.OpError, match="sets core.worktree"):
        ops.launch(env["broker"], params)
    assert not (env["broker"].run_dir / params["acc_id"]).exists()
    subprocess.run(["git", "-C", str(demo), "config", "--unset", "core.worktree"], check=True)
    ops.launch(env["broker"], base_params(env, provider="copilot", tmux_name="acc-t-copilot1"))


@pytest.mark.parametrize('provider', ['codex', 'claude'])
@pytest.mark.parametrize('payload', ['large', 'unicode'])
def test_long_launch_uses_private_script_and_preserves_arguments(env, monkeypatch, provider, payload):
    """Real shell execution, with a transport that rejects oversized tmux messages."""
    import shlex
    import stat
    from pathlib import Path

    capture = env['broker'].run_dir / 'captured.json'
    executable = env['broker'].run_dir / 'fake capture'
    executable.write_text('#!/usr/bin/env python3\nimport json,sys,os\nfrom pathlib import Path\n'
                          'if sys.argv[1:] == ["--version"]: sys.exit(0)\n'
                          f'Path({str(capture)!r}).write_text(json.dumps([sys.argv[1:], os.getenv("LAUNCH_CHECK")]))\n')
    executable.chmod(0o700)
    config = dataclasses.replace(env['broker'], providers={provider: str(executable)})
    arguments = ['--instructions', "core helper capabilities profile ' \" $HOME $(touch unwanted) ;\n雪" * 900]
    if payload == 'unicode':
        arguments = ['--instructions', '雪' * 1500]
    original_tmux = ops.tmux
    sent = []

    def bounded_tmux(config, *args, **kwargs):
        if args[0] == 'new-session':
            assert sum(len(arg.encode()) + 1 for arg in args) < 8192
            sent.append(args[-1])
        return original_tmux(config, *args, **kwargs)

    monkeypatch.setattr(ops, 'tmux', bounded_tmux)
    ops.launch(config, base_params(env, provider=provider, args=arguments,
                                   env={'LAUNCH_CHECK': "quotes ' $HOME ;\n雪"},
                                   files={'launch-command.sh': 'touch unwanted\n'}))
    import time
    for _ in range(100):
        if capture.exists():
            break
        time.sleep(.02)
    assert json.loads(capture.read_text()) == [arguments, "quotes ' $HOME ;\n雪"]
    script = Path(shlex.split(sent[0])[-1])
    assert script.parent == config.run_dir / 'abcdef123456'
    assert stat.S_IMODE(script.stat().st_mode) == 0o600
    assert not (env['workspaces'] / 'demo' / 'unwanted').exists()


def test_short_launch_keeps_direct_command(env):
    import shlex
    params = base_params(env)
    result = ops.launch(env['broker'], params)
    assert result['command'] == shlex.join([env['broker'].providers['claude'], *params['args']])


@pytest.mark.parametrize('override', [
    {'provider': 'sh'}, {'args': ['x' * 10000, '\0']}, {'env': {'PATH': '/tmp'}},
    {'files': {'../launch-command.sh': 'echo unsafe'}},
])
def test_long_launch_never_bypasses_validation(env, override):
    with pytest.raises(ops.OpError):
        ops.launch(env['broker'], base_params(env, **({'args': ['x' * 10000]} | override)))
    assert not (env['broker'].run_dir / 'abcdef123456' / 'launch-command.sh').exists()


def test_private_script_write_replaces_links_without_touching_target(env, tmp_path):
    run = env['broker'].run_dir / 'abcdef123456'
    ops._materialize_run(run, {}, {'TOKEN': 'keep'})
    target = tmp_path / 'outside'
    target.write_text('untouched')
    (run / 'launch-command.sh').symlink_to(target)
    ops._materialize_run(run, {'launch-command.sh': 'exec true\n'}, {}, private=True)
    assert target.read_text() == 'untouched'
    assert not (run / 'launch-command.sh').is_symlink()
    assert (run / 'session.env').read_text() == 'export TOKEN=keep\n'


def test_missing_workspace_delete_is_idempotent_but_never_accepts_a_symlink(env, tmp_path):
    local = env['workspaces'] / 'local'
    local.mkdir()
    for _ in range(2):
        result = ops.delete_workspace(env['broker'], str(local), 'already-removed')
        assert result == {'deleted': str(local / 'already-removed'), 'already_removed': True}
    (local / 'dangling').symlink_to(tmp_path / 'absent')
    with pytest.raises(ops.OpError, match='not a workspace folder'):
        ops.delete_workspace(env['broker'], str(local), 'dangling')
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'local').symlink_to(local)
    with pytest.raises(ops.OpError, match='not a local workspaces folder'):
        ops.delete_workspace(env['broker'], str(other / 'local'), 'already-removed')


def test_workspace_disappearing_during_removal_is_still_idempotent(env, monkeypatch):
    local = env['workspaces'] / 'local'
    target = local / 'vanishing'
    target.mkdir(parents=True)
    real_rmtree = ops.shutil.rmtree

    def vanished(path):
        real_rmtree(path)
        raise FileNotFoundError(str(path))

    monkeypatch.setattr(ops.shutil, 'rmtree', vanished)
    assert ops.delete_workspace(env['broker'], str(local), target.name)['already_removed'] is True
