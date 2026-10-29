"""Key vault (docs/VAULT.md): who may do what, values that never come back out, delivery at launch and resume, other
machines, redaction, duplicate variables, and the move of the catalog's MCP secrets into the vault."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from helpers import pause_supervisor, wait_for, write_db
from test_auth import do_setup, live  # noqa: F401 (fixture)
from test_multihost import StandInNode, enroll
from test_ownership import all_routes
from test_supervisor import get, state_is
from test_users import browser, csrf, join

from acc.server import vault
from acc.server.app import create_app
from acc.server.models import VaultKey

REPO = Path(__file__).resolve().parents[1]
CANARY = "sk-canary-7f3a9c1e5b2d4f60aa"  # a value that must never come back out
OTHER = "ghp_secondcanary0123456789abc"
SHORT = "abc123"  # under MIN_REDACT: left alone by redaction
PREFIX = 10  # a prefix this short is left alone (vault.PREFIX_MIN)


# ---------------------------------------------------------------------- the cipher and the key file

def test_round_trip_and_associated_data(tmp_path):
    cipher = vault.Cipher(tmp_path / "secrets" / "vault.key")
    sealed = cipher.seal("00112233aabbccdd", "OPENAI_API_KEY", CANARY, create=True)
    assert CANARY not in sealed and CANARY.encode() not in base64.b64decode(sealed)
    assert cipher.open("00112233aabbccdd", "OPENAI_API_KEY", sealed) == CANARY
    assert cipher.seal("00112233aabbccdd", "OPENAI_API_KEY", CANARY) != sealed  # a fresh nonce every time
    with pytest.raises(vault.VaultError):  # moved to another row
        cipher.open("ffeeddccbbaa9988", "OPENAI_API_KEY", sealed)
    with pytest.raises(vault.VaultError):  # or to another variable
        cipher.open("00112233aabbccdd", "GITHUB_TOKEN", sealed)
    raw = bytearray(base64.b64decode(sealed))
    raw[-1] ^= 1
    with pytest.raises(vault.VaultError):  # changed
        cipher.open("00112233aabbccdd", "OPENAI_API_KEY", base64.b64encode(bytes(raw)).decode())
    key_file = tmp_path / "secrets" / "vault.key"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert len(base64.b64decode(key_file.read_text().strip())) == 32
    with pytest.raises(vault.VaultError):  # another data key can't read it
        vault.Cipher(tmp_path / "other.key").open("00112233aabbccdd", "OPENAI_API_KEY", sealed)


def test_key_file_is_only_created_for_an_empty_vault(tmp_path, caplog):
    path = tmp_path / "vault.key"
    with pytest.raises(vault.VaultError, match="missing"):
        vault.Cipher(path).key()  # reading never creates one
    with caplog.at_level(logging.WARNING, logger="acc.vault"):
        vault.Cipher(path).key(create=True)
    assert "created a new data key" in caplog.text and path.exists()
    path.write_text("not a key")
    with pytest.raises(vault.VaultError, match="not a vault key"):
        vault.Cipher(path).key()


def test_variables():
    assert vault.check_variable("OPENAI_API_KEY") == "OPENAI_API_KEY"
    assert vault.check_variable("_PRIVATE_TOKEN") == "_PRIVATE_TOKEN"
    for bad in ("openai", "1KEY", "A-B", "X" * 65, "", "PATH", "HOME", "ACC_SESSION_ID", "ACC_ANYTHING", "LD_PRELOAD",
                "LC_ALL", "SHELL", "_", "RYNHULT_RUN_DIR", "RYNHULT_SESSION_ID", "RYNHULT_GUARD_FILES", "RYNHULT_ANYTHING"):
        with pytest.raises(vault.VaultError):
            vault.check_variable(bad)


def test_env_parsing():
    text = "\n".join([
        "# my keys", "", "OPENAI_API_KEY=sk-plain-123  # a comment", "export GITHUB_TOKEN='ghp $literal # not a comment'",
        'MULTI="line one\\nline \\"two\\""', 'PEM="-----BEGIN-----', "abc", '-----END-----"', "lower=nope", "PATH=/bin",
        "EMPTY=", "OPENAI_API_KEY=second", "not an assignment", 'OPEN="never closed',
    ])
    entries = {(e.variable, e.line): e for e in vault.parse_env(text)}
    ok = {e.variable: e.value for e in entries.values() if e.problem is None}
    assert ok == {"OPENAI_API_KEY": "sk-plain-123", "GITHUB_TOKEN": "ghp $literal # not a comment",
                  "MULTI": 'line one\nline "two"', "PEM": "-----BEGIN-----\nabc\n-----END-----"}
    problems = {(v, line): e.problem for (v, line), e in entries.items() if e.problem}
    assert set(problems) == {("lower", 9), ("PATH", 10), ("EMPTY", 11), ("OPENAI_API_KEY", 12), ("OPEN", 14)}
    assert "earlier" in problems[("OPENAI_API_KEY", 12)] and "quote" in problems[("OPEN", 14)]


def test_key_creation_is_atomic_and_heals_an_empty_key(tmp_path, monkeypatch):
    """Review D-low: O_EXCL then write meant a crash between the two left an empty vault.key that blocked the vault."""
    path = tmp_path / "secrets" / "vault.key"
    real_link = vault.os.link

    def crash(*args, **kwargs):
        raise KeyboardInterrupt  # the process dies before the key is in place
    monkeypatch.setattr(vault.os, "link", crash)
    with pytest.raises(KeyboardInterrupt):
        vault.Cipher(path).key(create=True)
    assert not path.exists() and list(path.parent.iterdir()) == []  # nothing half-written left behind
    monkeypatch.setattr(vault.os, "link", real_link)
    assert len(vault.Cipher(path).key(create=True)) == 32
    path.write_text("")  # an empty key from an older version's crash, while the vault is empty
    assert len(vault.Cipher(path).key(create=True)) == 32
    with pytest.raises(vault.VaultError):  # never while values may need it
        path.write_text("")
        vault.Cipher(path).key()


def test_install_makes_the_key_once_and_never_while_the_database_has_keys(tmp_path):
    script = [sys.executable, "-I", str(REPO / "deploy/lib/vault-key")]
    path, database = tmp_path / "vault.key", tmp_path / "acc.db"
    made = subprocess.run([*script, str(path), str(database)], capture_output=True, text=True)
    assert made.returncode == 0 and "created" in made.stdout and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(base64.b64decode(path.read_text().strip())) == 32
    first = path.read_text()
    assert subprocess.run([*script, str(path), str(database)]).returncode == 0 and path.read_text() == first
    path.unlink()  # acc.db restored without secrets/: it holds encrypted keys
    with sqlite3.connect(database) as db:
        db.execute("create table vault_key (id text)")
        db.execute("insert into vault_key values ('k1')")
    refused = subprocess.run([*script, str(path), str(database)], capture_output=True, text=True)
    assert refused.returncode == 1 and "restore vault.key" in refused.stderr and not path.exists()
    assert list(tmp_path.iterdir()) == [database]


def test_redactor_finds_encoded_and_truncated_forms():
    """Review D-low: base64, Basic-auth, percent-encoded and cut-short forms were stored unredacted."""
    import urllib.parse
    secret = "pa ss/wo+rd=" + CANARY  # characters that percent- and base64-encoding change
    redact = vault.Redactor([(secret, "Work")])
    mark = "«key: Work»"
    for user in ("u", "us", "use", "alice"):  # every byte alignment inside the credential
        basic = base64.b64encode(f"{user}:{secret}".encode()).decode()
        out = redact.text(f"Authorization: Basic {basic}")
        assert mark in out and len(out) < len(f"Authorization: Basic {basic}") - 20, (user, out)
    for encoded in (base64.b64encode(secret.encode()).decode(), base64.urlsafe_b64encode(secret.encode()).decode(),
                    urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)):
        assert encoded not in redact.text(f"x={encoded}&y=1") and mark in redact.text(f"x={encoded}&y=1")
    truncated = secret[:len(secret) // 2 + 1]
    assert redact.text(f"token {truncated}… (truncated)") == f"token {mark}… (truncated)"
    assert redact.text(f"{secret[:PREFIX]}") == secret[:PREFIX]  # a short common prefix (like sk-) stays
    line = json.dumps({"text": f"key {secret}"})
    assert json.loads(vault.Redactor([(secret, 'Say "hi"')]).text(line, json_escaped=True)) == {"text": 'key «key: Say "hi"»'}


def test_redactor():
    redact = vault.Redactor([(CANARY, "OpenAI (work)"), (CANARY[:20], "Prefix"), (SHORT, "Short")])
    assert redact.text(f"Bearer {CANARY} and {SHORT}") == f"Bearer «key: OpenAI (work)» and {SHORT}"  # longest first
    assert redact.deep({"a": [CANARY], CANARY: 1}) == {"a": ["«key: OpenAI (work)»"], "«key: OpenAI (work)»": 1}
    assert not vault.Redactor([(SHORT, "Short")])


# ---------------------------------------------------------------------- API helpers

async def add(client, name, variable, value=CANARY, member=False, **extra):
    response = await client.post("/api/vault/keys", json={"name": name, "variable": variable, "value": value, **extra},
                                 headers=csrf(client) if member else None)
    assert response.status_code == 201, response.text
    return response.json()


async def call(client, path, body=None, member=True):
    return await client.post(path, json=body or {}, headers=csrf(client) if member else None)


async def make_project(client, env, slug, member=True, host="local"):
    (env["workspaces"] / slug).mkdir(exist_ok=True)
    response = await client.post("/api/projects", json={"slug": slug, "name": slug.title(), "host": host,
                                                         "workspace_path": str(env["workspaces"] / slug)},
                                 headers=csrf(client) if member else None)
    assert response.status_code == 201, response.text
    trusted = await client.post(f"/api/projects/{response.json()['id']}/trust", headers=csrf(client) if member else None)
    assert trusted.status_code == 200, trusted.text
    return response.json()


@pytest.fixture
async def people(api, env, member_accounts):
    """The ACC owner (the bearer token, "@api"), and two members, alice and bob, each with a project."""
    await do_setup(api, env)
    members = {}
    for name in ("alice", "bob"):
        link = (await api.post("/api/users/invites", json={"role": "member", "account": f"{name}-agent"})).json()["link"]
        client = browser(api)
        assert (await join(client, link, name)).status_code == 200
        members[name] = client
    projects = {name: await make_project(client, env, f"p-{name}") for name, client in members.items()}
    yield {"owner": api, **members, "projects": projects}
    for client in members.values():
        await client.aclose()


def audit(api, action):
    async def check():
        return [a for a in (await api.get("/api/audit", params={"action": action})).json()]
    return check


# ---------------------------------------------------------------------- who can do what

async def test_permission_table(people):
    owner, alice, bob, projects = people["owner"], people["alice"], people["bob"], people["projects"]
    shared = await add(alice, "OpenAI (work)", "OPENAI_API_KEY", member=True, shared=True, note="team")
    private = await add(alice, "Mine only", "MY_TOKEN", OTHER, member=True)
    assert shared["mine"] and shared["owner"] == "alice" and "value" not in shared and "ciphertext" not in shared

    # See that it exists: the owner yes; others only when shared (ACC's owners included)
    for client, expected in ((alice, {shared["id"], private["id"]}), (bob, {shared["id"]}), (owner, {shared["id"]})):
        assert {k["id"] for k in (await client.get("/api/vault/keys")).json()} == expected
    # Give it to projects they can launch in
    pa, pb = projects["alice"]["id"], projects["bob"]["id"]
    assert (await call(alice, f"/api/vault/keys/{private['id']}/give", {"project_id": pa})).status_code == 200
    assert (await call(bob, f"/api/vault/keys/{shared['id']}/give", {"project_id": pb})).status_code == 200
    assert (await call(bob, f"/api/vault/keys/{private['id']}/give", {"project_id": pb})).status_code == 404
    assert (await call(bob, f"/api/vault/keys/{shared['id']}/give", {"project_id": pa})).status_code == 404  # not his
    assert (await call(owner, f"/api/vault/keys/{shared['id']}/give", {"project_id": pa}, member=False)).status_code == 200
    assert (await call(owner, f"/api/vault/keys/{private['id']}/give", {"project_id": pb}, member=False)).status_code == 404
    # Replace, rename, change sharing: its owner only (403 when shared, 404 when they can't see it)
    for path, body in (("/api/vault/keys/{id}", {"name": "x"}), ("/api/vault/keys/{id}/replace", {"value": "new value 123"}),
                       ("/api/vault/keys/{id}", {"shared": False}), ("/api/vault/keys/{id}", {"remote": True})):
        assert (await call(bob, path.format(id=shared["id"]), body)).status_code == 403
        assert (await call(bob, path.format(id=private["id"]), body)).status_code == 404
        assert (await call(owner, path.format(id=shared["id"]), body, member=False)).status_code == 403
        assert (await call(owner, path.format(id=private["id"]), body, member=False)).status_code == 404
    renamed = await call(alice, f"/api/vault/keys/{shared['id']}", {"name": "OpenAI (team)", "remote": True})
    assert renamed.status_code == 200 and renamed.json()["name"] == "OpenAI (team)" and renamed.json()["remote"]
    assert (await call(alice, f"/api/vault/keys/{shared['id']}/replace", {"value": CANARY + "2"})).status_code == 200
    # Project pages: what's given there and what you could give, only what you can see
    alice_view = (await alice.get(f"/api/projects/{pa}/keys")).json()
    assert {k["id"] for k in alice_view["keys"]} == {private["id"], shared["id"]}
    owner_view = (await owner.get(f"/api/projects/{pa}/keys")).json()
    assert {k["id"] for k in owner_view["keys"]} == {shared["id"]}  # alice's private key stays hers
    assert (await bob.get(f"/api/projects/{pa}/keys")).status_code == 404
    # Take back, delete
    assert (await call(bob, f"/api/vault/keys/{shared['id']}/take", {"project_id": pb})).status_code == 200
    assert (await call(bob, f"/api/vault/keys/{shared['id']}/remove")).status_code == 403
    assert (await call(bob, f"/api/vault/keys/{private['id']}/remove")).status_code == 404
    # An owner can delete any key (someone leaves), audited as such
    counts = {row["owner"]: row["keys"] for row in (await owner.get("/api/vault/people")).json()}
    assert counts == {"alice": 2}
    assert (await bob.get("/api/vault/people")).status_code == 403
    assert (await call(owner, f"/api/vault/keys/{private['id']}/remove", member=False)).status_code == 200
    deleted = await audit(owner, "vault.delete")()
    assert deleted[0]["detail"]["as_owner"] and deleted[0]["detail"]["owner"] == "alice"
    assert (await call(alice, f"/api/vault/keys/{shared['id']}/remove")).status_code == 200
    actions = {a["action"] for a in (await owner.get("/api/audit", params={"action": "vault."})).json()}
    assert {"vault.add", "vault.give", "vault.take", "vault.rename", "vault.remote", "vault.replace",
            "vault.delete"} <= actions
    assert CANARY not in json.dumps((await owner.get("/api/audit")).json())


async def test_bad_keys_are_refused(api):
    for body, words in (({"variable": "PATH"}, "own variables"), ({"variable": "lower"}, "capital"),
                        ({"value": "   "}, "empty")):
        response = await api.post("/api/vault/keys", json={"name": "X", "variable": "X_KEY", "value": "v", **body})
        assert response.status_code == 422 and words in response.text, response.text
    response = await api.post("/api/vault/keys", json={"name": "X", "variable": "X_KEY", "value": "v" * (vault.MAX_VALUE + 1)})
    assert response.status_code == 422


async def test_env_paste_preview_never_echoes_values(api):
    text = f"# keys\nexport OPENAI_API_KEY='{CANARY}'\nGITHUB_TOKEN={OTHER}\nPATH=/bin\n"
    preview = await api.post("/api/vault/import/preview", json={"text": text})
    assert preview.status_code == 200 and CANARY not in preview.text and OTHER not in preview.text
    entries = preview.json()["entries"]
    assert [(e["variable"], e["problem"] is None) for e in entries] == [("OPENAI_API_KEY", True), ("GITHUB_TOKEN", True),
                                                                          ("PATH", False)]
    added = await api.post("/api/vault/import", json={"text": text, "pick": ["OPENAI_API_KEY", "GITHUB_TOKEN"],
                                                       "names": {"OPENAI_API_KEY": "OpenAI"}, "shared": True})
    assert added.status_code == 201 and CANARY not in added.text
    assert [(k["name"], k["variable"], k["shared"]) for k in added.json()["added"]] == [
        ("OpenAI", "OPENAI_API_KEY", True), ("GITHUB_TOKEN", "GITHUB_TOKEN", True)]
    again = (await api.post("/api/vault/import/preview", json={"text": text})).json()["entries"]
    assert again[0]["exists"]  # you already have a key with this variable
    refused = await api.post("/api/vault/import", json={"text": text, "pick": ["PATH"]})
    assert refused.status_code == 422


# ---------------------------------------------------------------------- nothing returns a value

FILL = {"project_id": None, "acc_id": None, "key_id": None, "username": "alice", "name:path": "x.png", "name": "codex",
        "kind": "project", "key": None, "item_id": "1", "request_id": "pub-0", "device_id": "1", "user_id": "1",
        "invite_id": "1", "profile_id": "frontend", "flow_id": "agentx.flow", "provider": "claude",
        "session_id": "0123456789abcdef", "port": "8000", "endpoint_id": "local", "folder": "nothing", "action": "install"}


async def test_no_route_returns_a_value(people, env, caplog):
    owner, alice, projects = people["owner"], people["alice"], people["projects"]
    caplog.set_level(logging.DEBUG)
    mine = await add(alice, "Alice's OpenAI", "OPENAI_API_KEY", member=True, shared=True)
    owners = await add(owner, "Owner's GitHub", "GH_TOKEN", OTHER)
    for key, project, member in ((mine, projects["alice"], True), (owners, projects["alice"], False)):
        response = await call(owner if not member else alice, f"/api/vault/keys/{key['id']}/give",
                              {"project_id": project["id"]}, member=member)
        assert response.status_code == 200 and CANARY not in response.text
    session = (await owner.post("/api/sessions", json={"project_id": projects["alice"]["id"], "provider": "claude",
                                                       "name": "keys"})).json()
    await wait_for(state_is(owner, session["id"], "idle"))
    preview = await owner.post("/api/sessions/preview", json={"project_id": projects["alice"]["id"], "provider": "claude",
                                                             "name": "x"})
    assert sorted(k["variable"] for k in preview.json()["keys"]["keys"]) == ["GH_TOKEN", "OPENAI_API_KEY"]
    fill = {**FILL, "project_id": str(projects["alice"]["id"]), "acc_id": session["id"], "key_id": mine["id"],
            "key": str(projects["alice"]["id"])}
    routes = sorted({prefix + r.path for prefix, r in all_routes(owner.app.routes)
                     if isinstance(r, APIRoute) and "GET" in r.methods and (prefix + r.path).startswith("/api/")})
    assert "/api/vault/keys" in routes and "/api/projects/{project_id}/keys" in routes and len(routes) > 40
    checked = 0
    for path in routes:
        url = path
        for name, value in fill.items():
            url = url.replace("{" + name + "}", value or "")
        params = {"path": "README.md", "q": "sk", "after": 0}
        for client in (owner, alice):
            try:
                response = await asyncio.wait_for(client.get(url, params=params), 30)
            except asyncio.TimeoutError:
                continue  # nothing came back at all
            for value in (CANARY, OTHER):
                assert value not in response.text, (path, response.status_code)
            checked += 1
    assert checked > 60
    # nor the database (rows, launch specs, events, audit), nor the logs
    dump = "\n".join(sqlite3.connect(env["settings"].data_dir / "acc.db").iterdump())
    assert CANARY not in dump and OTHER not in dump
    assert CANARY not in caplog.text and OTHER not in caplog.text


# ---------------------------------------------------------------------- delivery

@pytest.mark.parametrize('host', ['local', 'remote'])
async def test_resume_rechecks_cached_vault_authority_after_broker_wait(env, backend_url, host):
    from sqlalchemy import create_engine, update
    from sqlalchemy.orm import Session as DbSession
    from sqlalchemy.orm import sessionmaker

    from acc.server.compiler import LaunchInputs, compile_files
    from acc.server.db import make_engine, migrate
    from acc.server.models import LaunchSpec, Project, Session, VaultGrant
    from acc.server.supervisor import Supervisor

    url = backend_url
    migrate(url)
    # Driver-managed transactions model a cached ORM authorization object independently
    # of SQLite's stronger global snapshot-write rejection.
    engine = create_engine(url) if url.startswith('sqlite:') else make_engine(url)
    factory = sessionmaker(engine, expire_on_commit=False)
    delivered = []

    def revoke_sharing():
        with DbSession(engine) as writer:
            writer.execute(update(VaultKey).where(VaultKey.id == '00112233aabbccdd').values(shared=False))
            writer.commit()

    class Broker:
        async def call(self, method, **kwargs):
            if method == 'ping':
                revoke_sharing()
                return {'home': '/home/agent', 'run_dir': '/run/agent', 'hook_path': '/bin/acc-hook'}
            assert method == 'launch'
            delivered.append(kwargs['env'])
            return {'pid': 123}

    supervisor = Supervisor(env['settings'], factory, Broker())
    inputs = LaunchInputs(provider='claude')
    compiled = compile_files(inputs, env['settings'].hook_path, env['settings'].statusline_path,
                             catalog=supervisor.catalog)
    with factory() as db:
        key = VaultKey(id='00112233aabbccdd', name='Shared', variable='WORK_TOKEN',
                       owner='alice', shared=True, remote=True,
                       ciphertext=supervisor.vault.cipher.seal('00112233aabbccdd', 'WORK_TOKEN', CANARY, create=True))
        project = Project(slug='demo', name='Demo', account='agent', workspace_path='/demo', host=host)
        session = Session(id='authority', name='Authority', project=project, owner='bob', native_id='native',
                          provider='claude', tmux_name='authority', run_as='agent',
                          launch_spec=LaunchSpec(provider='claude', inputs=inputs.as_dict(),
                                                 files=compiled.files, digest=compiled.digest))
        db.add_all([key, session])
        db.flush()
        db.add(VaultGrant(key_id=key.id, project_id=project.id, given_by='alice'))
        db.commit()
        assert vault.plan(db, project.id, 'bob', False).ids == [key.id]
        if host == 'local':
            revoke_sharing()
        await supervisor._start(db, session, reason='resume')
        assert delivered == [{}]
    engine.dispose()

def session_env(env, acc_id):
    path = env["run_dir"] / acc_id / "session.env"
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def test_session_env_can_disappear_while_being_read(tmp_path, monkeypatch):
    run = tmp_path / "session"
    run.mkdir()
    path = run / "session.env"
    path.write_text(f"export OPENAI_API_KEY={CANARY}\n")
    real_open = Path.open

    def removed(file, *args, **kwargs):
        if file == path:
            file.unlink()
        return real_open(file, *args, **kwargs)
    monkeypatch.setattr(Path, "open", removed)
    assert session_env({"run_dir": tmp_path}, "session") is None


async def test_keys_reach_session_env_and_nothing_else(api, env, project):
    key = await add(api, "OpenAI (work)", "OPENAI_API_KEY")
    await add(api, "Not given", "UNUSED_KEY", OTHER)
    assert (await api.post(f"/api/vault/keys/{key['id']}/give", json={"project_id": project["id"]})).status_code == 200
    preview = (await api.post("/api/sessions/preview", json={"project_id": project["id"], "provider": "claude",
                                                             "name": "x"})).json()
    assert preview["keys"]["keys"] == [{"id": key["id"], "name": "OpenAI (work)", "variable": "OPENAI_API_KEY"}]
    assert "`OPENAI_API_KEY`: OpenAI (work)" in preview["files"]["append-prompt.md"]
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                     "name": "keys"})).json()
    await wait_for(state_is(api, session["id"], "idle"))
    written = session_env(env, session["id"])
    assert f"export OPENAI_API_KEY={CANARY}\n" in written and OTHER not in written
    assert stat.S_IMODE((env["run_dir"] / session["id"] / "session.env").stat().st_mode) == 0o600
    prompt = (env["run_dir"] / session["id"] / "append-prompt.md").read_text()
    assert "`OPENAI_API_KEY`: OpenAI (work)" in prompt and CANARY not in prompt
    detail = await get(api, session["id"])
    assert CANARY not in json.dumps(detail)
    saved_prompt = detail["launch_spec"]["files"]["append-prompt.md"]
    assert "acc-request tool --manager" in saved_prompt and "OPENAI_API_KEY" not in saved_prompt
    events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
    command = next(e["payload"]["command"] for e in events if e["kind"] == "run_launch")
    assert CANARY not in command and "--append-system-prompt-file" in command
    used = await wait_for(audit(api, "vault.use"))
    assert used[0]["detail"]["keys"] == [key["id"]] and used[0]["target"] == session["id"]  # private: by id only
    listed = {k["id"]: k for k in (await api.get("/api/vault/keys")).json()}
    assert listed[key["id"]]["last_used_at"] and listed[key["id"]]["projects"][0]["id"] == project["id"]

    # Taken from the project, then a resume: the next run doesn't have it
    assert (await api.post(f"/api/vault/keys/{key['id']}/take", json={"project_id": project["id"]})).status_code == 200
    await api.post(f"/api/sessions/{session['id']}/stop")
    await wait_for(state_is(api, session["id"], "stopped"))
    await api.post(f"/api/sessions/{session['id']}/resume")
    await wait_for(state_is(api, session["id"], "idle"))
    assert CANARY not in (session_env(env, session["id"]) or "")
    events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
    resumed = [e["payload"]["command"] for e in events if e["kind"] in ("run_resume", "run_manual_resume")]
    assert resumed and "--append-system-prompt-file" in resumed[-1]  # tool policy remains, vault paragraph does not
    prompt = (env["run_dir"] / session["id"] / "append-prompt.md").read_text()
    assert "acc-request tool --manager" in prompt and "OPENAI_API_KEY" not in prompt

    # Given again and replaced: a resume gets the new value; archiving removes the file
    await api.post(f"/api/vault/keys/{key['id']}/give", json={"project_id": project["id"]})
    await api.post(f"/api/vault/keys/{key['id']}/replace", json={"value": OTHER + "-new"})
    await api.post(f"/api/sessions/{session['id']}/stop")
    await wait_for(state_is(api, session["id"], "stopped"))
    await api.post(f"/api/sessions/{session['id']}/resume")
    await wait_for(state_is(api, session["id"], "idle"))
    assert f"export OPENAI_API_KEY={OTHER}-new\n" in session_env(env, session["id"])
    await api.post(f"/api/sessions/{session['id']}/archive")
    assert session_env(env, session["id"]) is None


async def test_keys_leave_the_disk_when_a_session_stops_and_come_back_at_resume(api, env, project):
    """D21: session.env and the secret-filled MCP file stayed until archive (or forever), readable by every later
    session of the same agent account."""
    key = await add(api, "OpenAI (work)", "OPENAI_API_KEY")
    await api.post(f"/api/vault/keys/{key['id']}/give", json={"project_id": project["id"]})
    added = await api.post("/api/catalog/mcps", json={"id": "search", "command": "true", "env": {"SEARCH_TOKEN": OTHER},
                                                      "profiles": []})
    assert added.status_code in (200, 201), added.text
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "keys",
                                                     "mcps": ["search"], "skills": [], "agents": []})).json()
    await wait_for(state_is(api, session["id"], "idle"))
    run = env["run_dir"] / session["id"]
    assert CANARY in session_env(env, session["id"]) and OTHER in (run / "mcp.json").read_text()
    await api.post(f"/api/sessions/{session['id']}/stop")
    await wait_for(state_is(api, session["id"], "stopped"))
    assert session_env(env, session["id"]) is None and not (run / "mcp.json").exists()
    assert (run / "settings.json").exists()  # the rest of the run folder stays
    await api.post(f"/api/sessions/{session['id']}/resume")
    await wait_for(state_is(api, session["id"], "idle"))
    assert CANARY in session_env(env, session["id"]) and OTHER in (run / "mcp.json").read_text()


async def test_a_removal_that_fails_is_retried_and_survives_a_restart(api, env, project, monkeypatch):
    from acc.server.broker_client import BrokerUnavailable
    from acc.server.models import Observation
    key = await add(api, "OpenAI (work)", "OPENAI_API_KEY")
    await api.post(f"/api/vault/keys/{key['id']}/give", json={"project_id": project["id"]})
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                     "name": "keys"})).json()
    await wait_for(state_is(api, session["id"], "idle"))
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    real = supervisor.broker.call

    async def flaky(method, **params):
        if method == "clear_env":
            raise BrokerUnavailable("machine offline")
        return await real(method, **params)
    monkeypatch.setattr(supervisor.broker, "call", flaky)
    await api.post(f"/api/sessions/{session['id']}/stop")
    await wait_for(state_is(api, session["id"], "stopped"))
    assert CANARY in session_env(env, session["id"])  # couldn't be removed yet
    with api.app.state.db() as s:
        assert session["id"] in s.get(Observation, "env_cleanup").value  # recorded, so a restart remembers it
    fresh = create_app(env["settings"])
    retry_seen = asyncio.Event()
    available = False
    fresh_call = fresh.state.supervisor.broker.call

    async def offline(method, **params):
        if method == "clear_env" and not available:
            retry_seen.set()
            raise BrokerUnavailable("still offline after restart")
        return await fresh_call(method, **params)
    monkeypatch.setattr(fresh.state.supervisor.broker, "call", offline)
    task = asyncio.create_task(fresh.state.supervisor.run())
    try:
        await asyncio.wait_for(retry_seen.wait(), 15)
        assert CANARY in session_env(env, session["id"])
        with fresh.state.db() as s:
            assert session["id"] in s.get(Observation, "env_cleanup").value
        available = True

        async def gone():
            with fresh.state.db() as s:
                pending = s.get(Observation, "env_cleanup").value
            return session_env(env, session["id"]) is None and pending == {}
        assert await wait_for(gone)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fresh.state.supervisor.broker.close()


async def test_codex_gets_the_paragraph_in_its_instructions(api, env, project):
    key = await add(api, "Search API", "SEARCH_API_KEY")
    await api.post(f"/api/vault/keys/{key['id']}/give", json={"project_id": project["id"]})
    preview = (await api.post("/api/sessions/preview", json={"project_id": project["id"], "provider": "codex",
                                                             "name": "x"})).json()
    overrides = json.loads(preview["files"]["codex-overrides.json"])
    instructions = json.loads(next(o for o in overrides if o.startswith("developer_instructions="))
                              .split("=", 1)[1])
    assert "`SEARCH_API_KEY`: Search API" in instructions and CANARY not in json.dumps(preview)


async def test_two_keys_with_one_variable_refuse_the_launch(api, project):
    first = await add(api, "OpenAI (work)", "OPENAI_API_KEY")
    second = await add(api, "OpenAI (personal)", "OPENAI_API_KEY", OTHER)
    for key in (first, second):
        await api.post(f"/api/vault/keys/{key['id']}/give", json={"project_id": project["id"]})
    body = {"project_id": project["id"], "provider": "claude", "name": "x"}
    preview = await api.post("/api/sessions/preview", json=body)
    assert preview.status_code == 422 and "OpenAI (work)" in preview.text and "OpenAI (personal)" in preview.text
    launched = await api.post("/api/sessions", json=body)
    assert launched.status_code == 409 and "OpenAI (work)" in launched.text and "OpenAI (personal)" in launched.text
    assert (await api.get("/api/sessions")).json() == []  # refused before anything was recorded


async def test_launching_person_uses_only_their_own_or_shared_keys(people, env):
    owner, alice, projects = people["owner"], people["alice"], people["projects"]
    private = await add(alice, "Alice's", "ALICE_KEY", member=True)
    await call(alice, f"/api/vault/keys/{private['id']}/give", {"project_id": projects["alice"]["id"]})
    body = {"project_id": projects["alice"]["id"], "provider": "claude", "name": "x"}
    assert (await alice.post("/api/sessions/preview", json=body, headers=csrf(alice))).json()["keys"]["keys"] != []
    assert (await owner.post("/api/sessions/preview", json=body)).json()["keys"]["keys"] == []


async def test_keys_on_another_machine_only_when_allowed(api, env, live):  # noqa: F811
    key, node_id = await enroll(api)
    node = StandInNode(env["settings"].broker_socket, key, node_id)
    task = asyncio.create_task(node.run(live))
    try:
        async def connected():
            nodes = (await api.get("/api/nodes")).json()
            return nodes if nodes and nodes[0]["connected"] else None
        await wait_for(connected)
        project = await make_project(api, env, "remote", member=False, host="pc")
        allowed = await add(api, "Travels", "TRAVEL_KEY", remote=True)
        kept = await add(api, "Stays home", "HOME_ONLY_KEY", OTHER)
        for k in (allowed, kept):
            await api.post(f"/api/vault/keys/{k['id']}/give", json={"project_id": project["id"]})
        preview = (await api.post("/api/sessions/preview", json={"project_id": project["id"], "provider": "claude",
                                                                 "name": "x"})).json()
        assert [k["name"] for k in preview["keys"]["keys"]] == ["Travels"]
        assert [k["name"] for k in preview["keys"]["held_back"]] == ["Stays home"]
        session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                         "name": "far"})).json()
        await wait_for(state_is(api, session["id"], "idle"), timeout=20)
        written = session_env(env, session["id"])  # the stand-in node relays to the test broker: same run folder
        assert f"export TRAVEL_KEY={CANARY}\n" in written and OTHER not in written
        used = (await audit(api, "vault.use")())[0]["detail"]
        assert used["keys"] == [allowed["id"]] and used["held_back"] == [kept["id"]]  # private: by id only
        await api.post(f"/api/sessions/{session['id']}/archive")  # removed from that machine with the session
        assert session_env(env, session["id"]) is None
    finally:
        task.cancel()


# ---------------------------------------------------------------------- redaction

def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


async def test_redaction_in_events_activity_history_changes_and_export(api, env, project):
    # Drive ingest and history explicitly: a speculative read before the fake CLI creates its
    # transcript legitimately backs off for a minute. This test covers redaction, not that schedule.
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    workspace = env["workspaces"] / "demo"
    git(workspace, "init", "-q", "-b", "main")
    (workspace / "app.py").write_text("print('hi')\n")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "start")
    key = await add(api, "OpenAI (work)", "OPENAI_API_KEY")
    short = await add(api, "Tiny", "TINY_KEY", SHORT)
    for k in (key, short):
        await api.post(f"/api/vault/keys/{k['id']}/give", json={"project_id": project["id"]})
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                     "name": "leaky"})).json()
    async def started():
        await supervisor.ingest_pass()
        return await state_is(api, session["id"], "idle")()
    idle = await wait_for(started)
    acc_id, native_id = session["id"], idle["native_id"]
    placeholder = "«key: OpenAI (work)»"

    # What the agent does and says
    (workspace / "config.py").write_text(f"KEY = '{CANARY}'\n")
    hook = {"hook_event_name": "PreToolUse", "session_id": native_id, "tool_name": "Bash",
            "tool_input": {"command": f"curl -H 'Authorization: Bearer {CANARY}' https://api.example.com/{SHORT}"}}
    transcript = next((env["home"] / ".claude/projects").glob(f"*/{native_id}.jsonl"))
    with transcript.open("a") as handle:
        handle.write(json.dumps({"type": "user", "message": {"role": "user", "content": f"use {CANARY} please"}}) + "\n")
        handle.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"Done with {CANARY}."}]}}) + "\n")
    with (env["run_dir"] / acc_id / "events.jsonl").open("a") as spool:
        spool.write(json.dumps({"provider": "claude", "hook": hook}) + "\n")
    await supervisor.ingest_pass()
    await supervisor.index_transcripts()

    async def doing():
        s = await get(api, acc_id)
        return s if s["activity"] and "curl" in s["activity"] else None
    activity = (await wait_for(doing))["activity"]
    assert CANARY not in activity and placeholder in activity and SHORT in activity  # short values are left alone
    events = (await api.get(f"/api/sessions/{acc_id}/events")).text
    assert CANARY not in events and placeholder in events

    async def indexed():
        found = (await api.get("/api/search", params={"q": "please"})).json()["results"]
        return found or None
    hits = await wait_for(indexed)
    assert CANARY not in json.dumps(hits)
    exported = (await api.get(f"/api/sessions/{acc_id}/export")).text
    assert CANARY not in exported and f"use {placeholder} please" in exported
    changes = await api.get(f"/api/sessions/{acc_id}/changes")
    assert changes.status_code == 200 and CANARY not in changes.text and placeholder in changes.json()["diff"]
    message = await api.post(f"/api/sessions/{acc_id}/message", json={"text": f"here: {CANARY}"})
    assert message.status_code == 200
    assert CANARY not in json.dumps((await api.get("/api/audit")).json())
    dump = "\n".join(sqlite3.connect(env["settings"].data_dir / "acc.db").iterdump())
    assert CANARY not in dump


async def test_a_replaced_or_deleted_keys_old_value_stays_redacted(api, env, project):
    """D22/Q5: the running process keeps the value it was given; replacing or deleting the key in ACC used to switch
    redaction to the new value (or to nothing), so the old one reached events, history and exports in plain text."""
    from acc.server.models import Session as SessionRow
    from acc.server.models import VaultRetired
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    key = await add(api, "OpenAI (work)", "OPENAI_API_KEY")
    await api.post(f"/api/vault/keys/{key['id']}/give", json={"project_id": project["id"]})
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                     "name": "rotated"})).json()
    async def started():
        await supervisor.ingest_pass()
        return await state_is(api, session["id"], "idle")()
    idle = await wait_for(started)
    assert (await api.post(f"/api/vault/keys/{key['id']}/replace", json={"value": OTHER})).status_code == 200
    with api.app.state.db() as s:
        redact = supervisor.vault.redactor(s, s.get(SessionRow, session["id"]).vault_keys)
    assert redact.text(f"a {CANARY} b {OTHER}") == "a «key: OpenAI (work)» b «key: OpenAI (work)»"

    # The still-running process prints the old value; it is indexed (and re-indexed later) redacted
    transcript = next((env["home"] / ".claude/projects").glob(f"*/{idle['native_id']}.jsonl"))
    with transcript.open("a") as handle:
        handle.write(json.dumps({"type": "user", "message": {"role": "user", "content": f"rotate {CANARY} now"}}) + "\n")
        handle.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}) + "\n")
    await api.post(f"/api/sessions/{session['id']}/stop")  # its state moves: history (re)indexes it
    await supervisor.index_transcripts()

    async def indexed():
        found = (await api.get("/api/search", params={"q": "rotate"})).json()["results"]
        return found or None
    assert CANARY not in json.dumps(await wait_for(indexed))
    events = (await api.get(f"/api/sessions/{session['id']}/export")).text
    assert CANARY not in events

    # Deleted: still redacted, also by a vault that starts with an empty cache (an accd restart)
    assert (await api.post(f"/api/vault/keys/{key['id']}/remove")).status_code == 200
    fresh = vault.Vault(env["settings"].vault_key_path)
    with api.app.state.db() as s:
        redact = fresh.redactor(s, s.get(SessionRow, session["id"]).vault_keys)
        assert s.query(VaultRetired).filter_by(key_id=key["id"]).count() == 2  # both values it had
        assert CANARY not in json.dumps([r.ciphertext for r in s.query(VaultRetired)])  # sealed, not plain
    assert redact.text(f"{CANARY} {OTHER}") == "«key: OpenAI (work)» «key: OpenAI (work)»"


async def test_a_key_no_session_had_keeps_no_old_values(api, env, project):
    from acc.server.models import VaultRetired
    key = await add(api, "Unused", "UNUSED_KEY")
    await api.post(f"/api/vault/keys/{key['id']}/replace", json={"value": OTHER})
    await api.post(f"/api/vault/keys/{key['id']}/remove")
    with api.app.state.db() as s:
        assert s.query(VaultRetired).count() == 0


# ---------------------------------------------------------------------- the catalog's MCP secrets

def test_mcp_secrets_move_into_the_vault(env):
    local = env["settings"].catalog_local_dir
    local.mkdir(parents=True)
    (local / "local.json").write_text(json.dumps({"mcps": [{
        "id": "search", "name": "Search", "command": "true", "args": [], "providers": ["claude", "codex"],
        "env": {"SEARCH_TOKEN": CANARY, "region": "eu-west-1", "EMPTY": ""}, "added_by": "api", "origin": "local"}]}))
    app = create_app(env["settings"])
    data = json.loads((local / "local.json").read_text())
    env_after = data["mcps"][0]["env"]
    assert CANARY not in (local / "local.json").read_text()
    assert vault.REF.match(env_after["SEARCH_TOKEN"]) and vault.REF.match(env_after["region"]) and env_after["EMPTY"] == ""
    with app.state.db() as s:
        key = s.get(VaultKey, vault.REF.match(env_after["SEARCH_TOKEN"]).group(1))
        assert key.owner == "@api" and key.shared and not key.remote and key.variable == "SEARCH_TOKEN"
        assert app.state.supervisor.vault.value(key) == CANARY
        region = s.get(VaultKey, vault.REF.match(env_after["region"]).group(1))
        assert region.variable == "REGION"
    create_app(env["settings"])  # once: a second start finds only references
    assert json.loads((local / "local.json").read_text()) == data


async def test_a_launch_with_a_migrated_mcp_gets_the_real_value(api, env, project):
    added = await api.post("/api/catalog/mcps", json={"id": "search", "command": "true", "env": {"SEARCH_TOKEN": CANARY},
                                                       "profiles": []})
    assert added.status_code == 200, added.text
    stored = json.loads((env["settings"].catalog_local_dir / "local.json").read_text())
    assert CANARY not in json.dumps(stored) and vault.REF.match(stored["mcps"][0]["env"]["SEARCH_TOKEN"])
    keys = (await api.get("/api/vault/keys")).json()
    assert keys[0]["mcp"] == ["search"] and keys[0]["shared"]
    refused = await api.post(f"/api/vault/keys/{keys[0]['id']}/remove")
    assert refused.status_code == 409 and "search" in refused.text  # used by the catalog
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "m",
                                                     "mcps": ["search"], "skills": [], "agents": []})).json()
    written = env["run_dir"] / session["id"] / "mcp.json"
    await wait_for(lambda: asyncio.sleep(0, result=written.exists()))
    assert json.loads(written.read_text())["mcpServers"]["search"]["env"] == {"SEARCH_TOKEN": CANARY}
    detail = await get(api, session["id"])
    assert CANARY not in json.dumps(detail)
    # the key made private by its owner: someone else's launch with that server is refused, never handed it
    with write_db(api.app.state.db) as s:
        s.get(VaultKey, keys[0]["id"]).owner = "someone-else"
        s.get(VaultKey, keys[0]["id"]).shared = False
        s.commit()
    preview = await api.post("/api/sessions/preview", json={"project_id": project["id"], "provider": "claude", "name": "m",
                                                           "mcps": ["search"], "skills": [], "agents": []})
    assert preview.status_code == 422 and "private" in preview.text


async def test_private_key_names_stay_out_of_the_audit_log(people):
    """Prerelease review D (low): owners read the audit log; a member's private key is named only to them."""
    alice = people["alice"]
    secret = await add(alice, "Alice's bank", "BANK_TOKEN", member=True)
    shared = await add(alice, "Team search", "SEARCH_TOKEN", member=True, shared=True)
    await call(alice, f"/api/vault/keys/{secret['id']}", {"name": "Alice's other bank", "variable": "BANK_TOKEN_2"})
    log = (await people["owner"].get("/api/audit", params={"action": "vault."})).json()
    text = json.dumps([e for e in log if e["target"] == secret["id"]])
    assert "bank" not in text.lower() and "BANK_TOKEN" not in text and '"private": true' in text
    assert any(e["detail"].get("name") == "Team search" for e in log if e["target"] == shared["id"])


