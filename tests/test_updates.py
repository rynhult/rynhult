"""Updater contracts; all filesystem and command effects stay under temporary roots."""

import hashlib
import json
import ssl
import tarfile

import pytest
from test_auth import live  # noqa: F401 (fixture)

from acc import bundle
from acc.updates import newer, release, validate_request


@pytest.mark.parametrize(
    "candidate,current,want",
    [
        ("0.13.0", "0.12.0", True),
        ("0.12.0", "0.12.0", False),
        ("0.9.0", "0.12.0", False),
        ("1.10.0", "1.9.0", True),
        ("1.0rc1", "0.12.0", False),
    ],
)
def test_versions(candidate, current, want):
    assert newer(candidate, current) is want


def test_release_stable_and_artifacts():
    value = release(
        {"tag_name": "v0.13.0", "body": "<script>text</script>", "published_at": "2026-10-22", "assets": []}, "0.12.0"
    )
    assert value["available"] and value["notes"] == "<script>text</script>"
    for flags in ({"prerelease": True}, {"draft": True}):
        with pytest.raises(ValueError):
            release({"tag_name": "v0.13.0", **flags}, "0.12.0")


@pytest.mark.parametrize(
    "change",
    [
        {"version": "../../x"},
        {"action": "exec"},
        {"url": "https://example.com"},
        {"version": 1},
        {"version": "1.0rc1"},
        {"path": "/tmp/x"},
    ],
)
def test_request_allowlist(change):
    with pytest.raises(ValueError):
        validate_request({"action": "update", "version": "0.13.0", **change})


def test_request():
    assert validate_request({"action": "update", "version": "0.13.0"}) == "0.13.0"


@pytest.mark.parametrize(
    "name,kind",
    [
        ("acc-0.13.0/a", tarfile.SYMTYPE),
        ("/acc-0.13.0/a", tarfile.REGTYPE),
        ("acc-0.13.0/../a", tarfile.REGTYPE),
        ("other/a", tarfile.REGTYPE),
        ("acc-0.13.0/a", tarfile.LNKTYPE),
        ("acc-0.13.0/a", tarfile.CHRTYPE),
    ],
)
def test_archive_attacks(tmp_path, name, kind):
    path = tmp_path / "bundle.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.type = kind
        tar.addfile(info)
    with pytest.raises(ValueError):
        bundle.verify(path, "0.13.0", hashlib.sha256(path.read_bytes()).hexdigest())


