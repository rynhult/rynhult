"""Local workspaces: Researcher/Council sessions without a project, and promotion to a project."""
from __future__ import annotations

import subprocess

from helpers import wait_for, write_db


async def launch(api, **body):
    return await api.post("/api/sessions", json={"provider": "claude", "name": "niche", "profile": "researcher", **body})


async def test_local_workspace_launches_without_a_project(api, env):
    preview = (await api.post("/api/sessions/preview", json={"provider": "claude", "name": "n", "profile": "researcher",
                                                             "topic": "Beyblade tournament apps"})).json()
    assert preview["standalone"] and preview["cwd"].endswith("/local/beyblade-tournament-apps-" + preview["cwd"][-8:])
    response = await launch(api, topic="Beyblade tournament apps")
    assert response.status_code == 201, response.text
    session = response.json()
    assert session["standalone"] and session["project"] == "local" and session["cwd"] == session["workdir"]
    folder = env["workspaces"] / "local" / session["workdir"].rsplit("/", 1)[1]
    assert (folder / ".git").is_dir() and (folder / "docs/research").is_dir() and (folder / "README.md").is_file()

    async def launched():
        events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
        return next((e for e in events if e["kind"] == "run_launch"), None)
    assert await wait_for(launched)
    second = (await launch(api, topic="Beyblade tournament apps")).json()
    assert second["workdir"] != session["workdir"]  # same topic, same day: a new folder

    (folder / "docs/research/2026-10-18-market.md").write_text("# Market\nWorth it.")
    knowledge = (await api.get(f"/api/sessions/{session['id']}/knowledge")).json()
    assert [d["path"].rsplit("/", 1)[1] for d in knowledge["docs"]["research"]] == ["2026-10-18-market.md"]
    doc = (await api.get(f"/api/sessions/{session['id']}/knowledge/doc", params={"path": "docs/research/2026-10-18-market.md"})).json()
    assert "Worth it." in doc["content"]


async def test_only_standalone_profiles_and_a_topic_or_project(api):
    refused = await launch(api, topic="x", profile="frontend")
    assert refused.status_code == 422 and "without a project" in refused.text
    assert (await api.post("/api/sessions", json={"provider": "claude", "name": "n", "profile": "researcher"})).status_code == 422
    catalog = (await api.get("/api/catalog")).json()
    assert {p["id"] for p in catalog["profiles"] if p.get("standalone")} == {"researcher", "council"}


async def test_promote_locally_then_refuse_twice(api, env):
    session = (await launch(api, topic="local only")).json()
    promoted = await api.post(f"/api/sessions/{session['id']}/promote", json={"name": "Local only", "slug": "only-local", "github": False})
    assert promoted.status_code == 201, promoted.text
    assert promoted.json()["workspace_path"] == session["workdir"] and promoted.json()["repo"] is None
    again = await api.post(f"/api/sessions/{session['id']}/promote", json={"name": "Again", "slug": "again", "github": False})
    assert again.status_code == 409


class FakeGitHub:
    def __init__(self):
        self.created, self.protected = [], []

    async def create_repo(self, name, description, ruleset_path, auto_init=True, protect=True):
        assert auto_init is False and protect is False  # push first, protect after
        self.created.append(name)
        return {"full_name": f"casey/{name}", "html_url": "u", "clone_url": "c"}

    async def apply_ruleset(self, full_name, ruleset_path):
        self.protected.append(full_name)


async def test_promote_to_github_pushes_history_then_protects(api, env, monkeypatch, tmp_path):
    for key, value in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(key, value)
    fake = FakeGitHub()
    monkeypatch.setattr(type(api.app.state.supervisor), "github", property(lambda self: fake))
    session = (await launch(api, topic="worth it")).json()
    remote = tmp_path / "remotes" / "casey" / "worth-it.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    unconfirmed = await api.post(f"/api/sessions/{session['id']}/promote", json={"name": "Worth it", "slug": "worth-it"})
    assert unconfirmed.status_code == 409 and fake.created == []
    promoted = await api.post(f"/api/sessions/{session['id']}/promote", json={"name": "Worth it", "slug": "worth-it", "confirm": True})
    assert promoted.status_code == 201, promoted.text
    assert promoted.json()["repo"] == "casey/worth-it" and fake.protected == ["casey/worth-it"]
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline", "main"], capture_output=True, text=True).stdout
    assert "Local workspace promoted to a project" in log
    audit = (await api.get("/api/audit")).json()
    assert any(a["action"] == "workspace.promote" and a["detail"]["repo"] == "casey/worth-it" for a in audit)


