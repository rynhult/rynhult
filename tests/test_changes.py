"""Current-branch changes, shared by the Changes tab and the polled row summary."""
from __future__ import annotations

import subprocess

import pytest
from helpers import wait_for
from isolated_settings import isolated_broker
from test_supervisor import get, launch, state_is

from acc.broker import ops


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("one\ntwo\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "start")
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_changes_against_the_current_branch_merge_base(tmp_path):
    config = isolated_broker(tmp_path, socket_path=tmp_path / "s", run_dir=tmp_path, tmux_socket="x", allowed_roots=(tmp_path,),
                          allowed_uids=frozenset(), providers={}, hook_path="x")
    base = repo(tmp_path / "ws")
    ws = tmp_path / "ws"
    git(ws, "checkout", "-qb", "feature")
    (ws / "app.py").write_text("one\nTWO\nthree\n")  # edited
    (ws / "lib.py").write_text("a\nb\n")
    git(ws, "add", "lib.py")
    git(ws, "commit", "-qm", "agent commit")  # committed after the session started
    (ws / "notes.md").write_text("new\n")  # not yet added
    summary = ops.workspace_changes(config, str(ws), base)
    assert summary == {"git": True, "base": base[:12], "since_launch": False, "branch": "feature", "files": 3,
                       "additions": 5, "deletions": 1, "comparison_ref": "main"}
    detail = ops.workspace_changes(config, str(ws), base, detail=True)
    assert [(f["path"], f["status"]) for f in detail["list"]] == [("app.py", "M"), ("lib.py", "A"), ("notes.md", "A")]
    assert "+TWO" in detail["diff"] and "+new" in detail["diff"] and not detail["truncated"]

    assert ops.workspace_changes(config, str(ws), None) == summary  # independent of the launch commit
    (tmp_path / "fresh").mkdir()
    git(tmp_path / "fresh", "init", "-q", "-b", "main")
    assert ops.git_baseline(tmp_path / "fresh") == ops.EMPTY_TREE  # no commits yet: everything counts
    (tmp_path / "fresh" / "a.txt").write_text("x\n")
    assert ops.workspace_changes(config, str(tmp_path / "fresh"), ops.EMPTY_TREE)["files"] == 1
    git(tmp_path / "fresh", "add", ".")
    git(tmp_path / "fresh", "commit", "-qm", "first")
    assert ops.workspace_changes(config, str(tmp_path / "fresh"), ops.EMPTY_TREE)["files"] == 0
    (tmp_path / "plain").mkdir()
    assert ops.git_baseline(tmp_path / "plain") is None
    assert ops.workspace_changes(config, str(tmp_path / "plain"), None) == {"git": False}
    with pytest.raises(ops.OpError):
        ops.workspace_changes(config, str(ws), "HEAD; rm -rf /")
    with pytest.raises(ops.OpError):
        ops.workspace_changes(config, "/etc", None)  # outside the workspace roots


def test_names_git_would_quote_come_back_as_they_are(tmp_path):
    """R10: a tab or a quote in a name: the file list has the real name, which the UI matches to the diff's quoted
    header after decoding it (web/src/lib/gitpath.ts)."""
    config = isolated_broker(tmp_path, socket_path=tmp_path / "s", run_dir=tmp_path, tmux_socket="x", allowed_roots=(tmp_path,),
                          allowed_uids=frozenset(), providers={}, hook_path="x")
    ws = tmp_path / "ws"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "main")
    for name in ("tab\tname.txt", 'qu"ote.txt', "plain name.txt", "ünï.txt"):
        (ws / name).write_text("a\n")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "start")
    base = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    for name in ("tab\tname.txt", 'qu"ote.txt', "ünï.txt"):
        (ws / name).write_text("a\nb\n")
    (ws / "new\ttab.txt").write_text("n\n")
    git(ws, "rm", "-q", "plain name.txt")
    detail = ops.workspace_changes(config, str(ws), base, detail=True)
    assert {f["path"]: (f["status"], f["additions"], f["deletions"]) for f in detail["list"]} == {
        "tab\tname.txt": ("M", 1, 0), 'qu"ote.txt': ("M", 1, 0), "plain name.txt": ("D", 0, 1), "ünï.txt": ("M", 1, 0),
        "new\ttab.txt": ("A", 1, 0)}
    assert 'diff --git "a/tab\\tname.txt" "b/tab\\tname.txt"' in detail["diff"]  # the header the UI decodes


