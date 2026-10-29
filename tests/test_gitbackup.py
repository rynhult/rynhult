"""Git backup to the main ACC (M15): bare repositories served by git http-backend, pushed to by brokers with
one-push tokens (docs/MULTIHOST.md "Git backup")."""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from datetime import timedelta

import pytest
from helpers import wait_for, write_db
from test_auth import do_setup, live  # noqa: F401 (fixture)
from test_multihost import StandInNode, enroll
from test_users import browser, csrf, join

from acc.broker import ops
from acc.server import gitbackup
from acc.server.models import AuditLog, Event, Project


def git(cwd, *args, env=None, check=True) -> subprocess.CompletedProcess:
    # Bare repositories must be explicit even when Git is configured not to discover them.
    directory = ["--git-dir", str(cwd)] if (cwd / "HEAD").is_file() else []
    result = subprocess.run(["git", "-C", str(cwd), *directory, *args], capture_output=True, text=True, env=env)
    if check and result.returncode:
        raise AssertionError(f"git {args}: {result.stderr}")
    return result


def make_repo(path, branches=("main",)):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "agent@example.com")
    git(path, "config", "user.name", "Agent")
    (path / "README.md").write_text("hello\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    for branch in branches[1:]:
        git(path, "branch", branch)
    return path


def commit(path, name="change.txt", text="more\n", message="more"):
    if text is not None:
        (path / name).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD").stdout.strip()


def push_env(token: str) -> dict:
    return {**ops.backup_env(token), "HOME": os.environ["HOME"]}


@pytest.fixture
async def served(api, env, project, live, monkeypatch):  # noqa: F811
    """The live server is where brokers push: backups on, pointing at it. The supervisor's own schedule is paused,
    so each test drives backups itself (test_the_schedule_starts_a_round covers the schedule)."""
    base = live.replace("ws://", "http://")
    supervisor = api.app.state.supervisor
    if supervisor._backup_task is not None:
        supervisor._backup_task.cancel()
        await asyncio.gather(supervisor._backup_task, return_exceptions=True)

    async def paused():
        pass
    monkeypatch.setattr(supervisor, "schedule_backups", paused)
    object.__setattr__(api.app.state.ctx.settings, "backup_url", base)
    return base


def bare(env, project_id):
    return gitbackup.repo_path(env["settings"].git_dir, project_id)


def bare_refs(env, project_id) -> dict:
    out = git(bare(env, project_id), "for-each-ref", "--format=%(refname) %(objectname)").stdout
    return dict(line.split(" ") for line in out.splitlines())


# ------------------------------------------------------------------ the server side: http-backend and tokens


async def test_push_round_trip_and_refusals(api, env, project, served):
    supervisor = api.app.state.supervisor
    source = make_repo(env["workspaces"] / "demo", branches=("main", "feature"))
    pid = project["id"]
    with api.app.state.db() as s:
        owner = gitbackup.identity(s.get(Project, pid))
    await asyncio.to_thread(gitbackup.ensure_repo, env["settings"].git_dir, pid, owner)
    url = f"{served}/api/git/{pid}.git"
    token = supervisor.backup_tokens.mint(pid)
    pushed = await asyncio.to_thread(git, source, "push", "-q", url, "refs/heads/*:refs/heads/*", env=push_env(token))
    assert pushed.returncode == 0
    assert bare_refs(env, pid)["refs/heads/main"] == git(source, "rev-parse", "main").stdout.strip()
    (source / "big.bin").write_bytes(os.urandom(3 << 20))  # over git's 1 MiB post buffer: sent chunked
    big = commit(source, "big.bin", None, "big")
    pushed = await asyncio.to_thread(git, source, "push", "-q", url, "main", env=push_env(token))
    assert bare_refs(env, pid)["refs/heads/main"] == big

    async def refused(tok, target=url):
        result = await asyncio.to_thread(git, source, "push", url if target is None else target, "main:refs/heads/x",
                                         env=push_env(tok), check=False)
        return result.returncode != 0 and "refs/heads/x" not in bare_refs(env, pid)

    assert await refused("A" * 43)  # a wrong token
    other = (await api.post("/api/projects", json={"slug": "other", "name": "Other",
                                                   "workspace_path": str(env["workspaces"] / "other")})).json()
    assert await refused(supervisor.backup_tokens.mint(other["id"]))  # another project's token
    expired = supervisor.backup_tokens.mint(pid)
    key = supervisor.backup_tokens._key(expired)
    supervisor.backup_tokens._tokens[key] = (pid, time.monotonic() - 1)
    assert await refused(expired)
    revoked = supervisor.backup_tokens.mint(pid)
    supervisor.backup_tokens.revoke(revoked)
    assert await refused(revoked)

    # Nothing can be rewritten or deleted through a push, even with a good token.
    git(source, "checkout", "-q", "main")
    git(source, "commit", "-q", "--amend", "-m", "rewritten")
    forced = await asyncio.to_thread(git, source, "push", "--force", url, "main:refs/heads/main", env=push_env(token), check=False)
    assert forced.returncode != 0
    deleted = await asyncio.to_thread(git, source, "push", url, ":refs/heads/feature", env=push_env(token), check=False)
    assert deleted.returncode != 0 and "refs/heads/feature" in bare_refs(env, pid)

    # Only pushes: fetching from a backup (upload-pack) is refused, token or not.
    fetched = await asyncio.to_thread(git, source, "fetch", url, env=push_env(token), check=False)
    assert fetched.returncode != 0
    assert (await api.get(f"/api/git/{pid}.git/info/refs", params={"service": "git-upload-pack"},
                          headers={"Authorization": f"Bearer {token}"})).status_code == 403
    assert (await api.get(f"/api/git/{pid}.git/HEAD", headers={"Authorization": f"Bearer {token}"})).status_code != 200


async def test_backend_limits(api, env, project, tmp_path):
    pid = project["id"]
    await asyncio.to_thread(gitbackup.ensure_repo, env["settings"].git_dir, pid)
    small = gitbackup.HttpBackend(env["settings"].git_dir, max_body=1000, timeout=30)

    async def body():
        for _ in range(10):
            yield b"0" * 500
    status, _, _ = await small.run("POST", f"/{pid}.git/git-receive-pack", "", {"content-type":
                                   "application/x-git-receive-pack-request"}, body())
    assert status == 413
    assert gitbackup.parse_cgi(b"Status: 403 Forbidden\r\nContent-Type: text/plain\r\n\r\nno")[:1] == (403,)
    # accd's own body limit lets pushes through with their own (larger) limit, and nothing else
    token = api.app.state.supervisor.backup_tokens.mint(pid)
    big = b"x" * (5 << 20)
    answer = await api.post(f"/api/git/{pid}.git/git-receive-pack", content=big,
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-git-receive-pack-request"})
    assert answer.status_code != 413
    assert (await api.post("/api/projects", content=big, headers={"Content-Type": "application/json"})).status_code == 413


# ------------------------------------------------------------------ the broker op


def test_broker_op_never_puts_the_token_in_argv(env, tmp_path, monkeypatch):
    source = make_repo(env["workspaces"] / "demo")
    seen = []
    real = subprocess.run

    def spy(args, *a, **kw):
        seen.append((list(args), dict(kw.get("env") or {})))
        if "push" in args:
            return subprocess.CompletedProcess(args, 0, "To x\n \trefs/heads/main:refs/heads/main\t[up to date]\nDone\n", "")
        return real(args, *a, **kw)
    monkeypatch.setattr(ops.subprocess, "run", spy)
    token = "T0ken_" + "x" * 40
    result = ops.git_backup(env["broker"], str(source), "https://acc.example.lan/api/git/7.git", token,
                            ca_pem="-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
    assert result["branches"] == 1 and result["kept"] == []
    pushes = [(args, e) for args, e in seen if "push" in args]
    assert pushes
    for args, e in seen:
        assert not any(token in a for a in args)
    args, e = pushes[0]
    assert f"Authorization: Bearer {token}" in e.values()  # URL-scoped configuration, never argv
    assert e["GIT_CONFIG_GLOBAL"] == "/dev/null" and e["GIT_TERMINAL_PROMPT"] == "0"
    assert "--no-verify" in args and "core.hooksPath" in e.values()
    assert any(k.startswith("GIT_CONFIG_KEY") and v == "http.sslCAInfo" for k, v in e.items())
    assert not any("ca.pem" in a for a in args)
    with pytest.raises(ops.OpError):
        ops.git_backup(env["broker"], str(source), "file:///etc/passwd", token)
    with pytest.raises(ops.OpError):
        ops.git_backup(env["broker"], str(source), "https://acc.example.lan/api/git/7.git", "short")
    with pytest.raises(ops.OpError):
        ops.git_backup(env["broker"], "/etc", "https://acc.example.lan/api/git/7.git", token)


# ------------------------------------------------------------------ accd's schedule, end to end


async def test_backup_pushes_only_new_commits_and_keeps_rewrites(api, env, project, served, monkeypatch):
    supervisor = api.app.state.supervisor
    source = make_repo(env["workspaces"] / "demo", branches=("main", "feature"))
    git(source, "tag", "v1")
    pid = project["id"]
    pushes = []
    real_fresh = supervisor.broker.fresh

    def counting(*a, **kw):
        pushes.append(a)
        return real_fresh(*a, **kw)
    monkeypatch.setattr(supervisor.broker, "fresh", counting)

    status = await supervisor.backup_project(pid)
    assert not status.get("error"), status
    assert len(pushes) == 1 and status["last_at"]
    refs = bare_refs(env, pid)
    assert {"refs/heads/main", "refs/heads/feature", "refs/tags/v1"} <= set(refs)
    assert not supervisor.backup_tokens._tokens  # the push's token is gone with it

    await supervisor.backup_project(pid)  # nothing new: no push
    assert len(pushes) == 1
    head = commit(source)
    await supervisor.backup_project(pid)
    assert len(pushes) == 2 and bare_refs(env, pid)["refs/heads/main"] == head

    # Rewritten upstream (an amend, then a force push to GitHub): kept as backup/<time>/main, not lost or refused
    git(source, "commit", "-q", "--amend", "-m", "rewritten")
    status = await supervisor.backup_project(pid)
    assert not status.get("error"), status
    refs = bare_refs(env, pid)
    assert refs["refs/heads/main"] == head  # the old history stays where it was
    kept = [r for r in refs if r.startswith("refs/heads/backup/") and r.endswith("/main")]
    assert len(kept) == 1 and refs[kept[0]] == git(source, "rev-parse", "HEAD").stdout.strip()
    assert status["kept"][0]["ref"] == "refs/heads/main" and status["kept"][0]["as"] == kept[0]
    with api.app.state.db() as s:
        assert s.query(Event).filter_by(kind="git_backup_kept").count() == 1

    # A branch deleted in the project stays in the backup
    git(source, "branch", "-D", "feature")
    await supervisor.backup_project(pid)
    assert "refs/heads/feature" in bare_refs(env, pid)

    view = (await api.get(f"/api/projects/{pid}/backup")).json()
    assert view["enabled"] and view["configured"] and "digest" not in view
    assert {b["name"] for b in view["repo"]["branches"]} >= {"main", "feature"} and view["repo"]["size"] > 0
    assert view["repo"]["tags"] == 1
    host = (await api.get("/api/host")).json()
    assert host["git_backup"]["repos"] == 1 and host["git_backup"]["bytes"] > 0


async def test_schedule_interval_stops_and_the_switch(api, env, project, served, monkeypatch):
    supervisor = api.app.state.supervisor
    make_repo(env["workspaces"] / "demo")
    pid = project["id"]
    calls = []

    async def fake(project_id, force=False):
        calls.append(project_id)
        supervisor._save_backup(project_id, {"checked_at": time.time()})
    monkeypatch.setattr(supervisor, "backup_project", fake)
    await supervisor._backup_round(set())
    assert calls == [pid]
    await supervisor._backup_round(set())  # checked a moment ago: not due for 15 minutes
    assert calls == [pid]
    session = (await api.post("/api/sessions", json={"project_id": pid, "provider": "claude", "name": "s"})).json()

    async def idle():
        return (await api.get(f"/api/sessions/{session['id']}")).json()["observed_state"] == "idle"
    await wait_for(idle)
    supervisor._backup_due.clear()
    await api.post(f"/api/sessions/{session['id']}/stop")
    assert pid in supervisor._backup_due  # a session stopped: due now
    await supervisor._backup_round({pid})
    assert calls == [pid, pid]

    off = await api.post(f"/api/projects/{pid}/backup", json={"enabled": False})
    assert off.status_code == 200 and off.json()["git_backup"] is False
    await supervisor._backup_round({pid})
    assert calls == [pid, pid]  # off: never pushed
    assert (await api.post(f"/api/projects/{pid}/backup/run")).status_code == 409
    with api.app.state.db() as s:
        assert s.query(AuditLog).filter_by(action="project.backup").one().detail == {"enabled": False}


async def test_the_schedule_starts_a_round(api, env, project, live, monkeypatch):  # noqa: F811
    supervisor = api.app.state.supervisor
    rounds = []

    async def round_(due):
        rounds.append(set(due))
    monkeypatch.setattr(supervisor, "_backup_round", round_)
    object.__setattr__(api.app.state.ctx.settings, "backup_url", "https://acc.example.lan")
    supervisor._backup_scan = -1e9
    supervisor.backup_soon(project["id"])
    await supervisor.schedule_backups()
    await supervisor._backup_task
    assert rounds == [{project["id"]}] and not supervisor._backup_due
    await supervisor.schedule_backups()  # scanned a moment ago and nothing due: no new round
    assert len(rounds) == 1


async def test_pushes_refused_while_backups_are_off(api, env, project, served):
    supervisor = api.app.state.supervisor
    source = make_repo(env["workspaces"] / "demo")
    pid = project["id"]
    await asyncio.to_thread(gitbackup.ensure_repo, env["settings"].git_dir, pid)
    await api.post(f"/api/projects/{pid}/backup", json={"enabled": False})
    token = supervisor.backup_tokens.mint(pid)
    result = await asyncio.to_thread(git, source, "push", f"{served}/api/git/{pid}.git", "main", env=push_env(token), check=False)
    assert result.returncode != 0 and not bare_refs(env, pid)


async def test_no_backups_without_an_address(api, env, project):
    """Tests' origins are plain http: with no https origin and no ACC_BACKUP_URL, backups are off."""
    assert not env["settings"].backup_base
    supervisor = api.app.state.supervisor
    await supervisor.schedule_backups()
    assert supervisor._backup_task is None
    view = (await api.get(f"/api/projects/{project['id']}/backup")).json()
    assert view["configured"] is False and view["repo"] is None


async def test_a_project_on_another_machine(api, env, served, live):  # noqa: F811
    """A node's project is pushed by that node's broker, asked through the node link."""
    key, node_id = await enroll(api)
    node = StandInNode(env["settings"].broker_socket, key, node_id)
    task = asyncio.create_task(node.run(live))

    async def connected():
        nodes = (await api.get("/api/nodes")).json()
        return nodes and nodes[0]["connected"]
    await wait_for(connected)
    source = make_repo(env["workspaces"] / "remote")
    project = (await api.post("/api/projects", json={"slug": "remote", "name": "Remote", "host": "pc",
                                                     "workspace_path": str(source)})).json()
    opened = len(node.opened)
    status = await api.app.state.supervisor.backup_project(project["id"])
    assert not status.get("error"), status
    assert len(node.opened) > opened  # git_refs and git_backup went through the node
    assert bare_refs(env, project["id"])["refs/heads/main"] == git(source, "rev-parse", "HEAD").stdout.strip()
    task.cancel()
    await wait_for(lambda: _offline(api))
    status = await api.app.state.supervisor.backup_project(project["id"], force=True)
    assert "not reachable" in status["error"]


async def _offline(api):
    nodes = (await api.get("/api/nodes")).json()
    return nodes and not nodes[0]["connected"]


# ------------------------------------------------------------------ downloads


async def test_bundle_download_rules(api, env, project, served, tmp_path, member_accounts):
    await do_setup(api, env)
    supervisor = api.app.state.supervisor
    source = make_repo(env["workspaces"] / "demo")
    pid = project["id"]
    assert (await api.get(f"/api/projects/{pid}/backup/bundle")).status_code == 404  # nothing yet
    ran = await api.post(f"/api/projects/{pid}/backup/run")  # "Back up now" (a POST: its write lock never waits on itself)
    assert ran.status_code == 200 and ran.json()["last_at"] and ran.json()["repo"]["branches"][0]["name"] == "main"
    got = await api.get(f"/api/projects/{pid}/backup/bundle")
    assert got.status_code == 200 and got.content.startswith(b"# v2 git bundle")
    assert 'filename="demo.bundle"' in got.headers["content-disposition"]
    (tmp_path / "demo.bundle").write_bytes(got.content)
    git(tmp_path, "clone", "-q", str(tmp_path / "demo.bundle"), "restored")
    assert git(tmp_path / "restored", "rev-parse", "HEAD").stdout.strip() == git(source, "rev-parse", "HEAD").stdout.strip()
    assert not list(env["settings"].data_dir.glob("bundle-*"))  # the file is removed after it's sent

    link = (await api.post("/api/users/invites", json={"role": "member", "account": "dana-agent"})).json()["link"]
    member = browser(api)
    assert (await join(member, link, "alex")).status_code == 200
    assert (await member.get(f"/api/projects/{pid}/backup/bundle")).status_code == 404  # someone else's project
    (env["workspaces"] / "theirs").mkdir()
    theirs = (await member.post("/api/projects", json={"slug": "theirs", "name": "Theirs",
                                                       "workspace_path": str(env["workspaces"] / "theirs")},
                                headers=csrf(member))).json()
    make_repo(env["workspaces"] / "theirs")
    await supervisor.backup_project(theirs["id"])
    assert (await member.get(f"/api/projects/{theirs['id']}/backup/bundle")).status_code == 200  # their own
    assert (await api.get(f"/api/projects/{theirs['id']}/backup/bundle")).status_code == 200  # and owners
    await member.aclose()
    with api.app.state.db() as s:
        downloads = s.query(AuditLog).filter_by(action="project.backup_download").all()
        assert len(downloads) == 3 and {d.target for d in downloads} == {"demo", "theirs"}


@pytest.mark.skipif(os.getuid() != 0, reason="needs root to give the repository to another account")
def test_a_repository_owned_by_someone_else_is_trusted_for_its_own_push_only(tmp_path):
    """Live on acc-host: project folders belong to their owner (casey) while git runs as the agent account, so git refused
    ("dubious ownership") once the backup left out the agent's global config. The push trusts exactly its folder."""
    repo = tmp_path / "theirs"
    repo.mkdir()
    git(repo, "init", "-q")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    for path in (repo, other):
        for item in [path, *path.rglob("*")]:
            os.lchown(item, 65534, 65534)
    check = lambda path, env: subprocess.run(["git", "-C", str(path), "rev-parse", "--git-dir"], env=env,  # noqa: E731
                                             capture_output=True, text=True)
    assert "dubious ownership" in check(repo, ops.backup_env("t" * 43)).stderr
    env = ops.backup_env("t" * 43, repo=str(repo))
    assert check(repo, env).returncode == 0
    assert "dubious ownership" in check(other, env).stderr  # only that folder, never "*"


# ------------------------------------------------------------------ X5/Q6: a backup only ever gains history

def raw_push(source, backup, *refspecs):
    """A push straight into the bare repository (local receive-pack: the same repository policy and hooks as the
    HTTP route, without the cooperative broker's refspecs)."""
    return git(source, "push", "--porcelain", str(backup), *refspecs, check=False)


def test_tags_cannot_be_replaced_or_deleted_by_a_raw_push(tmp_path):
    git_dir = tmp_path / "git"
    backup = gitbackup.ensure_repo(git_dir, 7)
    source = make_repo(tmp_path / "src")
    git(source, "tag", "v1")
    git(source, "tag", "-a", "-m", "annotated", "v2")
    assert raw_push(source, backup, "refs/heads/main", "refs/tags/v1", "refs/tags/v2").returncode == 0
    first = git(source, "rev-parse", "main").stdout.strip()
    second = commit(source)
    git(source, "tag", "-f", "v1", second)
    forced = raw_push(source, backup, "+refs/tags/v1:refs/tags/v1")
    assert forced.returncode != 0
    assert git(backup, "rev-parse", "refs/tags/v1").stdout.strip() == first
    assert raw_push(source, backup, ":refs/tags/v1").returncode != 0
    assert raw_push(source, backup, ":refs/tags/v2").returncode != 0
    refs = bare_refs({"settings": type("S", (), {"git_dir": git_dir})}, 7)
    assert "refs/tags/v1" in refs and "refs/tags/v2" in refs
    # Branches: fast-forwards only, never deleted, even with the repository's own settings turned off
    git(backup, "config", "receive.denyNonFastForwards", "false")
    git(backup, "config", "receive.denyDeletes", "false")
    assert raw_push(source, backup, "main").returncode == 0  # a fast-forward is fine
    git(source, "commit", "-q", "--amend", "-m", "rewritten")
    assert raw_push(source, backup, "+main:refs/heads/main").returncode != 0
    assert git(backup, "rev-parse", "main").stdout.strip() == second
    assert raw_push(source, backup, ":refs/heads/main").returncode != 0
    # Only branches and tags: no other namespace
    assert raw_push(source, backup, "main:refs/notes/x").returncode != 0
    assert raw_push(source, backup, "main:refs/heads/backup/x/main").returncode == 0  # a new name is fine
    # A refused ref doesn't take the others with it (the broker keeps refused ones as backup/<time>/<name>)
    git(source, "tag", "v3")
    mixed = raw_push(source, backup, "+refs/tags/v1:refs/tags/v1", "refs/tags/v3")
    assert "refs/tags/v3" in git(backup, "for-each-ref", "refs/tags").stdout
    assert "!\trefs/tags/v1:refs/tags/v1" in mixed.stdout


def test_existing_repositories_get_the_hook_and_acc_keeps_it(tmp_path):
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    old = git_dir / "3.git"
    subprocess.run(["git", "init", "-q", "--bare", str(old)], check=True)  # made before the hook existed
    for name in os.listdir(old / "hooks"):
        os.unlink(old / "hooks" / name)
    (git_dir / "notes.txt").write_text("not a repository")
    assert gitbackup.install_hooks(git_dir) == 1
    hook = old / "hooks" / gitbackup.HOOK
    assert os.access(hook, os.X_OK) and hook.read_text() == gitbackup.HOOK_SCRIPT
    hook.write_text("#!/bin/sh\nexit 0\n")  # tampered with: put back on the next ensure
    gitbackup.ensure_repo(git_dir, 3)
    assert hook.read_text() == gitbackup.HOOK_SCRIPT
    source = make_repo(tmp_path / "src")
    git(source, "tag", "v1")
    assert raw_push(source, old, "main", "refs/tags/v1").returncode == 0
    assert raw_push(source, old, ":refs/tags/v1").returncode != 0


async def test_a_backup_belongs_to_its_project_not_just_its_id(api, env, project, served):
    """An older database restored next to newer backups reuses ids: the backup at that id is another project's."""
    supervisor = api.app.state.supervisor
    source = make_repo(env["workspaces"] / "demo")
    git(source, "tag", "secret-tag")
    pid = project["id"]
    assert not (await supervisor.backup_project(pid)).get("error")
    path = bare(env, pid)
    with api.app.state.db() as s:
        assert git(path, "config", "acc.project").stdout.strip() == gitbackup.identity(s.get(Project, pid))
    assert (await api.get(f"/api/projects/{pid}/backup/bundle")).status_code == 200

    with write_db(api.app.state.db) as s:  # the id now names a different project
        other = s.get(Project, pid)
        other.slug, other.created_at = "someone-else", other.created_at + timedelta(days=3)
        s.commit()
    view = (await api.get(f"/api/projects/{pid}/backup")).json()
    assert view["repo"] is None
    assert (await api.get(f"/api/projects/{pid}/backup/bundle")).status_code == 404
    token = supervisor.backup_tokens.mint(pid)
    refs = await api.get(f"/api/git/{pid}.git/info/refs", params={"service": "git-receive-pack"},
                         headers={"Authorization": f"Bearer {token}"})
    assert refs.status_code == 404  # no pushing into the other project's backup either

    git(source, "tag", "-d", "secret-tag")
    assert not (await supervisor.backup_project(pid)).get("error")
    assert "refs/tags/secret-tag" not in bare_refs(env, pid)  # a fresh repository
    orphaned = list((env["settings"].git_dir / "orphaned").iterdir())
    assert len(orphaned) == 1 and orphaned[0].name.startswith(f"{pid}-demo-")
    assert "secret-tag" in git(orphaned[0], "tag").stdout  # kept, for the operator


def test_a_legacy_repository_is_claimed_only_by_a_project_older_than_it(tmp_path):
    git_dir = tmp_path / "git"
    path = gitbackup.ensure_repo(git_dir, 4)  # made before identities were recorded
    made = os.stat(path / "description").st_mtime
    gitbackup.ensure_repo(git_dir, 4, "first@2026-01-01T00:00:00Z", made - 86400)  # its project: claimed
    assert git(path, "config", "acc.project").stdout.strip() == "first@2026-01-01T00:00:00Z"
    assert not (git_dir / "orphaned").exists()

    gitbackup.ensure_repo(git_dir, 5)
    gitbackup.ensure_repo(git_dir, 5, "later@2026-12-01T00:00:00Z", made + 86400)  # a project made after it
    assert [p.name.split("-")[1] for p in (git_dir / "orphaned").iterdir()] == ["unknown"]
    assert git(gitbackup.repo_path(git_dir, 5), "config", "acc.project").stdout.strip() == "later@2026-12-01T00:00:00Z"


async def test_capture_barrier_blocks_push_until_complete_consistent_copy(api, env, project, served, tmp_path, monkeypatch):
    """A real HTTP push overlaps the filesystem copy; capture holds its receive lease through the copy."""
    import threading

    from acc import backup_server
    from acc.backup_server import Service
    pid = project['id']
    work = make_repo(env['workspaces'] / 'demo')
    supervisor = api.app.state.supervisor
    await supervisor.backup_project(pid, force=True)
    before = bare_refs(env, pid)['refs/heads/main']
    after = commit(work)
    copying, release = threading.Event(), threading.Event()
    original = backup_server.copy_tree
    def pause_git(src, *args, **kwargs):
        if src == env['settings'].git_dir:
            copying.set()
            assert release.wait(timeout=15)
        return original(src, *args, **kwargs)
    monkeypatch.setattr(backup_server, 'copy_tree', pause_git)
    capture = asyncio.create_task(asyncio.to_thread(Service(env['settings']).capture))
    assert await asyncio.to_thread(copying.wait, 10)
    token = supervisor.backup_tokens.mint(pid)
    push = asyncio.create_task(asyncio.to_thread(git, work, 'push', '-q', f'{served}/api/git/{pid}.git',
                                               'main', env=push_env(token)))
    try:
        await asyncio.sleep(.2)
        assert not push.done()
    finally:
        release.set()
    stage = await capture
    await push
    saved = stage / 'git' / f'{pid}.git'
    assert git(saved, 'rev-parse', 'refs/heads/main').stdout.strip() == before
    assert git(saved, 'fsck', '--full').returncode == 0
    assert bare_refs(env, pid)['refs/heads/main'] == after


async def test_inflight_receive_finishes_before_capture_copies_git(api, env, project, served, monkeypatch):
    """Capture cannot copy a repository while an already admitted receive-pack owns its lease."""
    import threading

    from acc import backup_server
    pid = project['id']
    work = make_repo(env['workspaces'] / 'demo')
    supervisor = api.app.state.supervisor
    await supervisor.backup_project(pid, force=True)
    after = commit(work)
    admitted, release = asyncio.Event(), asyncio.Event()
    copying = threading.Event()
    original_backend = gitbackup.HttpBackend._locked_run
    original_copy = backup_server.copy_tree
    async def paused(self, method, *args, **kwargs):
        if method == 'POST':
            admitted.set()
            await release.wait()
        return await original_backend(self, method, *args, **kwargs)
    def copied(src, *args, **kwargs):
        if src == env['settings'].git_dir:
            copying.set()
        return original_copy(src, *args, **kwargs)
    monkeypatch.setattr(gitbackup.HttpBackend, '_locked_run', paused)
    monkeypatch.setattr(backup_server, 'copy_tree', copied)
    token = supervisor.backup_tokens.mint(pid)
    push = asyncio.create_task(asyncio.to_thread(git, work, 'push', '-q', f'{served}/api/git/{pid}.git',
                                               'main', env=push_env(token)))
    await asyncio.wait_for(admitted.wait(), 10)
    capture = asyncio.create_task(asyncio.to_thread(backup_server.Service(env['settings']).capture))
    try:
        await asyncio.sleep(.2)
        assert not copying.is_set()
    finally:
        release.set()
    await push
    stage = await capture
    saved = stage / 'git' / f'{pid}.git'
    assert git(saved, 'rev-parse', 'refs/heads/main').stdout.strip() == after
    assert git(saved, 'fsck', '--full').returncode == 0


@pytest.mark.parametrize('kind', ['git_status', 'server_job'])
async def test_backup_status_survives_another_sqlite_writer(api, env, project, kind):
    from helpers import pause_supervisor

    await pause_supervisor(api)  # this test owns the Observation-read interleaving
    """A writer arriving after merge's read must not turn status persistence into BUSY_SNAPSHOT."""
    import sqlite3
    import threading

    from helpers import pause_supervisor
    from sqlalchemy import event

    await pause_supervisor(api)  # Only the status write under test may consume the SELECT barrier.
    engine = api.app.state.db.kw['bind']
    read, resume, writing = threading.Event(), threading.Event(), threading.Event()
    armed = True
    def after_read(connection, cursor, statement, parameters, context, many):
        nonlocal armed
        if armed and statement.lstrip().startswith('SELECT observation.'):
            armed = False
            read.set()
            assert resume.wait(timeout=5)
    def other_writer():
        with sqlite3.connect(env['settings'].data_dir / 'acc.db', timeout=5) as db:
            writing.set()
            db.execute("insert or replace into setting(key,value,updated_at) values ('backup-race','{}',CURRENT_TIMESTAMP)")
            db.commit()
    event.listen(engine, 'after_cursor_execute', after_read)
    from acc.server.backup_jobs import BackupJobs
    jobs = BackupJobs(env['settings'], api.app.state.db)
    operation = (lambda: api.app.state.supervisor._save_backup(project['id'], {'checked_at': 123})) if kind == 'git_status' else (
        lambda: jobs.store({'id': 'a' * 32, 'state': 'complete', 'at': 123}))
    save = asyncio.create_task(asyncio.to_thread(operation))
    writer = None
    try:
        assert await asyncio.to_thread(read.wait, 5)
        writer = asyncio.create_task(asyncio.to_thread(other_writer))
        assert await asyncio.to_thread(writing.wait, 5)
        await asyncio.sleep(.1)
        resume.set()
        await save
        await writer
        if kind == 'git_status':
            assert api.app.state.supervisor.backup_status(project['id'])['checked_at'] == 123
        else:
            assert jobs.jobs()[0]['state'] == 'complete'
    finally:
        resume.set()
        event.remove(engine, 'after_cursor_execute', after_read)
        await asyncio.gather(save, *([writer] if writer else []), return_exceptions=True)


async def test_bundle_download_waits_for_a_concurrent_writer_without_blocking_loop(api, env, project, served):
    from sqlalchemy import event, text

    await do_setup(api, env)
    make_repo(env["workspaces"] / "demo")
    pid = project["id"]
    assert (await api.post(f"/api/projects/{pid}/backup/run")).status_code == 200
    api.supervisor_task.cancel()
    await asyncio.gather(api.supervisor_task, return_exceptions=True)
    await asyncio.gather(*api.supervisor_threads, return_exceptions=True)
    db = api.app.state.db

    def short_timeout(connection, *_args):
        connection.exec_driver_sql("PRAGMA busy_timeout=200")

    engine = db.kw["bind"]
    event.listen(engine, "begin", short_timeout)
    try:
        with db() as writer:
            writer.execute(text("UPDATE attention_item SET title = title"))

            async def release_writer():
                await asyncio.sleep(0.05)
                writer.rollback()

            release = asyncio.create_task(release_writer())
            try:
                got = await api.get(f"/api/projects/{pid}/backup/bundle")
                assert got.status_code == 200
                assert got.content.startswith(b"# v2 git bundle")
                from acc.server.models import AuditLog
                with db() as reader:
                    audit = reader.query(AuditLog).filter_by(action="project.backup_download").one()
                    assert audit.target == "demo" and audit.detail["bytes"] == len(got.content)
            finally:
                await release
    finally:
        event.remove(engine, "begin", short_timeout)


async def test_capture_503_retries_next_round_without_failed_status(api, env, project, served):
    from helpers import pause_supervisor

    from acc.maintenance import lease
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    pid = project['id']
    make_repo(env['workspaces'] / 'demo')
    await supervisor.backup_project(pid, force=True)
    previous = supervisor.backup_status(pid)
    commit(env['workspaces'] / 'demo')
    token = supervisor.backup_tokens.mint(pid)
    with lease(env['settings'].data_dir / '.maintenance.lock'):
        # Real git must see 503 rather than a server traceback/500.
        pushed = await asyncio.to_thread(git, env['workspaces'] / 'demo', 'push',
                                        f'{served}/api/git/{pid}.git', 'main', env=push_env(token), check=False)
        assert pushed.returncode != 0 and '503' in pushed.stderr
        result = await asyncio.to_thread(ops.git_backup, env['broker'], str(env['workspaces'] / 'demo'),
                                         f'{served}/api/git/{pid}.git', token)
        assert result.get('retry') is True
    # Exercise the real supervisor result boundary (the broker uses this same operation).
    class BusyClient:
        async def call(self, *args, **kwargs):
            return result
        async def close(self):
            pass
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(supervisor.broker, 'fresh', lambda *a, **k: BusyClient())
        deferred = await supervisor.backup_project(pid, force=True)
    assert 'failed_at' not in deferred and 'error' not in deferred
    assert supervisor.backup_status(pid) == previous
    assert pid not in supervisor._backup_due and pid in supervisor._backup_deferred  # next scan, not every pass
    await supervisor._backup_round({pid})
    assert supervisor.backup_status(pid)['digest'] != previous['digest']


async def test_background_round_busy_at_initial_read_is_deferred(api, env, project, served, caplog):
    import logging

    from helpers import pause_supervisor

    from acc.maintenance import lease
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    caplog.set_level(logging.INFO, 'acc.supervisor')
    with lease(env['settings'].data_dir / '.maintenance.lock'):
        await supervisor._backup_round({project['id']})
    assert project['id'] in supervisor._backup_due
    assert not any(r.exc_info for r in caplog.records)
    assert len([r for r in caplog.records if 'pass skipped' in r.message]) == 1


@pytest.mark.parametrize('method,suffix', [('GET', '/info/refs?service=git-receive-pack'),
                                          ('POST', '/git-receive-pack')])
async def test_git_http_maintenance_response(api, env, project, method, suffix):
    from helpers import pause_supervisor

    from acc.maintenance import lease
    await pause_supervisor(api)
    token = api.app.state.supervisor.backup_tokens.mint(project['id'])
    with lease(env['settings'].data_dir / '.maintenance.lock'):
        response = await api.request(method, f"/api/git/{project['id']}.git{suffix}",
                                     headers={'Authorization': f'Bearer {token}'})
    assert response.status_code == 503
    assert response.headers['retry-after'] == '1'
    assert 'busy with a backup or maintenance task' in response.json()['detail']


@pytest.mark.parametrize('status', [401, 403, 500, 503])
def test_broker_defers_only_temporary_http_unavailability(env, monkeypatch, status):
    source = make_repo(env['workspaces'] / 'demo')
    real = subprocess.run
    def run(args, *a, **kw):
        if 'push' in args:
            return subprocess.CompletedProcess(args, 128, '', f'fatal: The requested URL returned error: {status}')
        return real(args, *a, **kw)
    monkeypatch.setattr(ops.subprocess, 'run', run)
    args = (env['broker'], str(source), 'https://acc.example.lan/api/git/7.git', 'x' * 43)
    if status == 503:
        assert ops.git_backup(*args) == {'retry': True}
    else:
        with pytest.raises(ops.OpError, match=f'error: {status}'):
            ops.git_backup(*args)


async def test_public_git_admission_busy_is_503(api, project, monkeypatch):
    from acc.server.db import MaintenanceBusy
    def busy(*args):
        raise MaintenanceBusy('Backup capture or maintenance is busy; retry shortly')
    monkeypatch.setattr(api.app.state.ctx.edge, 'check', busy)
    response = await api.get(f"/api/git/{project['id']}.git/info/refs?service=git-receive-pack",
                             headers={'X-Acc-Edge-Client': 'c' * 64})
    assert response.status_code == 503
    assert response.headers['retry-after'] == '1'
    assert 'busy with a backup or maintenance task' in response.json()['detail']


async def test_maintenance_during_push_preparation_does_not_record_failure(api, env, project, served, monkeypatch, caplog):
    import logging

    from helpers import pause_supervisor

    from acc.server.db import MaintenanceBusy
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    make_repo(env['workspaces'] / 'demo')
    def busy(*a):
        raise MaintenanceBusy('busy')
    monkeypatch.setattr(gitbackup, 'public_backup_origin', busy)
    caplog.set_level(logging.INFO, 'acc.supervisor')
    await supervisor._backup_round({project['id']})
    assert supervisor.backup_status(project['id']) == {}
    assert project['id'] in supervisor._backup_due
    assert not any(r.exc_info or r.levelno >= logging.WARNING for r in caplog.records)


async def test_a_503_that_persists_becomes_a_recorded_failure(api, env, project, served, caplog):
    """Review 2026-10-25: any 503 (no HA authority, a proxy, a stuck lock) deferred the backup forever, retried every
    supervisor pass, and never showed on the project."""
    import logging

    from helpers import pause_supervisor

    from acc.server import backups
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    pid = project['id']
    make_repo(env['workspaces'] / 'demo')
    await supervisor.backup_project(pid, force=True)
    good = supervisor.backup_status(pid)
    commit(env['workspaces'] / 'demo')

    class Unavailable:
        async def call(self, *args, **kwargs):
            return {'retry': True}
        async def close(self):
            pass
    caplog.set_level(logging.INFO, 'acc.supervisor')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(supervisor.broker, 'fresh', lambda *a, **k: Unavailable())
        assert 'failed_at' not in await supervisor.backup_project(pid, force=True)  # a capture's few milliseconds
        assert supervisor.backup_status(pid) == good
        supervisor._backup_deferred[pid] -= backups.DEFER_LIMIT
        failed = await supervisor.backup_project(pid, force=True)
    assert failed['failed_at'] and '503' in failed['error'] and failed['digest'] == good['digest']
    assert supervisor.backup_status(pid)['error'] == failed['error']
    assert any(r.levelno == logging.WARNING and '503' in r.message for r in caplog.records)
    recovered = await supervisor.backup_project(pid, force=True)
    assert 'error' not in recovered and recovered['digest'] != good['digest'] and pid not in supervisor._backup_deferred
@pytest.mark.parametrize("path", ["", "/info/refs", "/git-receive-pack"])
@pytest.mark.parametrize("key, want", [("sslVerify", "true"), ("proxy", ""), ("followRedirects", "false"),
                                       ("sslCAInfo", "/etc/acc/ca.pem")])
def test_repository_config_cannot_override_push_protections_by_url_path(tmp_path, path, key, want):
    """Security review 7, SR7-3: git prefers the longest matching http.<url> key, so a repository's own
    http.<url>/git-receive-pack.* used to beat the push environment's http.<url>.*."""
    url = "https://acc.example.lan/api/git/5.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for suffix in ("", "/info/refs", "/git-receive-pack"):
        for name, value in (("sslVerify", "false"), ("proxy", "http://agent-proxy.example:8080"),
                            ("followRedirects", "true"), ("sslCAInfo", "/tmp/agent-ca.pem")):
            subprocess.run(["git", "-C", str(repo), "config", f"http.{url}{suffix}.{name}", value], check=True)
    env = {**ops.backup_env("t" * 43, ca_file="/etc/acc/ca.pem", repo=str(repo), url=url), "HOME": str(tmp_path)}
    got = subprocess.run(["git", "-C", str(repo), "config", "--get-urlmatch", f"http.{key}", url + path],
                         env=env, capture_output=True, text=True)
    assert got.stdout.strip() == want, (key, path, got.stdout, got.stderr)