async def test_second_session_shares_the_workspace_and_workspaces_lists_it(api, env):
    first = (await launch(api, topic="Shared folder")).json()
    folder = first["workdir"].rsplit("/", 1)[1]
    council = await api.post("/api/sessions", json={"provider": "claude", "name": "verdict", "profile": "council", "workspace": folder})
    assert council.status_code == 201, council.text
    assert council.json()["workdir"] == first["workdir"]
    missing = await api.post("/api/sessions", json={"provider": "claude", "name": "x", "profile": "council", "workspace": "nope-20260101"})
    assert missing.status_code == 404
    (env["workspaces"] / "local" / folder / "docs/council/2026-10-18-verdict.md").write_text("# Verdict")
    workspaces = (await api.get("/api/workspaces")).json()
    entry = next(w for w in workspaces if w["folder"] == folder)
    assert [x["profile"] for x in entry["sessions"]] == ["researcher", "council"]
    assert entry["docs"]["council"] == 1 and entry["project"] is None


async def test_delete_workspace_archives_sessions_and_removes_the_folder(api, env):
    session = (await launch(api, topic="Dead end")).json()
    folder = session["workdir"].rsplit("/", 1)[1]
    path = env["workspaces"] / "local" / folder

    async def launched():
        return (await api.get(f"/api/sessions/{session['id']}")).json()["observed_state"] not in ("starting",)
    assert await wait_for(launched)
    unconfirmed = await api.post(f"/api/workspaces/{folder}/delete", json={"confirm": "something-else"})
    assert unconfirmed.status_code == 409 and path.is_dir()
    deleted = await api.post(f"/api/workspaces/{folder}/delete", json={"confirm": folder})
    assert deleted.status_code == 200, deleted.text
    assert not path.exists()
    after = (await api.get(f"/api/sessions/{session['id']}")).json()
    assert after["desired_state"] == "archived" and after["workdir"] is None
    assert folder not in {w["folder"] for w in (await api.get("/api/workspaces")).json()}
    audit = (await api.get("/api/audit")).json()
    assert any(a["action"] == "workspace.delete" and a["target"] == folder for a in audit)
    assert (await api.post(f"/api/workspaces/{folder}/delete", json={"confirm": folder})).status_code == 404


async def test_promoted_workspace_cannot_be_deleted(api, env):
    session = (await launch(api, topic="Keeper")).json()
    folder = session["workdir"].rsplit("/", 1)[1]
    assert (await api.post(f"/api/sessions/{session['id']}/promote", json={"name": "Keeper", "slug": "keeper", "github": False})).status_code == 201
    refused = await api.post(f"/api/workspaces/{folder}/delete", json={"confirm": folder})
    assert refused.status_code == 409 and "project" in refused.text
    assert (env["workspaces"] / "local" / folder).is_dir()


async def test_worktree_with_a_local_workspace_is_refused_before_anything_is_made(api, env):
    """R2: a worktree flag left over from project mode is refused by preview and launch, and no folder is made."""
    from acc.server.models import Project
    body = {"provider": "claude", "name": "n", "profile": "researcher", "topic": "Stray folder", "worktree": True}
    preview = await api.post("/api/sessions/preview", json=body)
    assert preview.status_code == 422 and "Own worktree is for project sessions" in preview.text
    for _ in range(2):  # a retry makes nothing either
        refused = await api.post("/api/sessions", json=body)
        assert refused.status_code == 422 and "Own worktree is for project sessions" in refused.text
    local = env["workspaces"] / "local"
    assert not local.exists() or not any(local.iterdir())
    with api.app.state.db() as s:
        assert s.query(Project).filter(Project.slug == "local").count() == 0
    first = (await launch(api, topic="Kept")).json()
    reuse = await api.post("/api/sessions", json={"provider": "claude", "name": "n", "profile": "researcher",
                                                 "workspace": first["workdir"].rsplit("/", 1)[1], "worktree": True})
    assert reuse.status_code == 422
    assert (await api.post("/api/sessions/preview", json={"provider": "claude", "name": "n", "profile": "researcher",
                                                         "workspace": first["workdir"].rsplit("/", 1)[1],
                                                         "worktree": True})).status_code == 422


async def test_the_built_in_slugs_are_reserved(api, env):
    """Prerelease review X2: nobody may take "local" or "local-<account>" before its account's first workspace."""
    for slug in ("local", "local-agent", "local-someone-agent"):
        made = await api.post("/api/projects", json={"slug": slug, "name": "Mine", "workspace_path": str(env["workspaces"] / "demo")})
        assert made.status_code == 422, made.text
    assert (await api.post("/api/projects/import", json={"repo": "org/local", "slug": "local-x"})).status_code == 422
    session = (await launch(api, topic="reserved")).json()
    promoted = await api.post(f"/api/sessions/{session['id']}/promote", json={"name": "L", "slug": "local-x", "github": False})
    assert promoted.status_code == 422