def test_changes_name_the_current_branch(tmp_path):
    config = isolated_broker(tmp_path, socket_path=tmp_path / "s", run_dir=tmp_path, tmux_socket="x", allowed_roots=(tmp_path,),
                          allowed_uids=frozenset(), providers={}, hook_path="x")
    base = repo(tmp_path / "ws")
    ws = tmp_path / "ws"
    git(ws, "checkout", "-qb", "acc/fix-login-1")
    assert ops.workspace_changes(config, str(ws), base)["branch"] == "acc/fix-login-1"
    git(ws, "checkout", "-q", "--detach")
    assert ops.workspace_changes(config, str(ws), base)["branch"] is None  # detached: no branch a PR could match
    (tmp_path / "fresh").mkdir()
    git(tmp_path / "fresh", "init", "-q", "-b", "main")
    (tmp_path / "fresh" / "a.txt").write_text("x\n")
    assert ops.workspace_changes(config, str(tmp_path / "fresh"), ops.EMPTY_TREE)["branch"] is None  # no commit yet


@pytest.fixture
def changes_config(tmp_path):
    return isolated_broker(tmp_path, socket_path=tmp_path / "s", run_dir=tmp_path, tmux_socket="x", allowed_roots=(tmp_path,),
                        allowed_uids=frozenset(), providers={}, hook_path="x")


def test_branch_switch_replaces_the_stale_launch_baseline(tmp_path, changes_config):
    ws = tmp_path / "ws"
    repo(ws)
    git(ws, "checkout", "-qb", "old-task")
    (ws / "old-task.txt").write_text("unrelated\n" * 1000)
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "old task")
    stale = git(ws, "rev-parse", "HEAD")
    git(ws, "checkout", "-q", "main")
    (ws / "main-update.txt").write_text("upstream work\n" * 1000)
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "main advanced")
    main = git(ws, "rev-parse", "HEAD")
    git(ws, "checkout", "-qb", "new-task")
    (ws / "feature.txt").write_text("new task\n")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "new task")
    (ws / "app.py").write_text("one\ntwo\nthree\n")

    summary = ops.workspace_changes(changes_config, str(ws), stale)
    detail = ops.workspace_changes(changes_config, str(ws), stale, detail=True)
    assert summary["base"] == main[:12]
    assert summary["comparison_ref"] == "main" and not summary["since_launch"]
    assert (summary["files"], summary["additions"], summary["deletions"]) == (2, 2, 0)
    assert {key: detail[key] for key in summary} == summary
    assert {entry["path"] for entry in detail["list"]} == {"app.py", "feature.txt"}


def test_changes_use_the_merge_base_not_the_upstream_tip(tmp_path, changes_config):
    ws = tmp_path / "ws"
    base = repo(ws)
    git(ws, "branch", "-m", "trunk")
    git(ws, "checkout", "-qb", "topic")
    (ws / "app.py").write_text("one\ntwo\nthree\n")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "feature")
    git(ws, "checkout", "-q", "trunk")
    (ws / "upstream.txt").write_text("not this branch\n" * 1000)
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "upstream advanced")
    git(ws, "remote", "add", "upstream", str(tmp_path / "unused-remote"))
    git(ws, "update-ref", "refs/remotes/upstream/trunk", git(ws, "rev-parse", "HEAD"))
    git(ws, "checkout", "-q", "topic")
    git(ws, "branch", "--set-upstream-to=upstream/trunk")
    (ws / "notes.md").write_text("uncommitted\n")

    changes = ops.workspace_changes(changes_config, str(ws), None, detail=True)
    assert changes["base"] == base[:12] and changes["comparison_ref"] == "upstream/trunk"
    assert (changes["files"], changes["additions"], changes["deletions"]) == (2, 2, 0)
    assert "upstream.txt" not in changes["diff"]


def test_main_is_preferred_to_the_feature_branch_own_upstream(tmp_path, changes_config):
    ws = tmp_path / "ws"
    repo(ws)
    git(ws, "checkout", "-qb", "topic")
    (ws / "feature.txt").write_text("this branch\n")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "feature")
    git(ws, "remote", "add", "origin", str(tmp_path / "unused-remote"))
    git(ws, "update-ref", "refs/remotes/origin/topic", git(ws, "rev-parse", "HEAD"))
    git(ws, "branch", "--set-upstream-to=origin/topic")
    changes = ops.workspace_changes(changes_config, str(ws), None)
    assert changes["comparison_ref"] == "main"
    assert (changes["files"], changes["additions"], changes["deletions"]) == (1, 1, 0)


