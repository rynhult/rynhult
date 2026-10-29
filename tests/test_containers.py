"""Per-session containers, accd's and the broker's side (docs/CONTAINER-LAUNCHER.md).

The root helper is replaced by a fake that speaks its protocol on a socket and records every request; the unit's
terminal relay by a fake that runs the fake CLI with the grant's arguments on a pty, so a container session goes through
the real launch, attach client, hooks and stop paths without Podman, systemd or root."""
from __future__ import annotations

import dataclasses
import json
import os
import pty
import selectors
import shutil
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
from helpers import wait_for, write_db
from isolated_settings import isolated_broker
from test_auth import do_setup
from test_changes import git, repo
from test_supervisor import state_is
from test_users import browser, csrf, join

from acc.broker import containers as broker_containers
from acc.broker import ops
from acc.broker.config import BrokerConfig
from acc.server import containers
from acc.server.models import AuditLog, Project

MEMBER = {"cpus": 2, "memory_mib": 4096, "pids": 512, "disk_gib": 20, "sessions": 4}
NONE = {"cpus": None, "memory_mib": None, "pids": None, "disk_gib": None, "sessions": None}


FRAME = struct.Struct(">cI")


class FakeRelay:
    """Plays the execution guard's relay for one generation: the grant's fake CLI on a pty, in its checkout."""

    def __init__(self, path: Path, grant: Path, providers: dict, unit: str):
        self.grant, self.providers, self.unit = grant, providers, unit
        self.child, self.stopped, self.lock = None, False, threading.Lock()
        path.parent.mkdir(mode=0o700)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        grant = json.loads(self.grant.read_text())
        fake = self.providers[grant["provider"]]
        master, secondary = pty.openpty()
        # What the launcher's `podman run --env` gives the container
        environment = {**os.environ, "ACC_RUN_DIR": grant["run_dir"], "ACC_SESSION_ID": grant["session_id"],
                       "ACC_GUARD_FILES": grant["guard_files"]}
        with self.lock:
            if self.stopped:
                conn.close()
                os.close(master)
                os.close(secondary)
                return
            child = self.child = subprocess.Popen(
                [fake, *grant["args"]], cwd=grant["checkout"], stdin=secondary, stdout=secondary, stderr=secondary,
                start_new_session=True, env=environment)
        os.close(secondary)
        events = selectors.DefaultSelector()
        events.register(conn, selectors.EVENT_READ)
        events.register(master, selectors.EVENT_READ)
        pending = b""
        try:
            with conn:
                while True:
                    for key, _ in events.select():
                        if key.fileobj == master:
                            try:
                                data = os.read(master, 65536)
                            except OSError:
                                data = b""
                            if not data:
                                child.wait()
                                conn.sendall(FRAME.pack(b"x", 0))
                                return
                            conn.sendall(FRAME.pack(b"d", len(data)) + data)
                            continue
                        data = conn.recv(65536)
                        if not data:
                            return
                        pending += data
                        while len(pending) >= FRAME.size and len(pending) >= FRAME.size + FRAME.unpack_from(pending)[1]:
                            kind, length = FRAME.unpack_from(pending)
                            payload, pending = pending[FRAME.size:FRAME.size + length], pending[FRAME.size + length:]
                            if kind == b"d":
                                os.write(master, payload)
        except (BrokenPipeError, ConnectionResetError):
            if not self.stopped:
                raise
        finally:
            events.close()
            os.close(master)

    def close(self):
        """What stopping the launcher unit does to its relay's CLI. The CLI has its own session, outside every tmux
        server, so nothing else ends it: it was reparented to init and left running after the test."""
        with self.lock:
            if self.stopped:  # a launcher stop closed it already; FakeLauncher.close() closes every relay again
                return
            self.stopped = True
            child = self.child
        self.sock.shutdown(socket.SHUT_RDWR)  # wakes a waiting accept(): close() alone doesn't
        self.sock.close()
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)  # its own group (start_new_session), so its children too
            except ProcessLookupError:
                pass
            child.wait(timeout=10)
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("fake relay did not stop its terminal thread")


class FakeLauncher:
    """Speaks acc-containers' protocol: one JSON line in, one out. `answers` maps an operation to a reply override.
    A launch also starts a FakeRelay where the unit's relay would listen."""

    def __init__(self, path: Path, attach_root: Path | None = None, grants: Path | None = None,
                 providers: dict | None = None):
        self.path, self.requests, self.answers, self.relays = path, [], {}, []
        self.closed = False
        self.attach_root, self.grants, self.providers = attach_root, grants, providers
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                request = json.loads(conn.makefile().readline())
                self.requests.append(request)
                op = request["operation"]
                answer = {"ok": True, "code": {"launch": "launched", "stop": "stopped"}.get(op, "status"),
                          "state": {"launch": "running", "stop": "stopped"}.get(op, "running"),
                          "unit": f"acc-ct-{request['session_id']}-g{request['generation']}.service"}
                answer.update(self.answers.get(op, {}))
                answer["request_id"] = request["request_id"]
                if op == "launch" and answer.get("ok") and self.attach_root is not None:
                    socket_path = broker_containers.attach_socket(self.attach_root, request["session_id"],
                                                                  request["generation"])
                    relay = FakeRelay(socket_path, self.grants / f"{request['session_id']}.json",
                                      self.providers, answer["unit"])
                    self.relays.append(relay)
                    if self.closed:
                        relay.close()
                if op == "stop" and answer.get("ok"):
                    for relay in self.relays:
                        if relay.unit == answer["unit"]:
                            relay.close()
                conn.sendall(json.dumps(answer).encode() + b"\n")

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.sock.shutdown(socket.SHUT_RDWR)  # wakes a waiting accept(): close() alone doesn't
        self.sock.close()
        try:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError("fake launcher did not stop its request thread")
        finally:
            for relay in self.relays:
                relay.close()


@pytest.fixture
def containers_env(env, tmp_path, monkeypatch):
    """Containers on for this ACC: the grant folder, a fake launcher and its fake terminal relays. The fake relays run
    in this test process, so the pane is told to expect this process's cgroup instead of the launcher unit's."""
    own = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
    monkeypatch.setattr(broker_containers, "unit_cgroup", lambda session_id, generation: own)
    root = tmp_path / "containers"
    for sub in ("grants", "audit"):
        (root / sub).mkdir(parents=True)
    attach_root = Path(tempfile.mkdtemp(prefix="ct-", dir="/tmp"))  # a socket path must fit 108 bytes
    launcher = FakeLauncher(tmp_path / "ct.sock", attach_root, root / "grants", env["broker"].providers)
    env["broker"] = dataclasses.replace(env["broker"], containers_socket=launcher.path,
                                        containers_attach_root=attach_root)
    env["settings"] = dataclasses.replace(env["settings"], containers_enabled=True, containers_attach_qualified=True,
                                          containers_dir=root)
    qualified_host(tmp_path / "qualified", monkeypatch)  # the Preview gate: this test host has a passing record
    yield launcher
    launcher.close()
    shutil.rmtree(attach_root)


def qualified_host(folder, monkeypatch):
    """This test host has recorded a passing qualification report: the real record and check, with the test's own uid
    standing in for root and its folder for / (tests/test_container_host_record.py)."""
    from acc import container_host_record as hr
    (folder / "etc/acc/containers").mkdir(parents=True)
    for part in (folder, folder / "etc", folder / "etc/acc", folder / "etc/acc/containers"):
        part.chmod(0o755)
    (folder / "etc/machine-id").write_text("a" * 32 + "\n")
    report = folder / "report.json"
    report.write_text(json.dumps({"version": 1, "status": "go", "go_phase_1": True}))
    args = dict(machine_id=folder / "etc/machine-id", root_uid=os.getuid(), top=folder)
    record = folder / "etc/acc/containers/host-qualified.json"
    hr.record(report, path=record, **args)
    real = hr.check
    monkeypatch.setattr(hr, "check", lambda path=None: real(record, **args))
    return record


