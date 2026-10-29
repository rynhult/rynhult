"""D4: compose files an agent can edit run as the human owner, so autostart only runs a stack whose files an owner
approved (acc.server.compose)."""
from __future__ import annotations

import pytest
from helpers import write_db

from acc.broker import ops
from acc.server.models import AttentionItem, AuditLog, Event, Project

FAKE_DOCKER = """#!/bin/sh
# docker compose stand-in: logs what it was asked; `config` resolves to compose.yaml plus .env
echo "$*" >> "{log}"
case "$2" in
  config) cat compose.yaml; cat .env 2>/dev/null; true ;;
  ps) ;;
esac
"""


@pytest.fixture
def docker(env, tmp_path, monkeypatch):
    log = tmp_path / "docker.log"
    script = tmp_path / "bin" / "docker"
    script.write_text(FAKE_DOCKER.replace("{log}", str(log)))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{script.parent}:{__import__('os').environ['PATH']}")
    (env["workspaces"] / "demo" / "compose.yaml").write_text("services:\n  web:\n    image: nginx\n")
    return log


def ups(log) -> int:
    return sum(line.startswith("compose up") for line in log.read_text().splitlines()) if log.exists() else 0


def test_digest_covers_overrides_env_and_the_resolved_config(env, docker):
    demo = env["workspaces"] / "demo"
    first = ops.compose_digest(env["broker"], str(demo))
    assert first["files"] == {"compose.yaml": "services:\n  web:\n    image: nginx\n"} and first["resolved"]
    (demo / "compose.override.yaml").write_text("services:\n  web:\n    volumes: [\"/home/owner:/h\"]\n")
    second = ops.compose_digest(env["broker"], str(demo))
    assert second["digest"] != first["digest"] and "compose.override.yaml" in second["files"]
    (demo / ".env").write_text("DATA=/home/owner\n")
    third = ops.compose_digest(env["broker"], str(demo))
    assert third["digest"] != second["digest"] and ".env" not in third["files"]  # counted, never shown
    with pytest.raises(ops.OpError, match="changed since they were approved"):
        ops.compose_up(env["broker"], str(demo), expect=first["digest"])
    assert ups(docker) == 0
    assert ops.compose_up(env["broker"], str(demo), expect=third["digest"]) == {"ok": True}
    assert ups(docker) == 1


async def test_autostart_needs_approved_files_and_holds_a_changed_stack(api, env, project, docker):
    pid, demo = project["id"], env["workspaces"] / "demo"
    assert project["compose_autostart"] is False and project["compose_approved"] is False  # off by default
    assert (await api.post(f"/api/projects/{pid}/runtime", json={"compose_autostart": True})).status_code == 409
    review = (await api.get(f"/api/projects/{pid}/runtime")).json()["compose"]
    assert not review["approved"] and "+    image: nginx" in review["diff"] and "Never approved" in review["note"]
    (demo / "compose.yaml").write_text("services:\n  web:\n    image: nginx:2\n")  # changed after the review
    stale = await api.post(f"/api/projects/{pid}/runtime", json={"compose_autostart": True, "digest": review["digest"]})
    assert stale.status_code == 409 and "Review them again" in stale.text
    review = (await api.get(f"/api/projects/{pid}/runtime")).json()["compose"]
    on = await api.post(f"/api/projects/{pid}/runtime", json={"compose_autostart": True, "digest": review["digest"]})
    assert on.status_code == 200 and on.json()["compose_autostart"] and on.json()["compose_approved"]
    assert (await api.get(f"/api/projects/{pid}/runtime")).json()["compose"]["approved"]

    supervisor = api.app.state.supervisor
    supervisor._autostarted.clear()
    assert await supervisor.autostart_projects()
    assert ups(docker) == 1  # approved: brought up

    # The agent adds a mount of the owner's home: the next start holds it
    (demo / "compose.yaml").write_text("services:\n  web:\n    image: nginx:2\n    volumes: [\"/home/owner:/h\"]\n")
    supervisor._autostarted.clear()
    assert await supervisor.autostart_projects()  # held is not a failure to retry
    assert ups(docker) == 1
    with api.app.state.db() as s:
        item = s.query(AttentionItem).filter_by(dedupe_key=f"compose:{pid}", resolved_at=None).one()
        assert item.owner is None and "autostart held" in item.title
        assert s.query(Event).filter_by(kind="project_autostart_held").count() == 1
    review = (await api.get(f"/api/projects/{pid}/runtime")).json()["compose"]
    assert not review["approved"] and '+    volumes: ["/home/owner:/h"]' in review["diff"]
    assert review["by"] == "@api" and review["at"]

    # An owner approves the change: the held stack comes up, the Inbox item resolves, the audit records it
    again = await api.post(f"/api/projects/{pid}/runtime", json={"compose_autostart": True, "digest": review["digest"]})
    assert again.status_code == 200 and ups(docker) == 2
    with api.app.state.db() as s:
        assert s.query(AttentionItem).filter_by(dedupe_key=f"compose:{pid}", resolved_at=None).count() == 0
        audit = s.query(AuditLog).filter_by(action="project.autostart").order_by(AuditLog.id.desc()).first()
        assert audit.detail["approved"] == review["digest"][:12] and audit.detail["files"] == ["compose.yaml"]

    # Only .env changed: still held, and the review says why no file differs
    (demo / ".env").write_text("X=1\n")
    review = (await api.get(f"/api/projects/{pid}/runtime")).json()["compose"]
    assert not review["approved"] and review["diff"] == "" and ".env" in review["note"]


