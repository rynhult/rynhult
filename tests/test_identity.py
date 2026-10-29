"""Identity (prerelease reviews X1, D7, D9, Q1): a username is one person for good. What the bearer token creates
belongs to "@api", which nobody can sign up as; names ACC uses itself are reserved in any case."""
from __future__ import annotations

import pytest
from helpers import TOTP_AT, verified_post, write_db
from sqlalchemy import select
from test_auth import ORIGIN, PASSWORD, do_setup
from test_users import browser, csrf, join

from acc.server import auth
from acc.server.models import Project, PushSubscription, Session, User, VaultKey

API_PRINCIPAL = "@api"


async def invite(api, role="member", account: str | None = "dana-agent") -> str:
    body = {"role": role, "account": account}
    response = await (verified_post(api, "/api/users/invites", body) if role == "owner" else api.post("/api/users/invites", json=body))
    assert response.status_code == 201, response.text
    return response.json()["link"]


async def test_token_rows_belong_to_a_name_nobody_can_sign_up_as(api, env, project, member_accounts):
    await do_setup(api, env)
    with api.app.state.db() as s:
        assert s.get(Project, project["id"]).owner == API_PRINCIPAL
    for name, status in (("api", 422), ("API", 422), ("Supervisor", 422), ("Casey", 409)):
        async with browser(api) as person:
            joined = await join(person, await invite(api), name)
            assert joined.status_code == status, (name, joined.text)
    async with browser(api) as person:
        assert (await join(person, await invite(api), "dana")).status_code == 200
        assert (await person.get("/api/projects")).json() == []
        assert (await person.get(f"/api/projects/{project['id']}/knowledge")).status_code == 404


async def test_setup_refuses_a_reserved_name(api, env):
    token = env["settings"].setup_token_path.read_text().strip()
    anon = {**ORIGIN, "Authorization": ""}
    secret = (await api.post("/api/auth/setup/begin", json={"setup_token": token}, headers=anon)).json()["totp_secret"]
    done = await api.post("/api/auth/setup/complete", headers=anon, json={
        "setup_token": token, "username": "Api", "password": PASSWORD, "code": auth.totp(secret, TOTP_AT)})
    assert done.status_code == 422, done.text


async def test_owner_only_pushes_reach_the_token_but_not_a_person_called_api(api, env):
    sent = []
    supervisor = api.app.state.supervisor

    class Push:
        async def send(self, endpoint, p256dh, key, payload):
            sent.append(endpoint)
            return 201
    supervisor.push = Push()
    with write_db(api.app.state.db) as s:
        s.add(User(username="api", role="member", password_hash="x", totp_secret="x"))  # signed up before 0023
        s.add_all([PushSubscription(endpoint=f"https://fcm.googleapis.com/{name}", p256dh="k", auth="a", username=name,
                                    label=name) for name in (API_PRINCIPAL, "api")])
        s.commit()
    await supervisor.notify({"title": "owners only"}, everyone=False)
    assert sent == [f"https://fcm.googleapis.com/{API_PRINCIPAL}"]


def test_migration_moves_the_tokens_rows_to_the_sentinel(tmp_path):
    from alembic import command
    from sqlalchemy import text

    from acc.server.db import alembic_config, make_engine, migrate
    url = f"sqlite:///{tmp_path / 'acc.db'}"
    migrate(url)
    command.downgrade(alembic_config(url), "0022")
    engine = make_engine(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO project (slug, name, host, workspace_path, import_mode, trusted, archived, account, "
                        "owner, created_at) VALUES ('a', 'A', 'local', '/w/a', 'managed', 1, 0, 'x', 'api', '2026-10-22'), "
                        "('b', 'B', 'local', '/w/b', 'managed', 1, 0, 'x', 'dana', '2026-10-22')"))
        db.execute(text("INSERT INTO setting (key, value, updated_at) VALUES ('budget.api', '{}', '2026-10-22')"))
    command.upgrade(alembic_config(url), "head")
    with engine.begin() as db:
        assert dict(db.execute(text("SELECT slug, owner FROM project")).all()) == {"a": API_PRINCIPAL, "b": "dana"}
        assert db.execute(text("SELECT key FROM setting")).scalars().all() == ["budget.@api"]


async def member_with_things(api, env, name="dana"):
    """A member with a project, a private and a shared key, a phone and a session (launched by the owner, then
    theirs)."""
    async with browser(api) as person:
        assert (await join(person, await invite(api), name)).status_code == 200
        made = await person.post("/api/projects", headers=csrf(person), json={
            "slug": f"{name}-project", "name": "Theirs", "workspace_path": str(env["workspaces"] / "demo")})
        assert made.status_code == 201, made.text
        for key, shared in (("PRIVATE_TOKEN", False), ("SHARED_TOKEN", True)):
            added = await person.post("/api/vault/keys", headers=csrf(person),
                                      json={"name": key, "variable": key, "value": "example-value-123", "shared": shared})
            assert added.status_code == 201, added.text
    with write_db(api.app.state.db) as s:
        user = s.scalar(select(User).where(User.username == name))
        s.add(PushSubscription(endpoint=f"https://fcm.googleapis.com/{name}", p256dh="k", auth="a", username=name,
                               label="phone"))
        s.commit()
        return user.id, made.json()["id"]