async def container_project(api, env, project):
    repo(env["workspaces"] / "demo")
    response = await api.post(f"/api/projects/{project['id']}/containers", json={"enabled": True})
    assert response.status_code == 200, response.text
    assert response.json()["containers"] is True
    return response.json()


# ---- limits ---------------------------------------------------------------------------------------------------------

async def test_owner_unlimited_member_defaults_and_overrides(api, env, member_accounts):
    got = (await api.get("/api/containers")).json()
    rows = {r["account"]: r for r in got["accounts"]}
    owners = api.app.state.supervisor.broker.default_account
    assert rows[owners]["limits"] == NONE and rows[owners]["owner"] and rows[owners]["source"] == "default"
    assert rows["dana-agent"]["limits"] == MEMBER and not rows["dana-agent"]["owner"]
    assert got["member_defaults"] == MEMBER and got["enabled"] is False

    put = await api.put("/api/containers/limits/dana-agent", json={**MEMBER, "cpus": 6, "pids": None})
    assert put.status_code == 200 and put.json()["limits"] == {**MEMBER, "cpus": 6, "pids": None}
    put = await api.put(f"/api/containers/limits/{owners}", json={**NONE, "memory_mib": 8192})  # owners: only if set
    assert put.status_code == 200
    rows = {r["account"]: r for r in (await api.get("/api/containers")).json()["accounts"]}
    assert rows["dana-agent"]["limits"]["cpus"] == 6 and rows["dana-agent"]["source"] == "set"
    assert rows[owners]["limits"] == {**NONE, "memory_mib": 8192}
    with write_db(api.app.state.db) as s:
        audit = [a for a in s.query(AuditLog).filter(AuditLog.action == "containers.limits")]
        assert {a.target for a in audit} == {"dana-agent", owners}

    assert (await api.put("/api/containers/limits/dana-agent", json={**MEMBER, "cpus": 0})).status_code == 422
    assert (await api.put("/api/containers/limits/dana-agent", json={**MEMBER, "memory_mib": 1})).status_code == 422
    assert (await api.put("/api/containers/limits/nobody-agent", json=MEMBER)).status_code == 404


async def test_members_cant_see_or_change_limits(api, env, member_accounts):
    await do_setup(api, env)
    link = (await api.post("/api/users/invites", json={"role": "member", "account": "dana-agent"})).json()["link"]
    member = browser(api)
    try:
        assert (await join(member, link, "dana")).status_code == 200
        assert (await member.get("/api/containers")).status_code == 403
        put = await member.put("/api/containers/limits/dana-agent", headers=csrf(member), json=NONE)
        assert put.status_code == 403
    finally:
        await member.aclose()


def test_new_accounts_get_their_roles_defaults(api_db):
    with api_db() as s:
        containers.set_defaults(s, "erin-agent", "member", "alex")
        containers.set_defaults(s, "olive-agent", "owner", "alex")
        s.commit()
        assert containers.limits_for(s, "erin-agent", "agent") == (MEMBER, "set")
        assert containers.limits_for(s, "olive-agent", "agent") == (NONE, "set")
        containers.save_limits(s, "erin-agent", {**MEMBER, "sessions": 1}, "alex")
        containers.set_defaults(s, "erin-agent", "member", "alex")  # never overwrites a choice
        assert containers.limits_for(s, "erin-agent", "agent")[0]["sessions"] == 1


@pytest.fixture
def api_db(tmp_path):
    from acc.server.db import make_engine, make_sessionmaker, migrate
    url = f"sqlite:///{tmp_path / 'acc.db'}"
    migrate(url)
    return make_sessionmaker(make_engine(url))


def test_the_fakes_stop_listening_when_closed(tmp_path):
    """close() alone leaves a thread blocked in accept(); restarted after a signal, that accept() takes the fd number,
    which a later test's launcher socket may hold by then (it took one of its launches, which got no answer)."""
    attach_root = Path(tempfile.mkdtemp(prefix="ct-", dir="/tmp"))
    try:
        launcher = FakeLauncher(tmp_path / "ct.sock", attach_root, tmp_path, {})
        relay = FakeRelay(attach_root / "relay" / "attach.sock", tmp_path / "grant.json", {}, "acc-ct-example-g1.service")
        launcher.close()
        relay.close()
        for thread in (launcher.thread, relay.thread):
            thread.join(timeout=5)
            assert not thread.is_alive()
    finally:
        shutil.rmtree(attach_root)


@pytest.mark.parametrize("peer_closed", [False, True])
@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
async def test_launcher_close_collects_a_relay_still_being_created(containers_env, env, tmp_path, monkeypatch,
                                                                 peer_closed):
    """A launch accepted before teardown must not publish a live relay after teardown."""
    import sys

    launcher = containers_env
    ready, release = threading.Event(), threading.Event()
    created = []
    original_relay = FakeRelay

    class PausedRelay(original_relay):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)
            ready.set()
            assert release.wait(10)

    monkeypatch.setattr(sys.modules[__name__], "FakeRelay", PausedRelay)
    original_join = launcher.thread.join

    def join(timeout=None):
        release.set()  # let the accepted launch finish when teardown waits for its thread
        original_join(timeout)

    monkeypatch.setattr(launcher.thread, "join", join)
    agent_ready = tmp_path / "agent-ready"
    agent = env["home"].parent / "bin/fake-codex-late"
    agent.write_text("#!/usr/bin/env python3\nimport pathlib, sys, time\n"
                     "pathlib.Path(sys.argv[1]).touch()\ntime.sleep(120)\n")
    agent.chmod(0o700)
    monkeypatch.setitem(launcher.providers, "codex", str(agent))
    sid = "abc123456789"
    (launcher.grants / f"{sid}.json").write_text(json.dumps({
        "provider": "codex", "args": [str(agent_ready)], "checkout": str(env["workspaces"] / "demo"),
        "run_dir": str(env["run_dir"]), "session_id": sid, "guard_files": "",
    }))
    with socket.socket(socket.AF_UNIX) as client, socket.socket(socket.AF_UNIX) as terminal:
        try:
            client.connect(str(launcher.path))
            client.sendall(json.dumps({"operation": "launch", "request_id": "late-launch",
                                      "session_id": sid, "generation": 1}).encode() + b"\n")
            assert ready.wait(10)
            terminal.connect(str(broker_containers.attach_socket(launcher.attach_root, sid, 1)))
            await wait_for(agent_ready.exists)
            if peer_closed:
                terminal.shutdown(socket.SHUT_RD)
            launcher.close()
            release.set()
            original_join(timeout=10)
            assert not launcher.thread.is_alive()
            assert created[0].child.poll() is not None, "launcher teardown missed its in-flight fake agent"
        finally:
            release.set()
            original_join(timeout=10)
            for relay in created:
                relay.close()
                relay.thread.join(timeout=10)


# ---- the flag ---------------------------------------------------------------------------------------------------------