def test_hash_and_identity(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion="0.13.0"\n')
    (source / "file").write_text("one")
    first = bundle.identity(source)
    (source / "file").write_text("two")
    assert first != bundle.identity(source)
    path = bundle.build(source, tmp_path / "out")
    with pytest.raises(ValueError):
        bundle.verify(path, "0.13.0", "0" * 64)
    bundle.verify(path, "0.13.0", hashlib.sha256(path.read_bytes()).hexdigest())


def test_github_checks():
    from acc.updates import check

    for tag, available in [("v0.13.0", True), ("v0.12.0", False), ("v0.11.0", False)]:
        assert (
            check("example/acc", "0.12.0", get=lambda *a: json.dumps({"tag_name": tag}).encode())["available"]
            is available
        )
    for data in ({"tag_name": "v0.13.0", "prerelease": True}, {"message": "rate limit"}, {"tag_name": "garbage"}):
        with pytest.raises(ValueError):
            check("example/acc", "0.12.0", get=lambda *a: json.dumps(data).encode())


def test_helper_transaction_and_rollback(tmp_path):
    from acc.update_helper import Transaction

    events = []

    class Fake(Transaction):
        def step(self, name, work):
            events.append(name)
            if name == "health":
                raise ValueError("unhealthy")
            return super().step(name, work)

        def backup(self):
            return None

        def download(self):
            return tmp_path / "new"

        def activate(self):
            pass

        def install(self, source):
            pass

        def restore(self):
            pass

        def health(self):
            pass

    source = tmp_path / "app"
    source.mkdir()
    (tmp_path / "new").mkdir()
    job = Fake(tmp_path / "updates", source, tmp_path / "data", "0.13.0", {})
    result = job.apply()
    assert not result["ok"] and result["rolled_back"]
    assert events == ["backup", "download", "activate", "install", "health", "rollback"]


def test_backup_failure_stops(tmp_path):
    from acc.update_helper import Transaction

    class Fake(Transaction):
        def backup(self):
            raise ValueError("backup failed")

        def download(self):
            pytest.fail("download after failed backup")

    source = tmp_path / "app"
    source.mkdir()
    result = Fake(tmp_path / "updates", source, tmp_path / "data", "0.13.0", {}).apply()
    assert not result["ok"] and [s["name"] for s in result["steps"]] == ["backup"]


def test_postgres_update_refuses_stale_sqlite_backup_before_download(tmp_path, monkeypatch):
    import datetime
    import subprocess
    from pathlib import Path

    from acc.update_helper import Transaction

    secret_url = 'postgresql+psycopg://acc:synthetic-private-password@db.example.lan/acc'
    monkeypatch.setenv('ACC_DATABASE_URL', secret_url)
    downloaded = []

    def legacy_backup(argv, *, env, **kwargs):
        saved = Path(env['ACC_BACKUP_DIR']) / datetime.date.today().isoformat()
        saved.mkdir(parents=True)
        (saved / 'acc.db').write_bytes(b'stale SQLite database')
        (saved / 'secrets').mkdir()
        return subprocess.CompletedProcess(argv, 0)

    class Local(Transaction):
        def download(self):
            downloaded.append(True)
            raise ValueError('download reached after an unsuitable backup')

    source = tmp_path / 'app'
    source.mkdir()
    result = Local(tmp_path / 'updates', source, tmp_path / 'data', '0.13.0', {}, run=legacy_backup).apply()
    assert not result['ok']
    assert 'PostgreSQL' in result['detail']
    assert not downloaded
    assert secret_url not in json.dumps(result)


async def test_owner_api_check_and_dedupe(api, env, monkeypatch):
    from acc.server import updating

    monkeypatch.setattr(
        updating,
        "check",
        lambda *a: {"latest": "0.13.0", "available": True, "notes": "<b>plain</b>", "published_at": "2026-10-22"},
    )
    assert (await api.post("/api/updates/check")).status_code == 200
    assert (await api.post("/api/updates/check")).status_code == 200
    state = (await api.get("/api/updates")).json()
    assert state["latest"] == "0.13.0" and state["notes"] == "<b>plain</b>"
    items = (await api.get("/api/attention")).json()
    assert len([i for i in items if i["kind"] == "update"]) == 1
    api.app.dependency_overrides[api.app.state.ctx.owner] = lambda: (_ for _ in ()).throw(
        __import__("fastapi").HTTPException(403)
    )
    assert (await api.get("/api/updates")).status_code == 403
    assert (await api.post("/api/updates/check")).status_code == 403
    api.app.dependency_overrides.clear()


def test_node_update_frame_and_result(tmp_path):
    from acc.node.updates import Updates

    root = tmp_path / "updates"
    (root / "requests").mkdir(parents=True)
    node = Updates(root)
    frame = {"t": "update", "version": "0.13.0", "build": "a" * 64, "sha256": "b" * 64}
    node.handle(frame)
    queued = list((root / "requests").glob("*.json"))
    assert len(queued) == 1
    assert json.loads(queued[0].read_text()) == {
        "action": "update",
        "version": "0.13.0",
        "build": "a" * 64,
        "sha256": "b" * 64,
    }
    (root / "state.json").write_text(json.dumps({"ok": True, "version": "0.13.0"}))
    assert node.message()["result"]["ok"]
    node.handle({**frame, "url": "https://example.com"})
    assert node.message()["refused"]


def test_node_identity_in_handshake():
    from acc.node.identity import node_info

    assert len(node_info()["build"]) == 64


@pytest.mark.parametrize("failure", ["install", "health", None])
def test_real_source_swap_and_database_rollback(tmp_path, failure):
    import shutil
    import sqlite3

    from acc.update_helper import Transaction

    root = tmp_path / "updates"
    source = tmp_path / "app"
    data = tmp_path / "data"
    source.mkdir()
    data.mkdir()
    (source / "old").write_text("previous source")
    with sqlite3.connect(data / "acc.db") as s:
        s.execute("create table item(value)")
        s.execute("insert into item values (1)")
    events = []

    class Local(Transaction):
        def backup(self):
            self.saved = tmp_path / "saved"
            self.saved.mkdir()
            shutil.copyfile(data / "acc.db", self.saved / "acc.db")
            events.append("backup")

        def download(self):
            self.stage.mkdir()
            tree = self.stage / "new"
            tree.mkdir()
            (tree / "new").touch()
            events.append("download")
            return tree

        def install(self, tree):
            if (tree / "new").exists():
                events.append("install new")
                with sqlite3.connect(data / "acc.db") as s:
                    s.execute("update item set value=2")
                if failure == "install":
                    raise ValueError("install failed")
            else:
                events.append("install previous")

        def health(self):
            events.append("health")
            if failure == "health":
                raise ValueError("unhealthy")

        def command(self, argv, env=None):
            assert argv == ["systemctl", "stop", "accd.service", "accd.socket"]
            events.append("stop")

    job = Local(root, source, data, "0.13.0", {})
    result = job.apply()
    with sqlite3.connect(data / "acc.db") as s:
        value = s.execute("select value from item").fetchone()[0]
    assert result["ok"] is (failure is None)
    if failure:
        assert value == 1 and (source / "old").exists() and result["rolled_back"]
        assert events[-2:] == ["stop", "install previous"]
    else:
        assert value == 2 and (source / "new").exists() and (job.previous / "old").exists()


async def test_member_cannot_see_updates(api, env, member_accounts, monkeypatch):
    from test_auth import do_setup
    from test_users import browser, csrf, join

    from acc.server import updating

    monkeypatch.setattr(updating, "check", lambda *a: {"latest": "0.13.0", "available": True, "notes": ""})
    await api.post("/api/updates/check")
    await do_setup(api, env)
    link = (await api.post("/api/users/invites", json={"role": "member", "account": "dana-agent"})).json()["link"]
    member = browser(api)
    assert (await join(member, link, "dana")).status_code == 200
    for method, url, payload in [
        ("GET", "/api/updates", None),
        ("POST", "/api/updates/check", {}),
        ("POST", "/api/updates/apply", {}),
        ("PUT", "/api/updates/config", {"repository": "example/acc"}),
        ("POST", "/api/updates/machines/all", {}),
        ("POST", "/api/updates/machines/lab/follow", {"enabled": True}),
    ]:
        assert (await member.request(method, url, json=payload, headers=csrf(member))).status_code == 403
    assert not [i for i in (await member.get("/api/attention")).json() if i["kind"] == "update"]
    await member.aclose()


@pytest.mark.parametrize("status", [403, 429, 500])
def test_network_errors_are_sanitized(monkeypatch, status):
    import urllib.error

    from acc import updates

    class Opener:
        def open(self, *a, **kw):
            raise urllib.error.HTTPError("https://example.com/TOKEN", status, "TOKEN", {}, None)

    monkeypatch.setattr(updates.urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(ValueError) as error:
        updates.fetch("https://api.github.com/repos/example/acc/releases/latest")
    assert "TOKEN" not in str(error.value)
    assert ("rate limited" in str(error.value)) is (status == 429)


def test_semver_prerelease_and_build_metadata():
    assert newer("1.0.0", "1.0.0-rc.1")
    assert not newer("1.0.0-rc.2", "1.0.0-rc.1")  # candidates must be stable
    assert not newer("1.0.0+new", "1.0.0+old")
    with pytest.raises(ValueError):
        release({"tag_name": "v1.0.0-rc.1"}, "0.12.0")


def test_same_version_different_release_build():
    from acc.updates import check

    metadata = {
        "tag_name": "v0.12.0",
        "assets": [{"name": "BUILD", "url": "https://api.github.com/repos/example/acc/releases/assets/123"}],
    }

    def get(url, *args, **kwargs):
        return ("b" * 64).encode() if url.endswith("/123") else json.dumps(metadata).encode()

    result = check("example/acc", "0.12.0", current_build="a" * 64, get=get)
    assert result["available"] and result["latest_build"] == "b" * 64
    assert not check("example/acc", "0.12.0", current_build="b" * 64, get=get)["available"]


def test_helper_download_rechecks_release_and_build(tmp_path, monkeypatch):
    from acc import update_helper

    source = tmp_path / "app"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion="0.12.0"\n')
    new = tmp_path / "release"
    new.mkdir()
    (new / "pyproject.toml").write_text('[project]\nversion="0.13.0"\n')
    archive = bundle.build(new, tmp_path / "bundles")
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    build = bundle.identity(new)
    metadata = {
        "latest": "0.13.0",
        "available": True,
        "latest_build": build,
        "assets": {
            "SHA256SUMS": "https://api.github.com/repos/example/acc/releases/assets/1",
            "acc-0.13.0.tar.gz": "https://api.github.com/repos/example/acc/releases/assets/2",
        },
    }
    monkeypatch.setattr(update_helper, "check", lambda *a: metadata)

    def fetch(url, *a, **kw):
        assert kw["asset"]
        return (sha + "  acc-0.13.0.tar.gz\n").encode() if url.endswith("/1") else archive.read_bytes()

    monkeypatch.setattr(update_helper, "fetch", fetch)
    job = update_helper.Transaction(
        tmp_path / "updates", source, tmp_path / "data", "0.13.0", {"repository": "example/acc"}
    )
    assert (job.download() / "pyproject.toml").is_file() and job.expected_build == build


def test_node_helper_uses_enrolled_primary_and_tls(tmp_path, monkeypatch):
    from acc import update_helper

    source = tmp_path / "app"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion="0.12.0"\n')
    new = tmp_path / "release"
    new.mkdir()
    (new / "pyproject.toml").write_text('[project]\nversion="0.12.0"\n')
    archive = bundle.build(new, tmp_path / "bundles")
    config = tmp_path / "node.env"
    config.write_text("ACC_NODE_PRIMARY=https://acc.example.lan\nACC_NODE_ID=demo\nACC_NODE_TRUST=system\n")

    def fetch(url, *a, **kw):
        assert url == "https://acc.example.lan/api/nodes/bundle"
        assert kw["context"].check_hostname and kw["context"].verify_mode == __import__("ssl").CERT_REQUIRED
        return archive.read_bytes()

    monkeypatch.setattr(update_helper, "fetch", fetch)
    monkeypatch.setattr(update_helper, "check", lambda *a: pytest.fail("a node contacted GitHub"))
    job = update_helper.NodeTransaction(
        tmp_path / "updates",
        source,
        tmp_path / "data",
        "0.12.0",
        {
            "node_config": str(config),
            "node_state": str(tmp_path / "node-state"),
            "build": bundle.identity(new),
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        },
    )
    assert (job.download() / "pyproject.toml").is_file()


def test_helper_entry_rejects_injected_requests_under_temporary_root(tmp_path, monkeypatch):
    from acc import update_helper

    root = tmp_path / "updates"
    (root / "requests").mkdir(parents=True)
    source = tmp_path / "app"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion="0.12.0"\n')
    (source / ".git").touch()
    monkeypatch.setenv("ACC_UPDATE_ROOT", str(root))
    monkeypatch.setenv("ACC_UPDATE_SOURCE", str(source))
    monkeypatch.setenv("ACC_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("ACC_UPDATE_GROUP", "")
    monkeypatch.setattr(update_helper.subprocess, "run", lambda *a, **kw: pytest.fail("ran a privileged command"))
    for body in (
        {"action": "update", "version": "0.13.0", "url": "https://example.com/evil"},
        {"action": "update", "version": "0.13.0"},
    ):
        (root / "requests" / ("a" * 32 + ".json")).write_text(json.dumps(body))
        assert update_helper.main() == 0
        result = json.loads((root / "state.json").read_text())
        assert not result["ok"] and not result["running"]
    assert "git checkout" in result["detail"]


async def test_daily_check_opt_in_time_and_result_audit(api, env, tmp_path, monkeypatch):
    import dataclasses

    from acc.server import updating

    service = api.app.state.ctx.updates
    source = tmp_path / "release-install"
    source.mkdir()
    (source / ".acc-build").write_text("a" * 64)
    service.settings = dataclasses.replace(service.settings, update_source=source)
    root = service.settings.updates_dir
    (root / "requests").mkdir(parents=True)
    calls = []

    def check(*a):
        calls.append(a)
        return {"latest": "99.0.0", "available": True, "notes": "demo"}

    monkeypatch.setattr(updating, "check", check)
    assert not service.config()["automatic"]
    await service.tick()
    await service.tick()
    assert len(calls) == 1 and not list((root / "requests").glob("*.json"))
    assert (
        await api.put(
            "/api/updates/config",
            json={"repository": service.config()["repository"], "automatic": True, "time": "00:00"},
        )
    ).status_code == 200
    await service.tick()
    await service.tick()
    assert len(list((root / "requests").glob("*.json"))) == 1
    (root / "state.json").write_text(
        json.dumps({"id": "demo-result", "version": "99.0.0", "ok": True, "finished_at": 1})
    )
    await service.tick()
    await service.tick()
    entries = (await api.get("/api/audit")).json()
    assert len([a for a in entries if a["action"] == "update.result"]) == 1


def test_rollback_refuses_a_database_symlink(tmp_path):
    import sqlite3

    from acc.update_helper import Transaction

    data = tmp_path / "data"
    data.mkdir()
    saved = tmp_path / "saved"
    saved.mkdir()
    source = tmp_path / "app"
    source.mkdir()
    sentinel = tmp_path / "sentinel.db"
    for path, value in ((sentinel, 99), (saved / "acc.db", 1)):
        with sqlite3.connect(path) as s:
            s.execute("create table item(value)")
            s.execute("insert into item values (?)", (value,))
    (data / "acc.db").symlink_to(sentinel)
    job = Transaction(tmp_path / "updates", source, data, "99.0.0", {})
    job.saved = saved
    job.install_started = True
    job.command = lambda *a: None
    job.install = lambda *a: None
    with pytest.raises((ValueError, OSError)):
        job.restore()
    with sqlite3.connect(sentinel) as s:
        assert s.execute("select value from item").fetchone()[0] == 99


def test_running_source_version_wins_over_another_editable_install(monkeypatch):
    import tomllib
    from pathlib import Path

    import acc

    expected = tomllib.loads((Path(acc.__file__).resolve().parents[2] / "pyproject.toml").read_text())["project"][
        "version"
    ]
    monkeypatch.setattr(acc, "version", lambda package: "99.0.0")
    assert acc._version() == expected


def test_identity_cli_preserves_full_source_stamp_for_node_installers(tmp_path, capsys):
    source = tmp_path / "full-source"
    source.mkdir()
    (source / ".acc-build").write_text("a" * 64)
    assert bundle.main(["--source", str(source), "--identity"]) == 0
    assert capsys.readouterr().out.strip() == "a" * 64


def test_main_uses_fresh_install_entry_point(tmp_path):
    from acc.update_helper import Transaction

    source = tmp_path / "app"
    source.mkdir()
    job = Transaction(tmp_path / "updates", source, tmp_path / "data", "99.0.0", {})
    commands = []
    job.command = lambda argv: commands.append(argv)
    job.install(source)
    assert commands == [["bash", source / "deploy/setup.sh", "--yes"]]


def test_activation_keeps_the_served_web_build_until_the_installer_publishes_its_replacement(tmp_path):
    from acc.update_helper import Transaction

    source, incoming = tmp_path / 'app', tmp_path / 'incoming'
    static = source / 'src/acc/server/static'
    (static / 'assets').mkdir(parents=True)
    (static / 'index.html').write_text('old HTML')
    (static / 'assets/old.js').write_text('old chunk')
    incoming.mkdir()
    job = Transaction(tmp_path / 'updates', source, tmp_path / 'data', '99.0.0', {})
    job.new_source = incoming
    job.activate()
    assert (static / 'index.html').read_text() == 'old HTML'
    assert (static / 'assets/old.js').read_text() == 'old chunk'
    assert (job.previous / 'src/acc/server/static/assets/old.js').read_text() == 'old chunk'


def test_helper_cannot_be_redirected_by_acc_owned_repository_setting(tmp_path, monkeypatch):
    from acc import update_helper

    root = tmp_path / "updates"
    (root / "requests").mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    (data / "updates-config.json").write_text(json.dumps({"repository": "untrusted/acc"}))
    source = tmp_path / "app"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion="0.12.0"\n')
    monkeypatch.setenv("ACC_UPDATE_ROOT", str(root))
    monkeypatch.setenv("ACC_DATA", str(data))
    monkeypatch.setenv("ACC_UPDATE_SOURCE", str(source))
    monkeypatch.setenv("ACC_UPDATE_GROUP", "")
    monkeypatch.setenv("ACC_SOURCE_URL", "https://github.com/example/acc")
    monkeypatch.delenv("ACC_UPDATE_REPOSITORY", raising=False)
    monkeypatch.setattr(
        update_helper.Transaction,
        "apply",
        lambda self: pytest.fail("unprivileged configuration changed root code source"),
    )
    (root / "requests" / ("a" * 32 + ".json")).write_text(json.dumps({"action": "update", "version": "99.0.0"}))
    assert update_helper.main() == 0
    assert not json.loads((root / "state.json").read_text())["ok"]


def test_node_helper_pins_the_enrolled_ca_and_ignores_appended_roots(tmp_path, monkeypatch):
    from test_node import CA

    from acc import update_helper

    ca = CA(tmp_path / "ca")
    other = CA(tmp_path / "other")
    ca.path.write_bytes(ca.pem + other.pem)
    source = tmp_path / "app"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion="0.12.0"\n')
    archive = bundle.build(source, tmp_path / "bundles")
    config = tmp_path / "node.env"

    def configure(fingerprint):
        config.write_text(
            f"ACC_NODE_PRIMARY=https://acc.example.lan\nACC_NODE_ID=demo\nACC_NODE_TRUST=ca\nACC_NODE_CA_FILE={ca.path}\nACC_NODE_CA_FINGERPRINT={fingerprint}\n"
        )

    options = {
        "node_config": str(config),
        "node_state": str(tmp_path / "node-state"),
        "build": bundle.identity(source),
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    configure("0" * 64)
    monkeypatch.setattr(update_helper, "fetch", lambda *a, **kw: pytest.fail("downloaded without checking enrolled CA"))
    with pytest.raises(ValueError, match="fingerprint"):
        update_helper.NodeTransaction(tmp_path / "updates", source, tmp_path / "data", "0.12.0", options).download()
    configure(ca.fingerprint)

    def fetch(url, *a, **kw):
        certs = kw["context"].get_ca_certs(binary_form=True)
        assert len(certs) == 1 and hashlib.sha256(certs[0]).hexdigest() == ca.fingerprint
        return archive.read_bytes()

    monkeypatch.setattr(update_helper, "fetch", fetch)
    assert (
        update_helper.NodeTransaction(tmp_path / "updates", source, tmp_path / "data", "0.12.0", options)
        .download()
        .is_dir()
    )


async def test_repository_policy_is_shown_before_installation(api, tmp_path):
    import dataclasses

    service = api.app.state.ctx.updates
    source = tmp_path / "installed"
    source.mkdir()
    (source / ".acc-build").write_text("a" * 64)
    service.settings = dataclasses.replace(service.settings, update_source=source, update_repository="example/acc")
    response = await api.put("/api/updates/config", json={"repository": "other/acc"})
    assert response.status_code == 200 and "ACC_UPDATE_REPOSITORY" in response.json()["blocked"]
    assert (await api.post("/api/updates/apply")).status_code == 409


def test_health_socket_default_matches_accd_socket_unit():
    """The helper's health check must reach the socket accd really listens on, or every update rolls back."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    listen = [line.split("=", 1)[1].strip() for line in (root / "deploy/accd.socket").read_text().splitlines()
              if line.startswith("ListenStream=")]
    helper = (root / "src/acc/update_helper.py").read_text()
    assert listen and all(f'"{path}"' in helper for path in listen)
    assert "/run/accd/accd.sock" not in helper


@pytest.mark.parametrize("saved", [
    "{", "[]", '{"automatic":"false"}', '{"time":"25:00"}',
    '{"repository":42}', '{"repository":"../acc"}', '{"follow":{"node":"yes"}}',
    '{"unexpected":true}',
])
async def test_corrupt_config_blocks_updates_and_can_be_explicitly_repaired(api, saved, monkeypatch, caplog):
    from acc.server import updating

    service = api.app.state.ctx.updates
    path = service.settings.data_dir / "updates-config.json"
    path.write_text(saved)
    monkeypatch.setattr(updating, "check", lambda *a: pytest.fail("release check with corrupt config"))
    service.store({"available": True, "latest": "99.0.0"})
    state = (await api.get("/api/updates")).json()
    assert state["config_error"] and "configuration" in state["blocked"].lower()
    assert state["automatic"] is False and state["follow"] == {}
    await service.tick()
    assert (await api.post("/api/updates/check")).json()["config_error"]
    response = await api.post("/api/updates/apply")
    assert response.status_code == 409 and "configuration" in response.json()["detail"].lower()
    assert path.read_text() == saved
    assert "configuration" in caplog.text.lower()
    repaired = await api.put("/api/updates/config", json={"repository": "", "automatic": False, "time": "03:00"})
    assert repaired.status_code == 200 and not repaired.json().get("config_error")
    assert json.loads(path.read_text()) == {"repository": "", "automatic": False, "time": "03:00", "follow": {}}


async def test_unreadable_config_is_not_first_run(api, monkeypatch):
    from acc.server import updating

    original = updating.read_json

    def denied(path):
        if path.name == "updates-config.json":
            raise PermissionError("private diagnostic")
        return original(path)

    monkeypatch.setattr(updating, "read_json", denied)
    state = (await api.get("/api/updates")).json()
    assert state["config_error"] and "private diagnostic" not in json.dumps(state)


async def test_missing_config_is_first_run(api):
    state = (await api.get("/api/updates")).json()
    assert not state.get("config_error") and state["automatic"] is False


async def test_release_observation_preserves_legacy_state_until_next_write(api):
    from acc.server.models import Observation, Setting

    service = api.app.state.ctx.updates
    with service.db() as s:
        s.add(Setting(key="updates", value={"latest": "99.0.0"}))
        s.commit()
    assert service.observation()["latest"] == "99.0.0"
    service.store({"latest": "99.1.0"})
    with service.db() as s:
        assert s.get(Observation, "updates.release").value["latest"] == "99.1.0"
        assert s.get(Setting, "updates") is None


async def test_corrupt_updater_config_does_not_break_machine_listing_or_reconnect_follow(api):
    from types import SimpleNamespace

    from acc.server.models import Node

    service = api.app.state.ctx.updates
    (service.settings.data_dir / "updates-config.json").write_text("{")
    with service.db() as s:
        s.add(Node(id="corrupt-config-node", name="lab-pc", status="active", public_key="example",
                   created_by="@api", info={"os": "linux"}))
        s.commit()
    response = await api.get("/api/nodes")
    assert response.status_code == 200
    machine = next(n for n in response.json() if n["name"] == "lab-pc")
    assert machine["follow"] is False
    # A reconnect still reaches the normal link loop, without enqueueing an update.
    link = SimpleNamespace(name="lab-pc", info={"os": "linux", "build": "old"}, connected_at=0)
    service.follow(link)

@pytest.mark.parametrize('info', [
    {'os': 'linux'},
    {'os': 'linux', 'capabilities': ['update']},
    {'os': 'linux', 'build': 'unknown', 'capabilities': ['update']},
    {'os': 'linux', 'build': 'a' * 64},
    {'os': 'linux', 'build': 'a' * 64, 'capabilities': ['update']},
])
async def test_silent_machine_update_has_bounded_owner_feedback(api, env, monkeypatch, info):
    from acc.server import updating
    from acc.server.models import Node
    from acc.server.nodes import NodeLink

    ctx = api.app.state.ctx
    object.__setattr__(ctx.settings, "allowed_origins", ("https://acc.example.lan",))
    async def send(data):
        pass  # An old node silently ignores messages it does not understand.
    link = NodeLink('old-node', 'old-pc', send, info, [])
    ctx.broker.hub.attach(link)
    with ctx.db() as s:
        s.add(Node(id='old-node', name='old-pc', status='active', info=info))
        s.commit()
    monkeypatch.setattr(ctx.updates, 'bundle_descriptor', lambda: {'version': '0.13.0', 'build': 'b' * 64, 'sha256': 'c' * 64})
    try:
        result = (await api.post('/api/updates/machines/old-pc')).json()['old-pc']
        row = next(n for n in (await api.get('/api/nodes')).json() if n['name'] == 'old-pc')
        if 'update' in info.get('capabilities', []) and len(info.get('build', '')) == 64:
            assert result['queued'] and row['update']['running']
            requested = row['update']['requested_at']
            monkeypatch.setattr(updating.time, 'time', lambda: requested + 121)
            row = next(n for n in (await api.get('/api/nodes')).json() if n['name'] == 'old-pc')
        else:
            assert not result['queued']
        assert row['update']['installer_required'] and not row['update']['running']
        assert "didn't respond" in row['update']['detail']
        assert '/api/nodes/bundle' in row['update']['recovery']['command']
        assert '--update' in row['update']['recovery']['command']
        # A later read (or reconnect) retains the failure for the owner.
        assert next(n for n in (await api.get('/api/nodes')).json() if n['name'] == 'old-pc')['update'] == row['update']
    finally:
        ctx.broker.hub.detach(link)


@pytest.mark.parametrize("info, prefix, note", [
    ({"os": "linux", "wsl": "0"}, "sudo sh -c '", "Linux terminal"),
    ({"os": "linux", "wsl": "2", "wsl_distro": "Ubuntu-24.04"}, "wsl -d 'Ubuntu-24.04' -u root --exec sh -c '", "PowerShell"),
    ({"os": "linux", "system": "wsl"}, "wsl -d '<distro>' -u root --exec sh -c '", "wsl --list --quiet"),
    ({"os": "linux"}, "sudo sh -c '", "WSL"),
])
async def test_old_machine_recovery_is_specific_and_keeps_enrollment(api, info, prefix, note):
    from acc.server.models import Node

    ctx = api.app.state.ctx
    object.__setattr__(ctx.settings, "allowed_origins", ("https://acc.example.lan",))
    with ctx.db() as s:
        s.add(Node(id="recover", name="lab-pc", status="active",
                   info={**info, "update": {"installer_required": True, "ok": False,
                                          "detail": "Its Rynhult is too old; rerun its installer."}}))
        s.commit()
    row = next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "lab-pc")
    recovery = row["update"]["recovery"]
    assert "rerun its installer" not in row["update"]["detail"]
    assert recovery["command"].startswith(prefix)
    assert "https://acc.example.lan/api/nodes/bundle" in recovery["command"]
    assert "deploy/node/install.sh --update" in recovery["command"]
    assert note in recovery["detail"]
    assert "enrollment" in recovery["detail"]
    assert not any(secret in recovery["command"] for secret in ("--code", "enroll", "node.key", "--insecure", " -k "))
    if prefix.startswith("wsl"):
        script = recovery["command"].split("--exec sh -c '", 1)[1][:-1]
        assert '"' not in script
        # Without --exec, wsl.exe hands the line to root's login shell, which PowerShell gives the script in double
        # quotes: $(mktemp) and $work would expand there, before sh -c runs, and the update would unpack into /.
        assert recovery["command"].split(" sh -c ", 1)[0].endswith(" -u root --exec")
        assert recovery["root_command"] == f"sh -c '{script}'"


@pytest.mark.parametrize("download_ok", [True, False])
async def test_recovery_command_executes_update_and_cleans_up(api, tmp_path, download_ok):
    import io
    import os
    import subprocess
    import tarfile
    from pathlib import Path

    from acc.server.models import Node

    ctx = api.app.state.ctx
    object.__setattr__(ctx.settings, "allowed_origins", ("https://acc.example.lan",))
    with ctx.db() as s:
        s.add(Node(id="run-recover", name="lab-pc", status="active", info={
            "os": "linux", "wsl": "2", "update": {"installer_required": True}}))
        s.commit()
    row = next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "lab-pc")
    command = row["update"]["recovery"]["root_command"]
    archive = tmp_path / "source.tar.gz"
    record = tmp_path / "installed"
    installer = f'printf "%s\\n" "$@" > {record}\npwd >> {record}\n'.encode()
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("acc-0.14.0/deploy/node/install.sh")
        member.size = len(installer)
        tar.addfile(member, io.BytesIO(installer))
    tools = tmp_path / "recovery-tools"
    tools.mkdir()
    curl = tools / "curl"
    curl.write_text(
        '#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\n'
        f'printf "%s" "$2" > {tmp_path}/download\n'
        + (f'cp {archive} "$2"\n' if download_ok else "exit 22\n"))
    curl.chmod(0o755)
    result = subprocess.run(["sh", "-c", command], env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
                            text=True, capture_output=True)
    if download_ok:
        assert result.returncode == 0, result.stderr
        assert record.read_text().splitlines()[0] == "--update"
    else:
        assert result.returncode == 22
        assert not record.exists()
    assert not Path((tmp_path / "download").read_text()).parent.exists()


async def test_recovery_never_copies_credentials_from_an_origin(api):
    from acc.server.models import Node

    ctx = api.app.state.ctx
    object.__setattr__(ctx.settings, "allowed_origins", ("https://user:SECRET@acc.example.lan/?token=SECRET",))
    with ctx.db() as s:
        s.add(Node(id="bad-origin", name="lab-pc", status="active",
                   info={"os": "linux", "update": {"installer_required": True}}))
        s.commit()
    row = next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "lab-pc")
    recovery = row["update"]["recovery"]
    assert not recovery.get("command")
    assert "HTTPS" in recovery["detail"]
    assert "SECRET" not in json.dumps(recovery)


@pytest.mark.parametrize("distro", ["Ubuntu'; SECRET", "Ubuntu Lab", "Ubuntu$(SECRET)", "Ubuntu`SECRET`", "Ubuntu\nSECRET", "-u", "Ubuntu\"SECRET", "Ubuntu\u202eSECRET", "x" * 129])
async def test_wsl_recovery_uses_enrolled_ca_and_does_not_interpolate_unsafe_distro(api, tmp_path, distro):
    from acc.server.models import Node

    ctx = api.app.state.ctx
    object.__setattr__(ctx.settings, "allowed_origins", ("https://acc.example.lan",))
    from cryptography.hazmat.primitives.serialization import Encoding
    from test_installer import make_ca
    (tmp_path / "root.pem").write_bytes(make_ca("Recovery test CA")[1].public_bytes(Encoding.PEM))
    object.__setattr__(ctx.settings, "node_ca_file", tmp_path / "root.pem")
    with ctx.db() as s:
        s.add(Node(id="private-recover", name="lab-pc", status="active", info={
            "os": "linux", "wsl": "2", "wsl_distro": distro,
            "update": {"installer_required": True}}))
        s.commit()
    row = next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "lab-pc")
    recovery = row["update"]["recovery"]
    # Root never trusts the node's own CA file, which the acc-node account can replace (final RC audit, F-2): the
    # main's CA travels in the command and is checked against its fingerprint first.
    assert "/var/lib/acc-node/ca.pem" not in recovery["command"]
    der = ssl.PEM_cert_to_DER_cert((tmp_path / "root.pem").read_text())
    assert f"= x{hashlib.sha256(der).hexdigest()} " in recovery["command"]
    assert recovery["command"].index("openssl x509") < recovery["command"].index("curl ")
    assert "--cacert $work/ca.pem" in recovery["command"]
    assert "wsl -d '<distro>'" in recovery["command"]
    assert "SECRET" not in json.dumps(recovery)
    assert '"' not in recovery["root_command"]


@pytest.mark.parametrize("version, available", [("0.14.0", False), ("1.0.0", True)])
async def test_mac_update_only_links_to_a_build_with_installers(api, monkeypatch, version, available):
    from acc.server.api import nodes
    from acc.server.models import Node

    original = nodes.release_links
    monkeypatch.setattr(nodes, "release_links", lambda source: original(source, version))
    object.__setattr__(api.app.state.ctx.settings, "source_url", "https://github.com/example-org/acc")
    object.__setattr__(api.app.state.ctx.settings, "allowed_origins", ("https://acc.example.lan",))
    with api.app.state.ctx.db() as s:
        s.add(Node(id="mac-recover", name="studio-mac", status="active", info={"os": "darwin"}))
        s.commit()
    row = next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "studio-mac")
    assert bool(row.get("installer")) is available
    recovery = row["update"]["recovery"]
    if available:
        assert "Download and run the Mac installer" in recovery["detail"] and "command" not in recovery
    else:  # pre-v1: the Mac installer again from this main's source, keeping the enrollment
        assert "next installer release" not in recovery["detail"]
        assert recovery["command"].startswith("sh -c 'set -eu; ")
        assert "https://acc.example.lan/api/nodes/bundle" in recovery["command"]
        assert recovery["command"].endswith("bash $work/source/deploy/node/macos/install.sh'")
        assert not any(word in recovery["command"] for word in ("enroll", "--code", "sudo", " -k ", "--insecure"))
        assert "root_command" not in recovery


async def test_current_node_result_clears_timeout_but_stale_result_does_not(api, env, live, monkeypatch):  # noqa: F811
    import asyncio

    from helpers import wait_for
    from test_multihost import StandInNode, enroll

    from acc.server import updating

    ctx = api.app.state.ctx
    key, node_id = await enroll(api)
    node = StandInNode(env['settings'].broker_socket, key, node_id,
                       info={'os': 'linux', 'build': 'a' * 64, 'capabilities': ['update']})
    task = asyncio.create_task(node.run(live))
    monkeypatch.setattr(ctx.updates, 'bundle_descriptor', lambda: {'version': '0.13.0', 'build': 'b' * 64, 'sha256': 'c' * 64})
    async def state():
        return next(n for n in (await api.get('/api/nodes')).json() if n['name'] == 'pc')['update']
    try:
        await wait_for(lambda: hasattr(node, 'ws'))
        response = (await api.post('/api/updates/machines/pc')).json()['pc']
        assert response['queued']
        await wait_for(lambda: any(m.get('t') == 'update' for m in node.texts))
        requested = (await state())['requested_at']
        await node.ws.send(json.dumps({'t': 'update-state', 'result': {'ok': True, 'at': requested - 1, 'id': 'old'}}))
        await node.ws.send(json.dumps({'t': 'ping'}))
        await wait_for(lambda: any(m.get('t') == 'pong' for m in node.texts))
        assert (await state()).get('awaiting_result')
        await node.ws.send(json.dumps({'t': 'update-state', 'result': {'ok': True, 'at': requested + 1, 'id': 'new'}}))
        async def finished():
            return (await state()).get('id') == 'new'
        await wait_for(finished)
        monkeypatch.setattr(updating.time, 'time', lambda: requested + 121)
        assert (await state())['ok'] and not (await state()).get('installer_required')
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_public_rc_orders_between_installed_preview_and_stable_without_auto_opt_in():
    from acc.updates import version

    assert version('0.14.0') < version('0.15.0-rc.1') < version('0.15.0')
    assert version('0.15.0-rc.1') < version('0.15.0-rc.2') < version('0.15.0-rc.10')
    assert not newer('0.15.0-rc.1', '0.14.0')  # manual RC installation only
    assert not newer('0.14.0', '0.15.0-rc.1')
    assert newer('v0.15.0', 'v0.15.0-rc.1')
    assert release({'tag_name': 'v0.15.0'}, '0.15.0-rc.1')['available'] is True
    with pytest.raises(ValueError):
        release({'tag_name': 'v0.15.0-rc.1', 'prerelease': False}, '0.14.0')


async def test_helper_failure_detail_reaches_machine_view_over_node_link(api, env, live, tmp_path, monkeypatch):  # noqa: F811
    import asyncio

    from helpers import wait_for
    from test_multihost import StandInNode, enroll

    from acc.node.updates import Updates
    from acc.update_helper import NodeTransaction

    monkeypatch.setattr('acc.node.updates.node_info', lambda: {})

    class Failed(NodeTransaction):
        def download(self):
            raise ValueError('node bundle version or build mismatch')

    root = tmp_path / 'helper'
    Failed(root, tmp_path / 'app', tmp_path / 'data', '0.14.0', {}).apply()
    key, node_id = await enroll(api)
    node = StandInNode(env['settings'].broker_socket, key, node_id,
                       info={'os': 'linux', 'capabilities': ['update']})
    task = asyncio.create_task(node.run(live))
    try:
        await wait_for(lambda: hasattr(node, 'ws'))
        await node.ws.send(json.dumps(Updates(root).message()))
        async def reported():
            machine = next(n for n in (await api.get('/api/nodes')).json() if n['id'] == node_id)
            result = machine.get('update') or {}
            return result if result.get('detail') == 'node bundle version or build mismatch' else None
        result = await wait_for(reported)
        assert result['ok'] is False and result['running'] is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