async def test_a_stack_that_was_on_before_pins_existed_is_held(api, env, project, docker):
    pid = project["id"]
    with write_db(api.app.state.db) as s:  # autostart turned on before this release: no approved files
        s.get(Project, pid).compose_autostart = True
        s.commit()
    supervisor = api.app.state.supervisor
    supervisor._autostarted.clear()
    assert await supervisor.autostart_projects()
    assert ups(docker) == 0
    with api.app.state.db() as s:
        assert s.query(AttentionItem).filter_by(dedupe_key=f"compose:{pid}").count() == 1


@pytest.mark.parametrize("failure, expected", [("unavailable", 503), ("refused", 502)])
async def test_compose_approval_uses_shared_broker_error_contract(api, project, monkeypatch, failure, expected):
    from acc.server.broker_client import BrokerError, BrokerUnavailable

    async def failed(method, **params):
        assert method == "compose_digest"
        raise (BrokerUnavailable if failure == "unavailable" else BrokerError)("injected failure")

    monkeypatch.setattr(api.app.state.ctx.broker, "call", failed)
    response = await api.post(f"/api/projects/{project['id']}/runtime",
                              json={"compose_autostart": True, "digest": "a" * 64})
    assert response.status_code == expected
    with api.app.state.db() as s:
        assert s.get(Project, project["id"]).compose_autostart is False


async def test_compose_approval_invalid_input_keeps_validation_contract(api, project):
    response = await api.post(f"/api/projects/{project['id']}/runtime", json={"compose_autostart": []})
    assert response.status_code == 422


@pytest.mark.parametrize("sockets,host,engine", [
    ({"docker.sock"}, None, "docker"),
    ({"podman/podman.sock"}, None, "podman"),
    ({"docker.sock", "podman/podman.sock"}, None, "docker"),  # Docker wins: Debian/Ubuntu stay exactly as they were
    ({"podman/podman.sock"}, "unix:///elsewhere/docker.sock", "docker"),  # an explicit DOCKER_HOST is kept
    (set(), None, "docker"),  # nothing running: as before, Docker's own error says so
])
def test_project_stacks_use_podman_where_it_is_the_owners_engine(tmp_path, monkeypatch, sockets, host, engine):
    """Fedora/openSUSE run project stacks with Podman (docs/LINUX-DISTROS.md): `podman compose` with a compose v2
    provider, so ps/config/up and the approval digest work unchanged."""
    import socket as sockets_module
    runtime = tmp_path / "run"
    held = []
    for name in sockets:
        path = runtime / str(__import__("os").getuid()) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        server = sockets_module.socket(sockets_module.AF_UNIX)
        server.bind(str(path))
        held.append(server)
    monkeypatch.setattr(ops, "RUNTIME_ROOT", runtime)
    if host:
        monkeypatch.setitem(ops.environment, "DOCKER_HOST", host)
    else:
        monkeypatch.delitem(ops.environment, "DOCKER_HOST", raising=False)
    argv, env = ops.compose_command()
    uid = __import__("os").getuid()
    if engine == "podman":
        assert argv == ["podman", "compose"]
        assert env["DOCKER_HOST"] == f"unix://{runtime}/{uid}/podman/podman.sock"
    else:
        assert argv == ["docker", "compose"]
        assert env["DOCKER_HOST"] == (host or f"unix://{runtime}/{uid}/docker.sock")
    for server in held:
        server.close()