async def test_start_hook_after_ingest_is_read_before_verification_timeout(containers_env, api, env, project,
                                                                         monkeypatch):
    """Team requests launch after ingest; slow intervening stages must not turn a queued start into a false alert."""
    from datetime import timedelta

    from helpers import pause_supervisor

    from acc.server import reconcile

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    supervisor.ingest_all()  # this iteration cannot have ingested the helper it has not created yet
    await container_project(api, env, project)
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                    'name': 'After ingest', 'worktree': True})
    assert response.status_code == 201, response.text
    session = response.json()
    spool = env['run_dir'] / session['id'] / 'events.jsonl'
    await wait_for(lambda: spool.exists() and 'SessionStart' in spool.read_text())
    # Simulate expensive stages between ingest and reconcile, without a timing-dependent sleep.
    after_timeout = reconcile.now() + timedelta(seconds=env['settings'].claude_verify_timeout + 1)
    monkeypatch.setattr(reconcile, 'now', lambda: after_timeout)
    await supervisor.reconcile()
    current = (await api.get(f"/api/sessions/{session['id']}")).json()
    assert current['observed_state'] == 'idle', current
    assert current['status_source'] == 'claude_hook'
    assert current['runs'][0]['verified_at'] is not None
    assert current['last_error'] is None
    with api.app.state.db() as db:
        from acc.server.models import AttentionItem

        assert db.query(AttentionItem).filter_by(dedupe_key=f"needs_input:{session['id']}").first() is None


async def test_flag_off_launches_exactly_as_today(api, env, project, monkeypatch):
    """No container parameter, no grant, a worktree as before; and the project switch is refused."""
    repo(env["workspaces"] / "demo")
    broker = api.app.state.supervisor.broker
    calls = []
    real = broker.call

    async def spy(method, **params):
        calls.append((method, params))
        return await real(method, **params)
    monkeypatch.setattr(broker, "call", spy)
    refused = await api.post(f"/api/projects/{project['id']}/containers", json={"enabled": True})
    assert refused.status_code == 409 and "turned off" in refused.text
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "x",
                                                     "worktree": True})).json()
    await wait_for(state_is(api, session["id"], "idle"))
    assert session["container"] is False
    assert [m for m, _ in calls if m in ("make_worktree", "make_private_checkout")] == ["make_worktree"]
    [launch] = [p for m, p in calls if m == "launch"]
    assert "container" not in launch
    assert (Path(session["worktree"]) / ".git").is_file()  # an ordinary worktree


async def test_switched_on_project_refuses_when_the_flag_goes_off(containers_env, api, env, project):
    await container_project(api, env, project)
    object.__setattr__(env["settings"], "containers_enabled", False)  # the same Settings object accd uses
    try:
        response = await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                         "name": "x"})
        assert response.status_code == 409 and "turned off" in response.text  # never a native fallback
    finally:
        object.__setattr__(env["settings"], "containers_enabled", True)
    assert containers_env.requests == []


async def test_container_launches_are_refused_until_the_attach_path_is_qualified(containers_env, api, env, project,
                                                                                 monkeypatch):
    """Interim: the broker's pane can't reach the launcher unit's private runtime yet, so a container session would run
    an agent nobody sees (docs/decisions/drafts/container-attach-path.md). Off by default; nothing is made or asked."""
    from acc.server import config
    assert config.Settings().containers_attach_qualified is False
    monkeypatch.setattr(config, "environment", {"ACC_CONTAINERS_ATTACH_QUALIFIED": "1"})
    assert config.Settings.from_env().containers_attach_qualified is True
    await container_project(api, env, project)
    object.__setattr__(env["settings"], "containers_attach_qualified", False)
    try:
        response = await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                         "name": "x"})
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == ("container sessions are not available yet: the terminal attach path is "
                                             "not qualified")
    finally:
        object.__setattr__(env["settings"], "containers_attach_qualified", True)
    assert containers_env.requests == []



async def test_resumes_and_restarts_are_refused_until_the_attach_path_is_qualified(containers_env, api, env, project):
    """Review M-1: the 409 covered new sessions only. A container session that already exists (made before the gate,
    or while qualified) must not be resumed, restarted or retried into an agent nobody can see either."""
    await container_project(api, env, project)
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                     "name": "made while qualified"})).json()
    await wait_for(state_is(api, session["id"], "idle"))
    await api.post(f"/api/sessions/{session['id']}/stop")
    await wait_for(lambda: any(r["operation"] == "stop" for r in containers_env.requests))
    object.__setattr__(env["settings"], "containers_attach_qualified", False)
    try:
        await api.post(f"/api/sessions/{session['id']}/resume")

        async def failed():
            events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
            return [e for e in events if e["kind"] == "launch_failed"]
        [event, *_] = await wait_for(failed)
        assert "terminal attach path is not qualified" in event["payload"]["error"]
        assert [r["generation"] for r in containers_env.requests if r["operation"] == "launch"] == [1]
        assert not (env["settings"].containers_dir / "grants" / f"{session['id']}.json").exists()
    finally:
        object.__setattr__(env["settings"], "containers_attach_qualified", True)

async def test_new_container_sessions_need_a_qualified_host_and_running_ones_are_left_alone(containers_env, api, env,
                                                                                           project, monkeypatch):
    """Containers are a Preview: a new container session is refused, and audited, while this host has no passing
    qualification record. A session already running keeps running; nothing is made or asked for the refused one."""
    from acc import container_host_record as hr
    await container_project(api, env, project)
    running = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                     "name": "made while qualified"})).json()
    await wait_for(state_is(api, running["id"], "idle"))
    before = list(containers_env.requests)
    checkouts = sorted(p.name for p in env["settings"].account_root(running["run_as"]).iterdir())
    monkeypatch.setattr(hr, "check", lambda path=None: {"passed": False, "reason": "this host hasn't recorded a "
                                                        "passed container qualification; run the kit"})
    refused = await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "new"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == ("this project runs sessions in containers, a Preview that needs a qualified "
                                        "host: this host hasn't recorded a passed container qualification; run the kit")
    with write_db(api.app.state.db) as s:
        [audit] = s.query(AuditLog).filter(AuditLog.action == "container.launch_refused").all()
        assert audit.target == project["slug"] and audit.detail == {"reason": "host_not_qualified", "provider": "claude"}
    assert containers_env.requests == before  # no launch, no stop
    assert sorted(p.name for p in env["settings"].account_root(running["run_as"]).iterdir()) == checkouts  # no checkout
    assert [x["id"] for x in (await api.get("/api/sessions")).json()].count(running["id"]) == 1
    await wait_for(state_is(api, running["id"], "idle"))  # the running one is untouched


# ---- a container session end to end ---------------------------------------------------------------------------------

async def test_container_session_launches_through_the_launcher_and_stops(containers_env, api, env, project):
    await container_project(api, env, project)
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                     "name": "Fix Login"})).json()
    assert session["container"] is True and session["worktree"]
    checkout = Path(session["worktree"])
    assert (checkout / ".git").is_dir() and (checkout / "app.py").exists()  # a private checkout, not a worktree
    await wait_for(state_is(api, session["id"], "idle"))

    [launch] = containers_env.requests
    assert set(launch) == {"version", "request_id", "operation", "session_id", "generation", "policy_id"}
    assert launch["operation"] == "launch" and launch["session_id"] == session["id"] and launch["generation"] == 1
    grant_path = env["settings"].containers_dir / "grants" / f"{session['id']}.json"
    grant = json.loads(grant_path.read_text())
    assert stat.S_IMODE(grant_path.stat().st_mode) == 0o640
    assert grant["account"] == session["run_as"] and grant["checkout"] == session["worktree"]
    assert grant["run_dir"].endswith(session["id"]) and grant["generation"] == 1 and grant["policy_id"] == "default"
    assert grant["limits"] == NONE  # the owners' account: no limits unless an owner sets some
    files = session["launch_spec"]["files"]
    assert grant["guard_files"] == containers.guard_hashes(files) and "settings.json=" in grant["guard_files"]

    # Stop: the broker asks the launcher to stop it, and the grant goes (the launcher's reconcile backs that up)
    await api.post(f"/api/sessions/{session['id']}/stop")
    await wait_for(lambda: any(r["operation"] == "stop" for r in containers_env.requests))
    stop = [r for r in containers_env.requests if r["operation"] == "stop"][0]
    assert stop["generation"] == 1 and stop["session_id"] == session["id"]
    await wait_for(lambda: not grant_path.exists())

    # Resume: a new generation and a new grant
    await api.post(f"/api/sessions/{session['id']}/resume")
    await wait_for(lambda: [r["generation"] for r in containers_env.requests if r["operation"] == "launch"] == [1, 2])
    assert json.loads(grant_path.read_text())["generation"] == 2