def test_a_launch_refused_for_someone_elses_private_key_doesnt_name_it():
    from types import SimpleNamespace as NS

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from acc.server.models import Base
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as s:
        s.add(VaultKey(id="0123456789abcdef", name="Owner's private search", variable="SEARCH", ciphertext="x", owner="casey"))
        s.commit()
        catalog = NS(mcps={"search": NS(meta={"env": {"TOKEN": vault.ref("0123456789abcdef")}})})
        with pytest.raises(vault.VaultError) as refused:
            vault.plan(s, None, "alice", False, catalog, ("search",))
        assert "private" in str(refused.value) and "Owner's private search" not in str(refused.value)


def test_installer_external_database_never_mints_a_key_from_stale_sqlite(tmp_path):
    import subprocess
    import sys

    from conftest import REPO
    path, database = tmp_path / 'vault.key', tmp_path / 'stale.db'
    command = [sys.executable, '-I', str(REPO / 'deploy/lib/vault-key'), str(path), str(database),
               '--external-database']
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert 'external database' in result.stderr
    assert not path.exists()
    assert not database.exists()
    path.write_text('existing-key')
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0
    assert path.read_text() == 'existing-key'


@pytest.mark.parametrize("prefix", ["ACC", "RYNHULT"])
@pytest.mark.parametrize("suffix", ["RUN_DIR", "SESSION_ID", "GUARD_FILES"])
async def test_protected_rename_variables_are_refused_before_node_delivery(api, prefix, suffix):
    variable = f"{prefix}_{suffix}"
    response = await api.post("/api/vault/keys", json={"name": "Protected override", "variable": variable, "value": CANARY})
    assert response.status_code == 422, response.text
    assert CANARY not in response.text
    assert all(key["variable"] != variable for key in (await api.get("/api/vault/keys")).json())


async def test_precompat_key_cannot_override_new_session_guards(api, project):
    key = await add(api, 'Earlier project key', 'PROJECT_TOKEN')
    await api.post(f"/api/vault/keys/{key['id']}/give", json={'project_id': project['id']})
    # Before this namespace was reserved, an existing row could use this spelling.
    with write_db(api.app.state.db) as db:
        db.get(VaultKey, key['id']).variable = 'RYNHULT_GUARD_FILES'
        db.commit()
    response = await api.post('/api/sessions/preview', json={'project_id': project['id'], 'provider': 'claude', 'name': 'safe'})
    assert response.status_code == 422, response.text
    assert 'RYNHULT_GUARD_FILES' in response.text and CANARY not in response.text