@pytest.mark.parametrize("resources", ["transfer", "archive"])
async def test_a_removed_name_is_retired_and_nothing_passes_to_a_newcomer(api, env, project, member_accounts, resources):
    await do_setup(api, env)
    user_id, project_id = await member_with_things(api, env)
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "x"})).json()
    with write_db(api.app.state.db) as s:
        s.get(Session, session["id"]).owner = "dana"
        s.commit()
    removed = await api.post(f"/api/users/{user_id}/remove", json={"resources": resources})
    assert removed.status_code == 200, removed.text
    assert removed.json()["projects"] == 1 and removed.json()["keys"] == 2
    with api.app.state.db() as s:
        theirs = s.get(Project, project_id)
        keys = {k.variable: k.owner for k in s.scalars(select(VaultKey))}
        assert s.scalars(select(PushSubscription).where(PushSubscription.username == "dana")).all() == []
        if resources == "transfer":
            assert theirs.owner == API_PRINCIPAL and not theirs.archived
            assert s.get(Session, session["id"]).owner == API_PRINCIPAL
            assert keys == {"PRIVATE_TOKEN": API_PRINCIPAL, "SHARED_TOKEN": API_PRINCIPAL}
        else:
            assert theirs.owner == "dana" and theirs.archived
            assert s.get(Session, session["id"]).desired_state == "archived"
            assert keys == {"SHARED_TOKEN": API_PRINCIPAL}  # the private one is gone; others still use the shared one
    for name in ("dana", "Dana"):
        async with browser(api) as newcomer:
            joined = await join(newcomer, await invite(api), name)
            assert joined.status_code == 409, joined.text
    async with browser(api) as newcomer:
        assert (await join(newcomer, await invite(api), "dana2")).status_code == 200
        assert (await newcomer.get(f"/api/projects/{project_id}/knowledge")).status_code == 404


async def test_removal_needs_a_decision_about_their_things(api, env, member_accounts):
    await do_setup(api, env)
    user_id, _ = await member_with_things(api, env)
    assert (await api.post(f"/api/users/{user_id}/remove")).status_code == 422
    assert (await api.post(f"/api/users/{user_id}/remove", json={"resources": "keep"})).status_code == 422


def test_migration_retires_names_that_own_rows_without_a_user(tmp_path):
    from alembic import command
    from sqlalchemy import text

    from acc.server.db import alembic_config, make_engine, migrate
    url = f"sqlite:///{tmp_path / 'acc.db'}"
    migrate(url)
    command.downgrade(alembic_config(url), "0023")
    engine = make_engine(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO user (username, password_hash, totp_secret, created_at, role) "
                        "VALUES ('casey', 'x', 'x', '2026-10-22', 'owner')"))
        db.execute(text("INSERT INTO project (slug, name, host, workspace_path, import_mode, trusted, archived, account, "
                        "owner, created_at) VALUES ('a', 'A', 'local', '/w/a', 'managed', 1, 0, 'x', 'departed', '2026-10-22'), "
                        "('b', 'B', 'local', '/w/b', 'managed', 1, 0, 'x', 'casey', '2026-10-22'), "
                        "('c', 'C', 'local', '/w/c', 'managed', 1, 0, 'x', '@api', '2026-10-22')"))
    command.upgrade(alembic_config(url), "head")
    with engine.begin() as db:
        assert db.execute(text("SELECT username FROM retired_user")).scalars().all() == ["departed"]


async def test_a_member_never_runs_as_the_owners_or_someone_elses_account(api, env, member_accounts):
    """Prerelease review D10: a member on the default (owners') account, or one another person has, could work in
    their checkouts and use their logins."""
    await do_setup(api, env)
    refused = await api.post("/api/users/invites", json={"role": "member", "account": "agent"})
    assert refused.status_code == 422 and "owners' agent account" in refused.text
    assert (await api.post("/api/users/invites", json={"role": "member"})).status_code == 422  # no default for members
    first, second = await invite(api), await invite(api)  # two links for one account: only the first can be used
    async with browser(api) as a, browser(api) as b:
        assert (await join(a, first, "alice")).status_code == 200
        late = await join(b, second, "bob")
        assert late.status_code == 409 and "own" in late.text
    shared = await api.post("/api/users/invites", json={"role": "member", "account": "dana-agent"})
    assert shared.status_code == 422 and "Someone else" in shared.text
    owner_on_members = await verified_post(api, "/api/users/invites", {"role": "owner", "account": "dana-agent"})
    assert owner_on_members.status_code == 422
    assert (await verified_post(api, "/api/users/invites", {"role": "owner"})).status_code == 201  # owners share the default


async def test_an_owner_on_the_default_account_cant_be_demoted(api, env, member_accounts):
    await do_setup(api, env)
    async with browser(api) as other:
        assert (await join(other, await invite(api, role="owner", account=None), "sam")).status_code == 200
    users = {u["username"]: u for u in (await api.get("/api/users")).json()["users"]}
    demoted = await api.post(f"/api/users/{users['sam']['id']}/role", json={"role": "member"})
    assert demoted.status_code == 409 and "owners' agent account" in demoted.text
    async with browser(api) as own:
        assert (await join(own, await invite(api, role="owner", account="spare-agent"), "kim")).status_code == 200
    kim = next(u for u in (await api.get("/api/users")).json()["users"] if u["username"] == "kim")
    assert (await api.post(f"/api/users/{kim['id']}/role", json={"role": "member"})).status_code == 200