async def test_member_limits_reach_the_grant(containers_env, api, env, project):
    await container_project(api, env, project)
    owners = api.app.state.supervisor.broker.default_account
    await api.put(f"/api/containers/limits/{owners}", json={**MEMBER, "cpus": 3})
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                     "name": "limited"})).json()
    await wait_for(state_is(api, session["id"], "idle"))
    grant = json.loads((env["settings"].containers_dir / "grants" / f"{session['id']}.json").read_text())
    assert grant["limits"] == {**MEMBER, "cpus": 3}


async def test_a_refused_container_never_falls_back_to_the_host(containers_env, api, env, project):
    await container_project(api, env, project)
    containers_env.answers["launch"] = {"ok": False, "code": "unpinned_image", "detail": "not pinned"}
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                     "name": "refused"})).json()

    async def failed():
        events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
        return [e for e in events if e["kind"] == "launch_failed"]
    [event, *_] = await wait_for(failed)
    assert "unpinned_image" in event["payload"]["error"]
    panes = subprocess.run(["tmux", "-L", env["broker"].tmux_socket, "list-sessions", "-F", "#{session_name}"],
                           capture_output=True, text=True).stdout
    assert session["tmux_name"] not in panes


async def test_orchestrators_and_other_machines_are_refused(containers_env, api, env, project):
    await container_project(api, env, project)
    with write_db(api.app.state.db) as s:
        from acc.server.models import Project
        s.get(Project, project["id"]).host = "elsewhere"
        s.commit()
    response = await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "x"})
    assert response.status_code == 409 and "on another machine" in response.text
    assert containers_env.requests == []


async def test_switching_on_needs_a_qualified_host_and_switching_off_never_does(containers_env, api, env, project,
                                                                                   monkeypatch):
    """Containers are a Preview (docs/CONTAINERS.md, "Release status"): on only where this host passed qualification."""
    from acc import container_host_record as hr
    repo(env["workspaces"] / "demo")
    got = (await api.get("/api/containers")).json()
    assert got["release"] == "preview" and got["qualification"]["passed"] is True
    qualified = hr.check
    monkeypatch.setattr(hr, "check", lambda path=None: {"passed": False, "reason": "this host hasn't recorded a passed "
                                                        "container qualification; run the container qualification kit"})
    refused = await api.post(f"/api/projects/{project['id']}/containers", json={"enabled": True})
    assert refused.status_code == 409
    assert refused.json()["detail"].startswith("containers are a Preview that needs a qualified host: this host hasn't")
    got = (await api.get("/api/containers")).json()
    assert got["qualification"] == {"passed": False, "reason": refused.json()["detail"].split(": ", 1)[1]}
    with write_db(api.app.state.db) as s:
        assert s.get(Project, project["id"]).containers is False

    monkeypatch.setattr(hr, "check", qualified)
    assert (await api.post(f"/api/projects/{project['id']}/containers", json={"enabled": True})).status_code == 200
    monkeypatch.setattr(hr, "check", lambda path=None: {"passed": False, "reason": "record removed"})
    # A project that is already on can be confirmed on, and switched off, without a record.
    assert (await api.post(f"/api/projects/{project['id']}/containers", json={"enabled": True})).status_code == 200
    off = await api.post(f"/api/projects/{project['id']}/containers", json={"enabled": False})
    assert off.status_code == 200 and off.json()["containers"] is False


def test_the_record_path_is_roots_by_default():
    from acc.server.config import Settings
    assert Settings().containers_record == Path("/etc/acc/containers/host-qualified.json")
    assert Settings().containers_enabled is False


async def test_only_owners_switch_projects(containers_env, api, env, member_accounts):
    await do_setup(api, env)
    link = (await api.post("/api/users/invites", json={"role": "member", "account": "dana-agent"})).json()["link"]
    member = browser(api)
    try:
        assert (await join(member, link, "dana")).status_code == 200
        made = await member.post("/api/projects", headers=csrf(member), json={
            "slug": "mine", "name": "Mine", "workspace_path": str(env["workspaces"] / "demo")})
        pid = made.json()["id"]
        refused = await member.post(f"/api/projects/{pid}/containers", headers=csrf(member), json={"enabled": True})
        assert refused.status_code == 403
        assert (await api.post(f"/api/projects/{pid}/containers", json={"enabled": True})).status_code == 200
        with write_db(api.app.state.db) as s:
            [detail] = [a.detail for a in s.query(AuditLog).filter(AuditLog.action == "project.containers")]
            assert detail["enabled"] is True and len(detail["qualification"]) == 64  # the report it relied on
    finally:
        await member.aclose()


# ---- the launcher's audit lines ---------------------------------------------------------------------------------------