async def test_a_preclaimed_or_mismatched_built_in_project_is_refused(api, env):
    """A project that took the slug before the reservation (or a built-in one that isn't this account's, here, in its
    folder) is never used: the launch is refused before any folder is made."""
    from acc.server.models import Project
    with write_db(api.app.state.db) as s:
        s.add(Project(slug="local", name="Preclaimed", owner="attacker", account="attacker-agent", host="hostile-node",
                      workspace_path="/example/attacker", trusted=True))
        s.commit()
    refused = await launch(api, topic="secret plans")
    assert refused.status_code == 409, refused.text
    assert not (env["workspaces"] / "local").exists() or not list((env["workspaces"] / "local").iterdir())
    with write_db(api.app.state.db) as s:
        row = s.query(Project).filter_by(slug="local").one()
        row.builtin, row.account, row.host = "local", "agent", "local"  # this account's, but another folder
        s.commit()
    assert (await launch(api, topic="secret plans")).status_code == 409


def test_ensure_project_checks_owner_account_host_and_folder(tmp_path):
    from pathlib import Path

    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from acc.server.local_workspace import LocalProjectError, ensure_project
    from acc.server.models import Base, Project
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(engine, expire_on_commit=False)
    root = Path("/example/owner-workspaces")
    with db() as s:
        mine = ensure_project(s, root, "owner-agent", "administrator", "owner-agent", lambda p: True)
        assert (mine.slug, mine.builtin, mine.owner, mine.host) == ("local", "local", "administrator", "local")
        assert ensure_project(s, root, "owner-agent", "administrator", "owner-agent", lambda p: True).id == mine.id
        theirs = ensure_project(s, Path("/example/m"), "member-agent", "member", "owner-agent", lambda p: True)
        assert theirs.slug == "local-member-agent" and theirs.id != mine.id
        with pytest.raises(LocalProjectError):  # not the caller's
            ensure_project(s, root, "owner-agent", "member", "owner-agent", lambda p: p.owner == "member")
        with pytest.raises(LocalProjectError):  # another folder
            ensure_project(s, Path("/example/elsewhere"), "owner-agent", "administrator", "owner-agent", lambda p: True)
        s.add(Project(slug="local-new-agent", name="Preclaimed", owner="attacker", account="attacker-agent",
                      host="hostile-node", workspace_path="/example/attacker", trusted=True))
        s.flush()
        with pytest.raises(LocalProjectError):  # the slug is held by a project that isn't built in
            ensure_project(s, Path("/example/n"), "new-agent", "newcomer", "owner-agent", lambda p: True)


def test_migration_marks_built_in_projects_and_moves_preclaims_aside(tmp_path):
    from alembic import command
    from sqlalchemy import text

    from acc.server.db import alembic_config, make_engine, migrate
    url = f"sqlite:///{tmp_path / 'acc.db'}"
    migrate(url)
    command.downgrade(alembic_config(url), "0024")
    engine = make_engine(url)
    note = "Built in: local workspaces (one folder each, local git only)."
    with engine.begin() as db:
        rows = [("local", "Local workspaces", "local", "/w/local", "a-agent", note),
                ("local-b-agent", "Local workspaces", "local", "/w/b/local", "b-agent", note),
                ("local-c-agent", "Preclaimed", "pc", "/w/x", "x-agent", "mine")]
        for slug, name, host, path, account, notes in rows:
            db.execute(text("INSERT INTO project (slug, name, host, workspace_path, import_mode, trusted, archived, "
                            "account, notes, created_at) VALUES (:slug, :name, :host, :path, 'managed', 1, 0, :account, "
                            ":notes, '2026-10-22')"), dict(slug=slug, name=name, host=host, path=path, account=account,
                                                            notes=notes))
    command.upgrade(alembic_config(url), "head")
    with engine.begin() as db:
        got = dict(db.execute(text("SELECT slug, builtin FROM project")).all())
        assert got == {"local": "local", "local-b-agent": "local", "renamed-local-c-agent-3": None}
        assert db.execute(text("SELECT action FROM audit_log")).scalars().all() == ["project.renamed"]


async def test_promoted_workspace_still_accepts_a_second_standalone_session(api):
    session = (await launch(api, topic='Shared research')).json()
    promoted = await api.post(f"/api/sessions/{session['id']}/promote", json={
        'name': 'Shared research', 'slug': 'shared-research', 'github': False})
    assert promoted.status_code == 201, promoted.text
    body = {'provider': 'claude', 'name': 'Council', 'profile': 'council',
            'workspace': session['workdir'].rsplit('/', 1)[1]}
    preview = await api.post('/api/sessions/preview', json=body)
    assert preview.status_code == 200, preview.text
    second = await api.post('/api/sessions', json=body)
    assert second.status_code == 201, second.text
    assert second.json()['workdir'] == session['workdir']