def test_untracked_scratch_is_not_a_checkout_and_nested_tracked_folders_are_scoped(tmp_path, changes_config):
    ws = tmp_path / "ws"
    base = repo(ws)
    nested = ws / "package"
    nested.mkdir()
    (nested / "module.py").write_text("one\n")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "package")
    scratch = ws / "scratch"
    scratch.mkdir()
    (scratch / "unpacked.py").write_text("not a checkout\n" * 1000)
    assert ops.workspace_changes(changes_config, str(scratch), base) == {"git": False}

    (ws / "app.py").write_text("unrelated parent changes\n" * 1000)
    (nested / "module.py").write_text("one\ntwo\n")
    changes = ops.workspace_changes(changes_config, str(nested), base, detail=True)
    assert (changes["files"], changes["additions"], changes["deletions"]) == (1, 1, 0)
    assert changes["list"][0]["path"] == "module.py" and "app.py" not in changes["diff"]


def test_without_an_upstream_or_main_only_working_changes_are_counted(tmp_path, changes_config):
    ws = tmp_path / "ws"
    stale = repo(ws)
    git(ws, "branch", "-m", "trunk")
    (ws / "app.py").write_text("one\ntwo\nthree\n")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "committed work")
    git(ws, "checkout", "-q", "--detach")
    (ws / "notes.md").write_text("uncommitted\n")
    changes = ops.workspace_changes(changes_config, str(ws), stale)
    assert changes["base"] == git(ws, "rev-parse", "HEAD")[:12] and not changes["since_launch"]
    assert (changes["files"], changes["additions"], changes["deletions"]) == (1, 1, 0)


def test_nested_checkout_keeps_its_last_staged_deletion(tmp_path, changes_config):
    ws = tmp_path / "ws"
    repo(ws)
    nested = ws / "package"
    nested.mkdir()
    module = nested / "module.py"
    module.write_text("one\n")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "package")
    module.unlink()
    git(ws, "rm", "-q", "package/module.py")
    nested.mkdir(exist_ok=True)
    changes = ops.workspace_changes(changes_config, str(nested), None, detail=True)
    assert changes["git"] and (changes["files"], changes["deletions"]) == (1, 1)
    assert changes["list"][0]["status"] == "D"


def test_failed_merge_base_is_an_error_not_a_clean_badge(tmp_path, changes_config, monkeypatch):
    ws = tmp_path / "ws"
    repo(ws)
    real = ops._git

    def broken(cwd, *args, **kwargs):
        if args[0] == "merge-base":
            return subprocess.CompletedProcess(args, 128, "", "fatal: broken history")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(ops, "_git", broken)
    with pytest.raises(ops.OpError, match="broken history"):
        ops.workspace_changes(changes_config, str(ws), None)


async def test_session_summary_and_changes_share_the_current_branch_baseline(api, project, env):
    repo(env["workspaces"] / "demo")
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    (env["workspaces"] / "demo" / "app.py").write_text("one\ntwo\nthree\n")

    async def counted():
        s = await get(api, session["id"])
        changes = s.get("changes") or {}
        # A collection overlapping write_text can briefly see the truncated file.
        # Wait for the complete asynchronous snapshot, not just its file count.
        return s if changes.get("files") == 1 and changes.get("additions") == 1 else None
    row = await wait_for(counted)
    assert row["changes"]["additions"] == 1 and row["changes"]["branch"] == "main"
    changes = (await api.get(f"/api/sessions/{session['id']}/changes")).json()
    assert not changes["since_launch"] and changes["comparison_ref"] == "main"
    assert changes["list"][0]["path"] == "app.py" and "+three" in changes["diff"]

    ws = env["workspaces"] / "demo"
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "main advanced after launch")
    git(ws, "checkout", "-qb", "next-task")
    (ws / "feature.txt").write_text("new branch work\n")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "feature")
    (ws / "notes.md").write_text("uncommitted\n")

    async def branch_counted():
        session_row = await get(api, session["id"])
        summary = session_row.get("changes") or {}
        return summary if summary.get("branch") == "next-task" and summary.get("files") == 2 else None

    summary = await wait_for(branch_counted)
    changes = (await api.get(f"/api/sessions/{session['id']}/changes")).json()
    assert (summary["additions"], summary["deletions"]) == (2, 0)
    assert {key: changes[key] for key in summary} == summary
    assert {entry["path"] for entry in changes["list"]} == {"feature.txt", "notes.md"}