def test_audit_lines_are_copied_once_with_a_cursor(api_db, tmp_path):
    from acc.server.config import Settings
    settings = Settings(containers_dir=tmp_path / "ct")
    folder = settings.containers_dir / "audit"
    folder.mkdir(parents=True)
    lines = [{"id": "1", "ts": "2026-10-23T10:00:00+00:00", "action": "container.launch", "session_id": "abc12345",
              "account": "dana-agent", "limits": MEMBER, "correlation": "brk-1"},
             {"id": "2", "ts": "2026-10-23T10:01:00+00:00", "action": "container.refused", "code": "wrong_account",
              "peer_uid": 1001, "secret": "never copied"},
             {"action": "something.else"}]
    (folder / "20261023.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + '{"action": "container.part')
    with api_db() as s:
        assert containers.ingest_audit(s, settings) == 2
        s.commit()
        assert containers.ingest_audit(s, settings) == 0  # nothing twice, and the half-written line waits
        rows = s.query(AuditLog).order_by(AuditLog.id).all()
        assert [r.action for r in rows] == ["container.launch", "container.refused"]
        assert rows[0].actor == "acc-containers" and rows[0].target == "abc12345" and rows[0].detail["limits"] == MEMBER
        assert "secret" not in rows[1].detail and rows[1].detail["code"] == "wrong_account"
    with open(folder / "20261023.jsonl", "a") as handle:
        handle.write('ial"}\n')
    (folder / "20261024.jsonl").write_text(json.dumps({"action": "container.stop", "session_id": "abc12345"}) + "\n")
    with api_db() as s:
        assert containers.ingest_audit(s, settings) == 2  # the line once complete, then the next file
        s.commit()


# ---- broker side ----------------------------------------------------------------------------------------------------

def config(tmp_path, providers=None) -> BrokerConfig:
    return isolated_broker(tmp_path, socket_path=tmp_path / "s", run_dir=tmp_path / "run", tmux_socket="x", allowed_roots=(tmp_path,),
                        allowed_uids=frozenset(), providers=providers or {}, hook_path="x", container_policy=tmp_path / "container-policy.json")


def test_private_checkout_brings_its_branch_back(tmp_path):
    cfg = config(tmp_path)
    repo(tmp_path / "proj")
    made = ops.make_private_checkout(cfg, str(tmp_path / "proj"), str(tmp_path), "proj-abc12345", "acc/fix-abc12345")
    tree = Path(made["path"])
    assert made["private"] and (tree / ".git").is_dir() and (tree / "app.py").exists()
    assert not (tmp_path / "proj/.git/worktrees").exists()  # nothing shared with the project
    (tree / "app.py").write_text("changed\n")
    kept = ops.remove_worktree(cfg, str(tmp_path / "proj"), str(tree))
    assert kept["removed"] is False and "uncommitted" in kept["reason"]
    git(tree, "commit", "-qam", "work")
    assert ops.remove_worktree(cfg, str(tmp_path / "proj"), str(tree)) == {"removed": True, "branch": "acc/fix-abc12345"}
    assert not tree.exists()
    log = subprocess.run(["git", "-C", str(tmp_path / "proj"), "log", "--format=%s", "acc/fix-abc12345"],
                         capture_output=True, text=True).stdout.split("\n")
    assert log[0] == "work"


def test_private_checkout_snapshot_preserves_racy_index_detection(tmp_path):
    cfg = config(tmp_path)
    repo(tmp_path / "proj")
    tree = Path(ops.make_private_checkout(cfg, str(tmp_path / "proj"), str(tmp_path),
                                          "proj-racy", "acc/fix-racy")["path"])
    file, index = tree / "app.py", tree / ".git/index"
    timestamp = 1_000_000_000_000_000_000
    os.utime(file, ns=(timestamp, timestamp))
    git(tree, "add", "app.py")
    os.utime(index, ns=(timestamp, timestamp))
    file.write_text("changed\n")  # same size and cached mtime: Git must use the index's racy-clean check
    os.utime(file, ns=(timestamp, timestamp))

    result = ops._git(tree, "-c", "core.trustctime=false", "status", "--porcelain")
    assert result.returncode == 0, result.stderr
    assert result.stdout == " M app.py\n"
    assert tree.exists() and file.read_text() == "changed\n"


@pytest.mark.parametrize("key, value", [
    ("core.fsmonitor", "touch {marker}"), ("filter.x.clean", "touch {marker}"), ("diff.x.textconv", "touch {marker}"),
    ("include.path", "{marker}.cfg"), ("core.pager", "touch {marker}"), ("gpg.program", "touch {marker}"),
])
def test_git_never_runs_a_program_the_container_put_in_its_checkout(tmp_path, key, value):
    """The agent in the container can write its checkout's .git/config; the broker runs git there on the host."""
    cfg = config(tmp_path)
    repo(tmp_path / "proj")
    tree = Path(ops.make_private_checkout(cfg, str(tmp_path / "proj"), str(tmp_path), "p-1", "acc/x-1")["path"])
    marker = tmp_path / "RAN"
    hook = tree / ".git/hooks/post-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    (tree / ".gitattributes").write_text("* filter=x diff=x\n")
    git(tree, "add", ".gitattributes")
    git(tree, "commit", "-qm", "attributes")
    marker.unlink(missing_ok=True)  # the test's own commit ran the hook: only the broker's git counts
    subprocess.run(["git", "-C", str(tree), "config", key, value.format(marker=marker)], check=True)
    for call in (lambda: ops.workspace_changes(cfg, str(tree), None, True),
                 lambda: ops.remove_worktree(cfg, str(tmp_path / "proj"), str(tree))):
        with pytest.raises(ops.OpError, match="won't run git there"):
            call()
    assert not marker.exists() and tree.exists()
    subprocess.run(["git", "-C", str(tree), "config", "--unset", key], check=True)
    assert ops.remove_worktree(cfg, str(tmp_path / "proj"), str(tree))["removed"]  # hooks off: post-checkout etc.
    assert not marker.exists()


def test_launcher_requests_carry_only_the_fixed_fields(tmp_path):
    launcher = FakeLauncher(tmp_path / "ct.sock")
    try:
        answer = broker_containers.ask(launcher.path, "launch", "abc12345", {"generation": 4, "policy_id": "default"})
        assert answer["code"] == "launched"
        [request] = launcher.requests
        assert set(request) == {"version", "request_id", "operation", "session_id", "generation", "policy_id"}
        launcher.answers["stop"] = {"ok": False, "code": "wrong_account", "detail": "no"}
        with pytest.raises(broker_containers.ContainerError, match="wrong_account"):
            broker_containers.ask(launcher.path, "stop", "abc12345", {"generation": 4, "policy_id": "default"})
    finally:
        launcher.close()
    with pytest.raises(broker_containers.ContainerError, match="isn't reachable"):
        broker_containers.ask(tmp_path / "missing.sock", "status", "abc12345", {"generation": 1, "policy_id": "default"})


@pytest.mark.parametrize("bad", [None, {}, {"generation": 1}, {"generation": "1", "policy_id": "default"},
                                 {"generation": 1, "policy_id": "default", "image": "x"},
                                 {"generation": 1, "policy_id": "../x"}, {"generation": 0, "policy_id": "default"}])
def test_broker_refuses_bad_container_parameters(tmp_path, bad):
    with pytest.raises(broker_containers.ContainerError):
        broker_containers.check(bad)


def test_container_launch_needs_a_private_checkout(tmp_path):
    cfg = config(tmp_path, providers={"claude": "/bin/true"})
    repo(tmp_path / "proj")
    tree = ops.make_worktree(cfg, str(tmp_path / "proj"), str(tmp_path), "proj-1", "acc/x-1")["path"]
    with pytest.raises(ops.OpError, match="private checkout"):
        ops.launch(cfg, {"acc_id": "abc12345", "tmux_name": "acc-x", "provider": "claude", "cwd": tree,
                         "container": {"generation": 1, "policy_id": "default"}})


def test_guard_hashes_match_the_brokers():
    files = {"settings.json": "{}", "guardrails.json": "[]", "other.md": "x"}
    import hashlib
    broker_side = " ".join(f"{n}={hashlib.sha256(files[n].encode()).hexdigest()}" for n in ops.GUARD_FILES if n in files)
    assert containers.guard_hashes(files) == broker_side


def test_kill_without_a_container_is_unchanged(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    monkeypatch.setattr(ops, "tmux", lambda config, *args, **kw: subprocess.CompletedProcess(args, 1))
    assert ops.kill(cfg, "acc-x") == {"killed": False}
    with pytest.raises(ops.OpError):
        ops.kill(cfg, "acc-x", {"generation": 1, "policy_id": "default"}, "../x")


def test_ls07_member_on_default_account_gets_member_defaults(api_db):
    from acc.server.models import User
    with api_db() as s:
        s.add(User(username="member-example", password_hash="unused", totp_secret="unused", role="member", account=None))
        s.commit()
        assert containers.limits_for(s, "alex-agent", "alex-agent")[0] == MEMBER


def test_ls08_replaced_git_administration_is_refused(tmp_path):
    import shutil
    cfg = config(tmp_path)
    repo(tmp_path / "proj")
    tree = Path(ops.make_private_checkout(cfg, str(tmp_path / "proj"), str(tmp_path), "p-1", "acc/x-1")["path"])
    shutil.rmtree(tree / ".git")
    (tree / ".git").write_text(f"gitdir: {tmp_path / 'proj/.git'}\n")
    with pytest.raises(ops.OpError, match="won't run git"):
        ops.workspace_changes(cfg, str(tree), None, True)


def test_ls08_config_swap_cannot_execute_on_host(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    repo(tmp_path / "proj")
    tree = Path(ops.make_private_checkout(cfg, str(tmp_path / "proj"), str(tmp_path), "p-1", "acc/x-1")["path"])
    (tree / "app.py").write_text("changed\n")
    marker = tmp_path / "SYNTHETIC_HOST_EXECUTION"
    real = broker_containers.git_guard
    def swap(cwd):
        extra = real(cwd)
        subprocess.run(["git", "-C", str(tree), "config", "diff.external", f"touch {marker}"], check=True)
        return extra
    monkeypatch.setattr(broker_containers, "git_guard", swap)
    ops._git(tree, "diff", "HEAD")
    assert not marker.exists()


@pytest.mark.parametrize("linked", ["root", "nested"])
def test_ls10_run_folder_links_cannot_write_outside_the_session(tmp_path, monkeypatch, linked):
    cfg = config(tmp_path, providers={"claude": "/bin/true"})
    cfg.run_dir.mkdir()
    repo(tmp_path / "proj")
    tree = ops.make_private_checkout(cfg, str(tmp_path / "proj"), str(tmp_path), "p-1", "acc/x-1")["path"]
    outside = tmp_path / "outside"
    outside.mkdir()
    run = cfg.run_dir / "abc12345"
    if linked == "root":
        run.symlink_to(outside, target_is_directory=True)
    else:
        run.mkdir()
        (run / "nested").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(ops, "tmux", lambda *a, **kw: subprocess.CompletedProcess(a, 1, stdout=""))
    def refuse(*args, **kwargs):
        raise broker_containers.ContainerError("synthetic refusal")
    monkeypatch.setattr(broker_containers, "ask", refuse)
    name = "proof.txt" if linked == "root" else "nested/proof.txt"
    with pytest.raises(ops.OpError):
        ops.launch(cfg, {"acc_id": "abc12345", "tmux_name": "acc-x", "provider": "claude", "cwd": tree,
                         "files": {name: "SYNTHETIC"}, "container": {"generation": 1, "policy_id": "default"}})
    assert not (outside / "proof.txt").exists()


async def test_fixture_does_not_ingest_readable_host_audit(tmp_path, monkeypatch):
    """Root can read the installation default; pytest must never inherit it, even with containers disabled."""
    from types import SimpleNamespace

    import conftest
    from acc.server.app import create_app

    host = tmp_path / 'simulated-host'
    (host / 'audit').mkdir(parents=True)
    (host / 'audit/20261022.jsonl').write_text(
        ''.join(json.dumps({'action': 'container.refused', 'code': f'foreign-{i}'}) + '\n' for i in range(8)))
    original = conftest.Settings

    def host_default(*args, **kwargs):
        kwargs.setdefault('containers_dir', host)
        return original(*args, **kwargs)

    monkeypatch.setattr(conftest, 'Settings', host_default)
    case = tmp_path / 'case'
    case.mkdir()
    fixture = conftest.env.__wrapped__(case, monkeypatch, SimpleNamespace(register=lambda _: None),
                                       SimpleNamespace(fixturenames=[]))
    world = next(fixture)
    app = create_app(world['settings'])
    try:
        await app.state.supervisor.ingest_container_audit()
        await app.state.supervisor.ingest_container_audit()
        with app.state.db() as db:
            assert db.query(AuditLog).filter(AuditLog.action == 'container.refused').count() == 0
    finally:
        app.state.db.kw['bind'].dispose()
        with pytest.raises(StopIteration):
            next(fixture)


async def test_ls11_flag_off_still_ingests_launcher_audit(api, env, tmp_path):
    """The supervisor's first pass ingests the launcher audit at once, from the fixture's folder; this test points it
    at its own folder only afterwards. Under load that first pass came first, and the next one was 10 s away (the
    flake: a 2 s wait). A shorter cadence alone wasn't enough: in a serial gate on a loaded host the row still wasn't
    there after 5 s, since each pass waits for the whole supervisor iteration before it (machines, ingest, reconcile,
    ...). So wait for the real condition, a containers pass that began after the redirect and has finished; then the
    row must be there."""
    supervisor = api.app.state.supervisor
    started, finished = [], []
    original = supervisor.ingest_container_audit

    async def counted():
        number = len(started)
        started.append(number)
        await original()
        finished.append(number)
    object.__setattr__(supervisor, "ingest_container_audit", counted)
    object.__setattr__(env["settings"], "container_audit_interval", 0.1)  # read on every pass
    await wait_for(lambda: finished, timeout=60)  # a pass from the fixture's folder has run
    root = tmp_path / "launcher-audit"
    (root / "audit").mkdir(parents=True)
    (root / "audit/20261023.jsonl").write_text(json.dumps({
        "id": "synthetic-refusal", "action": "container.refused", "code": "not_broker"}) + "\n")
    object.__setattr__(env["settings"], "containers_dir", root)
    after = len(started)  # passes numbered from here began after the redirect
    # Genuinely asynchronous: bounded only by how long the loop takes to come round under load.
    await wait_for(lambda: any(number >= after for number in finished), timeout=60)
    with api.app.state.db() as s:
        assert s.query(AuditLog).filter(AuditLog.action == "container.refused").count() == 1


def test_ls09_independent_audit_streams_are_ingested_once(api_db, tmp_path):
    from acc.server.config import Settings
    settings = Settings(containers_dir=tmp_path)
    folder = tmp_path / "audit"
    folder.mkdir()
    (folder / "20261023.jsonl").write_text('{"action":"container.launch"}\n')
    with api_db() as s:
        assert containers.ingest_audit(s, settings) == 1
        s.commit()
    (folder / "20261023-refused-1001.jsonl").write_text('{"action":"container.refused"}\n')
    with api_db() as s:
        assert containers.ingest_audit(s, settings) == 1
        s.commit()
        assert containers.ingest_audit(s, settings) == 0


def test_ls08_snapshot_reads_preserve_artifact_exclusions(tmp_path):
    cfg = config(tmp_path)
    repo(tmp_path / "proj")
    tree = Path(ops.make_private_checkout(cfg, str(tmp_path / "proj"), str(tmp_path), "p-1", "acc/x-1")["path"])
    ops.exclude_artifacts(tree)
    (tree / ".acc").mkdir()
    (tree / ".acc/artifact.txt").write_text("SYNTHETIC")
    assert ops.workspace_changes(cfg, str(tree), None)["files"] == 0


def test_container_git_snapshot_preserves_racy_index_timestamp(tmp_path):
    """Copying index bytes must not make a racy stat cache look older than the index."""
    import os
    import sys

    tree = tmp_path / 'repo'
    repo(tree)
    old = 1_700_000_000_000_000_000
    file = tree / 'app.py'
    os.utime(file, ns=(old, old))
    git(tree, 'add', 'app.py')
    os.utime(tree / '.git/index', ns=(old, old))
    file.write_text('changed\n')  # same eight-byte size
    os.utime(file, ns=(old, old))
    # Emulate a filesystem whose ctime/subsecond stat fields cannot distinguish
    # these writes. Git must hash entries at least as new as the source index.
    args = ['-c', 'core.trustctime=false', '-c', 'core.checkstat=minimal', 'status', '--porcelain']
    direct = subprocess.check_output(['/usr/bin/git', '-C', str(tree), *args], text=True,
                                     env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    assert 'app.py' in direct
    wrapper = Path(broker_containers.__file__).with_name('container_git.py')
    copied = subprocess.check_output([sys.executable, str(wrapper), str(tree), *args], text=True)
    assert 'app.py' in copied


@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_container_team_helpers_inherit_isolation_and_prepare_merge(containers_env, api, env, project, provider):
    from test_team import grant, request

    await container_project(api, env, project)
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                             'name': 'Lead', 'worktree': True, 'orchestrator': grant(agents=[{'agent': provider}])})
    assert response.status_code == 201, response.text
    lead = response.json()
    assert 'inside your container' in json.dumps(lead['launch_spec']['files'])
    await wait_for(state_is(api, lead['id'], 'idle'))
    result = await request(api, env, lead, 'start', name='Helper', agent=provider, task='work')
    assert result['ok'], result
    helper = result['result']
    assert helper['container'] is True
    await wait_for(state_is(api, helper['id'], 'idle'))
    assert Path(helper['worktree'], '.git').is_dir()
    assert Path(helper['worktree']) != Path(lead['worktree'])
    summary = await request(api, env, lead, 'status', helper=helper['id'])
    assert summary['ok'] and 'error' not in summary['result']['git']
    grandchild = await request(api, env, helper, 'start', name='Nested', agent=provider, task='nested work')
    assert grandchild['ok'] and grandchild['result']['container']
    await wait_for(state_is(api, grandchild['result']['id'], 'idle'))
    assert (await request(api, env, helper, 'finish', helper=grandchild['result']['id']))['ok']
    assert {r['session_id'] for r in containers_env.requests if r['operation'] == 'launch'} == {lead['id'], helper['id'], grandchild['result']['id']}
    (Path(helper['worktree']) / 'helper.txt').write_text('helper work')
    git(Path(helper['worktree']), 'add', 'helper.txt')
    git(Path(helper['worktree']), 'commit', '-m', 'helper work')
    stopped = await request(api, env, lead, 'stop', helper=helper['id'])
    assert stopped['ok'], stopped
    merged = await request(api, env, lead, 'merge', helper=helper['id'])
    assert merged['ok'], merged
    prepared = merged['result']
    assert prepared['prepared'] and not prepared['merged']
    bundle = Path(prepared['bundle'])
    team = env['run_dir'] / lead['id'] / 'team'
    assert bundle.is_relative_to(team)
    again = await request(api, env, lead, 'merge', helper=helper['id'])  # replaces the bundle, never adds one
    assert again['ok'] and again['result']['bundle'] == prepared['bundle']
    assert [p.name for p in team.glob('merge-*')] == [bundle.name]
    # The fake runtime has no namespaces: emulate the lead's in-container Git commands.
    git(Path(lead['worktree']), 'fetch', str(bundle), prepared['branch'])
    git(Path(lead['worktree']), 'merge', '--no-edit', 'FETCH_HEAD')
    assert (Path(lead['worktree']) / 'helper.txt').read_text() == 'helper work'
    assert (await request(api, env, lead, 'finish', helper=helper['id']))['ok']
    assert not Path(helper['worktree']).exists()
    assert not bundle.exists()
    (team / 'merge-left-over.bundle').write_bytes(b'x')
    replies = sorted(p.name for p in team.glob('*.json'))
    assert (await api.post(f"/api/sessions/{lead['id']}/archive")).status_code == 200
    assert not list(team.glob('merge-*')) and sorted(p.name for p in team.glob('*.json')) == replies


async def test_native_team_in_a_project_switched_to_containers_gets_a_clear_merge_refusal(containers_env, api, env,
                                                                                          project):
    from test_team import lead as start_lead
    from test_team import request

    lead = await start_lead(api, project, env)
    await wait_for(state_is(api, lead['id'], 'idle'))
    helper = (await request(api, env, lead, 'start', name='Helper', agent='claude', task='work'))['result']
    assert helper['container'] is False
    await wait_for(state_is(api, helper['id'], 'idle'))
    assert (await request(api, env, lead, 'stop', helper=helper['id']))['ok']
    response = await api.post(f"/api/projects/{project['id']}/containers", json={"enabled": True})
    assert response.status_code == 200, response.text
    refused = await request(api, env, lead, 'merge', helper=helper['id'])
    assert not refused['ok']
    assert 'started natively' in refused['error'] and helper['worktree_branch'] in refused['error']
    assert not [r for r in containers_env.requests if r['operation'] == 'launch']


def test_container_merge_bundle_is_capped_before_anything_is_written(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    repo(tmp_path / 'proj')
    tree = Path(ops.make_private_checkout(cfg, str(tmp_path / 'proj'), str(tmp_path), 'lead', 'acc/lead')['path'])
    (cfg.run_dir / 'abc12345').mkdir(parents=True)
    written = ops.team_prepare_merge(cfg, str(tree), 'acc/lead', 'abc12345')
    bundle = Path(written['bundle'])
    assert bundle == cfg.run_dir / 'abc12345/team/merge-lead.bundle'
    git(tmp_path / 'proj', 'bundle', 'verify', str(bundle))
    monkeypatch.setattr(ops, 'TEAM_BUNDLE_LIMIT', bundle.stat().st_size - 1)
    bundle.unlink()
    with pytest.raises(ops.OpError, match='exceeds 8 MiB'):
        ops.team_prepare_merge(cfg, str(tree), 'acc/lead', 'abc12345')
    assert not list((cfg.run_dir / 'abc12345/team').iterdir())
    assert sorted(p.name for p in cfg.run_dir.iterdir()) == ['abc12345']  # no temporary bundle left either


def test_clearing_merge_bundles_removes_links_and_only_bundle_names(tmp_path):
    cfg = config(tmp_path)
    team = cfg.run_dir / 'abc12345/team'
    team.mkdir(parents=True)
    outside = tmp_path / 'outside.bundle'
    outside.write_text('keep')
    (team / 'merge-one.bundle').write_text('x')
    (team / 'merge-two.bundle').symlink_to(outside)
    (team / ('f' * 32 + '.json')).write_text('{}')
    assert ops.team_clear_bundles(cfg, 'abc12345', 'acc/one') == {'removed': ['merge-one.bundle']}
    assert ops.team_clear_bundles(cfg, 'abc12345') == {'removed': ['merge-two.bundle']}
    assert outside.read_text() == 'keep' and [p.name for p in team.iterdir()] == ['f' * 32 + '.json']
    assert ops.team_clear_bundles(cfg, 'nothere1') == {'removed': []}
    for bad in (('../x1234567', None), ('abc12345', 'main'), ('abc12345', 'acc/../x')):
        with pytest.raises(ops.OpError):
            ops.team_clear_bundles(cfg, *bad)


async def test_container_team_refuses_disabled_flag_and_remote_grant(containers_env, api, env, project):
    from test_team import grant, request

    await container_project(api, env, project)
    with write_db(api.app.state.db) as db:
        from acc.server.models import Node
        db.add(Node(id='elsewhere', name='elsewhere', status='active'))
        db.commit()
    body = {'project_id': project['id'], 'provider': 'claude', 'name': 'Lead', 'worktree': True,
            'orchestrator': grant(machines=['local', 'elsewhere'])}
    response = await api.post('/api/sessions', json=body)
    assert response.status_code == 422 and 'local' in response.text
    body['orchestrator'] = grant()
    response = await api.post('/api/sessions', json=body)
    assert response.status_code == 201, response.text
    lead = response.json()
    await wait_for(state_is(api, lead['id'], 'idle'))
    object.__setattr__(env['settings'], 'containers_enabled', False)
    try:
        result = await request(api, env, lead, 'start', name='Helper', agent='claude', task='work')
        assert not result['ok'] and 'turned off' in result['error']
    finally:
        object.__setattr__(env['settings'], 'containers_enabled', True)
    assert len([r for r in containers_env.requests if r['operation'] == 'launch']) == 1


async def test_refused_helper_container_has_no_native_fallback(containers_env, api, env, project):
    from test_team import grant, request

    await container_project(api, env, project)
    limits = grant()
    limits.pop('machines')  # defaults are validated before the container-specific check
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                             'name': 'Lead', 'worktree': True, 'orchestrator': limits})
    assert response.status_code == 201, response.text
    lead = response.json()
    await wait_for(state_is(api, lead['id'], 'idle'))
    containers_env.answers['launch'] = {'ok': False, 'code': 'unpinned_image', 'detail': 'qualification required'}
    result = await request(api, env, lead, 'start', name='Helper', agent='claude', task='work')
    assert result['ok'], result
    helper = result['result']
    async def failed():
        events = (await api.get(f"/api/sessions/{helper['id']}/events")).json()
        return [e for e in events if e['kind'] == 'launch_failed']
    [event, *_] = await wait_for(failed)
    assert 'unpinned_image' in event['payload']['error']
    panes = subprocess.run(['tmux', '-L', env['broker'].tmux_socket, 'list-sessions', '-F', '#{session_name}'],
                           capture_output=True, text=True).stdout
    assert helper['tmux_name'] not in panes
    assert helper['container']


@pytest.mark.parametrize('operation', ['seed-helper', 'prepare-merge'])
@pytest.mark.parametrize('attack', ['config-program', 'alternate-link', 'head-link'])
def test_container_team_git_rejects_host_escape_metadata(tmp_path, operation, attack):
    from acc.broker.containers import ContainerError

    cfg = config(tmp_path)
    repo(tmp_path / 'proj')
    tree = Path(ops.make_private_checkout(cfg, str(tmp_path / 'proj'), str(tmp_path), 'lead', 'acc/lead')['path'])
    marker = tmp_path / 'RAN'
    if attack == 'config-program':
        git(tree, 'config', 'core.fsmonitor', f'touch {marker}')
    elif attack == 'alternate-link':
        info = tree / '.git/objects/info'
        info.mkdir(exist_ok=True)
        (info / 'alternates').symlink_to(tmp_path / 'proj/.git/objects')
    else:
        (tree / '.git/HEAD').unlink()
        (tree / '.git/HEAD').symlink_to(tmp_path / 'proj/.git/HEAD')
    with pytest.raises((ops.OpError, ContainerError, OSError)):
        if operation == 'seed-helper':
            ops.make_private_checkout(cfg, str(tree), str(tmp_path), 'child', 'acc/child')
        else:
            ops.team_prepare_merge(cfg, str(tree), 'acc/lead', 'abc12345')
    assert not marker.exists()
    assert not (tmp_path / '.worktrees/child').exists()


@pytest.mark.parametrize("invalid", ["missing", "writable", "other_host", "restored"])
async def test_resume_requires_current_host_record(containers_env, api, env, project, tmp_path, invalid):
    await container_project(api, env, project)
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'old'})
    sid = response.json()['id']
    await wait_for(state_is(api, sid, 'idle'))
    stopped = await api.post(f'/api/sessions/{sid}/stop')
    assert stopped.status_code == 200, stopped.text
    await wait_for(lambda: any(r['operation'] == 'stop' for r in containers_env.requests), timeout=30)
    record = tmp_path / 'qualified/etc/acc/containers/host-qualified.json'
    if invalid in ('missing', 'restored'):
        record.unlink()
    elif invalid == 'writable':
        record.chmod(0o666)
    else:
        (tmp_path / 'qualified/etc/machine-id').write_text('b' * 32 + '\n')
    if invalid == 'restored':
        # A restored database can retain session.container independently of the project's current switch.
        with write_db(api.app.state.db) as db:
            db.get(Project, project['id']).containers = False
            db.commit()
    await api.post(f'/api/sessions/{sid}/resume')

    async def refused():
        events = (await api.get(f'/api/sessions/{sid}/events')).json()
        return [e for e in events if e['kind'] == 'launch_failed']
    events = await wait_for(refused)
    assert 'container launch requires a qualified local host' in events[0]['payload']['error']
    assert [r['generation'] for r in containers_env.requests if r['operation'] == 'launch'] == [1]
    with write_db(api.app.state.db) as db:
        assert db.query(AuditLog).filter_by(action='container.launch_refused', target=sid).count() >= 1


async def test_internal_launch_cannot_fall_back_to_native_for_container_project(containers_env, api, env, project):
    from acc.server.compiler import LaunchInputs
    from acc.server.supervisor import LaunchRefused
    await container_project(api, env, project)
    with pytest.raises(LaunchRefused, match='container'):
        await api.app.state.supervisor.create_session(project['id'], LaunchInputs(provider='claude'),
                                                     'helper', 'admin')


async def test_existing_team_cannot_start_native_helper_after_project_switch(containers_env, api, env, project):
    from test_team import lead, request
    leader = await lead(api, project, env)
    response = await api.post(f"/api/projects/{project['id']}/containers", json={'enabled': True})
    assert response.status_code == 200, response.text
    result = await request(api, env, leader, 'start', name='helper', agent='claude', profile=None, task='work')
    assert not result['ok'] and 'container' in result['error'], result
    from acc.server.models import Session, Team
    with write_db(api.app.state.db) as db:
        assert db.get(Team, leader['team_id']).started == 0
        assert db.query(Session).filter_by(parent_session_id=leader['id']).count() == 0


async def test_internal_container_creator_needs_record(containers_env, api, env, project, monkeypatch):
    from acc import container_host_record as hr
    from acc.server.compiler import LaunchInputs
    from acc.server.supervisor import LaunchRefused
    await container_project(api, env, project)
    monkeypatch.setattr(hr, 'check', lambda path=None: {'passed': False, 'reason': 'record missing'})
    with pytest.raises(LaunchRefused, match='record missing'):
        await api.app.state.supervisor.create_session(project['id'], LaunchInputs(provider='claude'),
                                                     'helper', 'admin', container=True)
    assert containers_env.requests == []


async def test_record_removed_after_request_before_dispatch(containers_env, api, env, project, tmp_path, monkeypatch):
    await container_project(api, env, project)
    broker = api.app.state.supervisor.broker
    original = broker.call

    async def lose_record(method, **params):
        if method == 'trust_workspace':
            (tmp_path / 'qualified/etc/acc/containers/host-qualified.json').unlink(missing_ok=True)
        return await original(method, **params)

    monkeypatch.setattr(broker, 'call', lose_record)
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'queued'})
    assert response.status_code == 201, response.text
    sid = response.json()['id']
    events = (await api.get(f'/api/sessions/{sid}/events')).json()
    assert any(e['kind'] == 'launch_failed' and 'qualified local host' in e['payload']['error'] for e in events)
    assert containers_env.requests == []
    assert not (env['settings'].containers_dir / 'grants' / f'{sid}.json').exists()


async def test_enabling_refusal_is_audited(containers_env, api, project, monkeypatch):
    from acc import container_host_record as hr
    monkeypatch.setattr(hr, 'check', lambda path=None: {'passed': False, 'reason': 'missing'})
    response = await api.post(f"/api/projects/{project['id']}/containers", json={'enabled': True})
    assert response.status_code == 409
    with write_db(api.app.state.db) as db:
        [audit] = db.query(AuditLog).filter_by(action='project.containers_refused').all()
        assert audit.target == project['slug'] and audit.detail == {'reason': 'host_not_qualified'}
        assert not db.get(Project, project['id']).containers


async def test_project_registration_cannot_import_container_enable(api, env, project):
    response = await api.post('/api/projects', json={'slug': 'imported', 'name': 'Imported',
        'workspace_path': str(env['workspaces'] / 'imported'), 'containers': True})
    assert response.status_code == 201, response.text
    assert response.json()['containers'] is False
