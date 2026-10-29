from __future__ import annotations

import os

import pytest
from test_auth import live  # noqa: F401 (fixture)


@pytest.mark.parametrize("before, kept", [
    ("", ""),
    ("# local settings\n[boot]\nsystemd=true\n", "[boot]\nsystemd=true"),
    ("[interop]\nenabled=true\nappendWindowsPath=true\n", ""),
    ("; keep this\n[interop]\nenabled = true # why\n[network]\nhostname=build-box\n", "# why"),
    ('[automount]\nroot=/windows/\noptions="case=off,uid=1000,umask=022"\n', "root=/windows/"),
    ("[automount]\nenabled=false\n", "enabled=false"),
])
def test_isolation_merges_without_clobbering_and_is_idempotent(tmp_path, before, kept):
    from acc.wsl import configure

    path = tmp_path / "wsl.conf"
    path.write_text(before)
    backup = configure(path)
    assert backup.read_text() == before
    after = path.read_text()
    assert kept in after
    import configparser
    config = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    config.read_string(after)
    assert config["interop"].getboolean("enabled") is False
    assert config["interop"].getboolean("appendWindowsPath") is False
    options = config["automount"]["options"].strip('"').split(",")
    assert {"metadata", "uid=0", "gid=0", "umask=077", "fmask=077"} <= set(options)
    assert "uid=1000" not in options
    if "case=off" in before:
        assert "case=off" in options
    assert configure(path) is None
    assert path.read_text() == after
    assert list(tmp_path.glob("wsl.conf.rynhult-backup-*")) == [backup]


@pytest.mark.parametrize("before", [
    "enabled=true\n", "[interop\nenabled=true\n", "[interop]\nenabled=true\nenabled=false\n",
    "[interop]\nenabled=true\n[interop]\nappendWindowsPath=true\n",
    "[DEFAULT]\nenabled=true\n", "[interop]\nenabled=true\n  continuation\n",
    "[interop] unexpected text\nenabled=true\n",
])
def test_unparsable_wsl_config_is_refused_without_writes(tmp_path, before):
    from acc.wsl import configure

    path = tmp_path / "wsl.conf"
    path.write_text(before)
    with pytest.raises(ValueError, match="unparsable"):
        configure(path)
    assert path.read_text() == before
    assert list(tmp_path.iterdir()) == [path]


def test_disabling_automount_requires_its_own_choice_and_preserves_other_keys(tmp_path):
    from acc.wsl import configure

    path = tmp_path / "wsl.conf"
    path.write_text('[automount]\noptions="case=off"\nmountFsTab=true\n')
    configure(path, disable_automount=True)
    assert 'options="case=off"\nmountFsTab=true\n' in path.read_text()
    assert "enabled=false" in path.read_text()


def test_wsl_config_dry_run_and_link_refusal(tmp_path):
    from acc.wsl import configure

    path = tmp_path / "wsl.conf"
    configure(path, dry_run=True)
    assert not path.exists()
    target = tmp_path / "other"
    target.write_text("[boot]\nsystemd=true\n")
    path.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        configure(path)
    assert target.read_text() == "[boot]\nsystemd=true\n"


@pytest.mark.parametrize("unsafe", ["writable", "hardlink"])
def test_unsafe_wsl_config_is_refused_without_a_backup(tmp_path, unsafe):
    from acc.wsl import configure

    path = tmp_path / "wsl.conf"
    path.write_text("[boot]\nsystemd=true\n")
    if unsafe == "writable":
        path.chmod(0o666)
    else:
        os.link(path, tmp_path / "linked")
    with pytest.raises(ValueError, match="administrator-owned"):
        configure(path)
    assert path.read_text() == "[boot]\nsystemd=true\n"
    assert not list(tmp_path.glob("wsl.conf.rynhult-backup-*"))


def test_untrusted_account_evidence_cannot_claim_safe_isolation():
    from acc.wsl import clean_isolation

    assert clean_isolation({"interop_enabled": "false", "accounts": {"alex-agent": 0}},
                           [None, {}, "bad/name", "alex-agent"]) == {
        "interop_enabled": None, "windows_drive_readable": None, "accounts": {"alex-agent": None}}


def test_config_creation_and_crlf_preservation(tmp_path):
    from acc.wsl import configure

    path = tmp_path / "wsl.conf"
    assert configure(path) is None
    assert path.exists()
    path.write_bytes(b"# Windows line endings\r\n[boot]\r\nsystemd=true\r\n")
    configure(path)
    assert b"[boot]\r\nsystemd=true\r\n" in path.read_bytes()
    assert b"\n" not in path.read_bytes().replace(b"\r\n", b"")


@pytest.mark.parametrize("enabled", [True, False, None])
def test_effective_interop_comes_from_proc_not_pending_config(tmp_path, enabled):
    from acc.wsl import interop_enabled

    (tmp_path / "status").write_text("enabled\n")  # binfmt_misc is mounted
    if enabled is not None:
        (tmp_path / "WSLInterop").write_text("enabled\n" if enabled else "disabled\n")
    assert interop_enabled(tmp_path) is (False if enabled is None else enabled)


@pytest.mark.parametrize("name", ["WSLInterop-late", "WSLInterop"])
def test_interop_counts_every_wsl_handler(tmp_path, name):
    # WSL with systemd can register its handler as WSLInterop-late; a missing plain WSLInterop is not "off".
    from acc.wsl import interop_enabled

    (tmp_path / "status").write_text("enabled\n")
    (tmp_path / "WSLInterop").write_text("disabled\n")
    (tmp_path / name).write_text("enabled\n")
    assert interop_enabled(tmp_path) is True


def test_interop_is_unknown_without_binfmt_misc(tmp_path):
    from acc.wsl import interop_enabled

    assert interop_enabled(tmp_path) is None  # not mounted: no "status" file, so no evidence either way
    assert interop_enabled(tmp_path / "missing") is None


@pytest.mark.parametrize("filesystem", ["9p", "drvfs"])
@pytest.mark.parametrize("readable", [True, False])
def test_agent_drive_probe_checks_only_mounted_drive_metadata(tmp_path, monkeypatch, filesystem, readable):
    from acc.wsl import windows_drive_readable

    mounts = tmp_path / "mountinfo"
    mounts.write_text("1 0 0:1 / / rw - ext4 /dev/root rw\n"
                      f"2 1 0:2 / /mnt/c rw - {filesystem} C:\\ rw,aname=drvfs;path=C:\\\n")
    calls = []
    def access(path, mode, *, effective_ids):
        calls.append((path, mode, effective_ids))
        return readable
    monkeypatch.setattr(os, "access", access)
    assert windows_drive_readable(mounts) is readable
    assert calls == [("/mnt/c", os.R_OK | os.X_OK, True)]
    mounts.write_text("1 0 0:1 / / rw - ext4 /dev/root rw\n")
    assert windows_drive_readable(mounts) is False
    assert len(calls) == 1


ROOT_MOUNT = "1 0 0:1 / / rw - ext4 /dev/root rw\n"
# WSL's own 9p shares (GPU drivers, libraries) are not Windows drives and must not make every machine "unknown".
WSL_INTERNAL = "5 1 0:5 / /usr/lib/wsl/drivers ro - 9p drivers ro,aname=drivers;fmask=222;dmask=222\n"


@pytest.mark.parametrize("line", [
    "2 1 0:2 / /mnt/c rw - virtiofs drvfsC rw\n",                      # virtiofs mode, drvfs tag
    "2 1 0:2 / /mnt/d rw - virtiofs D: rw\n",                          # virtiofs, drive-letter tag
    "2 1 0:2 / /win rw - 9p C:\\ rw,aname=drvfs;path=C:\\\n",       # fstab-style 9p elsewhere
    "2 1 0:2 / /win rw - virtiofs C:\\134 rw\n",                    # as the kernel writes C:\ (SR7-8), custom root
    "2 1 0:2 / /drives/e rw - 9p E:\\134 rw\n",                     # escaped 9p source, no drvfs option
])
@pytest.mark.parametrize("readable", [True, False])
def test_drive_probe_recognises_other_wsl_drive_mounts(tmp_path, monkeypatch, line, readable):
    from acc.wsl import windows_drive_readable

    mounts = tmp_path / "mountinfo"
    mounts.write_text(ROOT_MOUNT + WSL_INTERNAL + line)
    monkeypatch.setattr(os, "access", lambda path, mode, *, effective_ids: readable)
    assert windows_drive_readable(mounts) is readable


@pytest.mark.parametrize("line", [
    "2 1 0:2 / /mnt/c rw - fuse.something C: rw\n",                   # unrecognised type, Windows drive source
    "2 1 0:2 / /mnt/e rw - virtiofs share7 rw\n",                     # unrecognised tag at a drive mount point
    "2 1 0:2 / /data rw - cifs //pc/c$ rw,drvfs\n",                   # drvfs named in its options
])
def test_unrecognised_windows_like_mounts_are_unknown_not_safe(tmp_path, monkeypatch, line):
    from acc.wsl import windows_drive_readable

    mounts = tmp_path / "mountinfo"
    mounts.write_text(ROOT_MOUNT + WSL_INTERNAL + line)
    monkeypatch.setattr(os, "access", lambda path, mode, *, effective_ids: True)
    assert windows_drive_readable(mounts) is None
    monkeypatch.setattr(os, "access", lambda path, mode, *, effective_ids: False)
    assert windows_drive_readable(mounts) is False  # this account can't open it either way


def test_wsl_internal_shares_are_not_windows_drives(tmp_path, monkeypatch):
    from acc.wsl import windows_drive_readable

    mounts = tmp_path / "mountinfo"
    mounts.write_text(ROOT_MOUNT + WSL_INTERNAL)
    monkeypatch.setattr(os, "access", lambda path, mode, *, effective_ids: True)
    assert windows_drive_readable(mounts) is False


def test_missing_mount_evidence_is_unknown_not_safe(tmp_path):
    from acc.wsl import windows_drive_readable

    assert windows_drive_readable(tmp_path / "missing") is None


@pytest.mark.parametrize("interop, readable, warning", [
    (True, False, True), (False, True, True), (False, False, False), (None, None, False),
])
async def test_machine_api_warns_from_reported_exposure_not_configuration(api, interop, readable, warning):
    from acc.server.models import Node

    with api.app.state.db() as db:
        db.add(Node(id="wsl-box", name="build-box", status="active", info={
            "os": "linux", "wsl": "2", "accounts": ["alex-agent"],
            "wsl_isolation": {"interop_enabled": interop, "accounts": {"alex-agent": readable}}}))
        db.commit()
    row = next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "build-box")
    assert row["wsl_isolation"]["warning"] is warning
    assert row["wsl_isolation"]["windows_drive_readable"] is readable
    assert row["wsl_isolation"]["dismissed"] is False


async def test_wsl_risk_dismissal_is_owner_only_per_machine_durable_and_audited(api, monkeypatch):
    from sqlalchemy import select

    from acc.server.api.context import Principal
    from acc.server.models import AuditLog, Node

    ctx = api.app.state.ctx
    with ctx.db() as db:
        for name in ("build-box", "dev-box"):
            db.add(Node(id=name, name=name, status="active", info={"os": "linux", "wsl": "2"}))
        db.commit()
    original = ctx.principal
    monkeypatch.setattr(ctx, "principal", lambda request: Principal("blair", "member", "agent"))
    assert (await api.put("/api/nodes/build-box/wsl-isolation", json={"dismissed": True})).status_code == 403
    monkeypatch.setattr(ctx, "principal", original)
    seen = {"interop_enabled": None, "accounts": {}}
    revision = next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "build-box")["wsl_isolation"]["revision"]
    result = await api.put("/api/nodes/build-box/wsl-isolation",
                           json={"dismissed": True, "accepted": seen, "expected_revision": revision})
    assert result.status_code == 200, result.text
    rows = {row["name"]: row for row in (await api.get("/api/nodes")).json()}
    assert rows["build-box"]["wsl_isolation"]["dismissed"] is True
    assert rows["dev-box"]["wsl_isolation"]["dismissed"] is False
    with ctx.db() as db:
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "node.wsl_isolation"))
        assert audit.target == "build-box" and audit.detail == {"dismissed": True, "accepted": seen}
        assert audit.actor == "@api"
    assert (await api.put("/api/nodes/build-box/wsl-isolation", json={"dismissed": False, "expected_revision": rows["build-box"]["wsl_isolation"]["revision"]})).status_code == 200
    row = next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "build-box")
    assert row["wsl_isolation"]["dismissed"] is False
    assert (await api.put("/api/nodes/missing/wsl-isolation", json={"dismissed": True, "accepted": seen, "expected_revision": revision})).status_code == 404


def exposure_node(ctx, interop, accounts):
    from helpers import write_db

    from acc.server.models import Node

    with write_db(ctx.db) as db:
        node = db.get(Node, "lab") or Node(id="lab", name="lab", status="active")
        node.info = {"os": "linux", "wsl": "2", "accounts": list(accounts),
                     "wsl_isolation": {"interop_enabled": interop, "accounts": accounts}}
        db.merge(node)
        db.commit()


async def lab(api):
    return next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "lab")["wsl_isolation"]


async def test_a_dismissal_must_name_the_exposure_it_accepts(api):
    ctx = api.app.state.ctx
    exposure_node(ctx, True, {"agent": False})
    revision = (await lab(api))["revision"]
    assert (await api.put("/api/nodes/lab/wsl-isolation", json={"dismissed": True, "expected_revision": revision})).status_code == 422
    # Another tab or a reconnect changed the exposure after this owner looked: refuse, don't accept the new one.
    stale = {"interop_enabled": True, "accounts": {"agent": False}}
    exposure_node(ctx, True, {"agent": True})
    refused = await api.put("/api/nodes/lab/wsl-isolation", json={"dismissed": True, "accepted": stale,
                                                                       "expected_revision": revision})
    assert refused.status_code == 409
    assert (await lab(api))["dismissed"] is False


@pytest.mark.parametrize("interop, accounts", [
    (True, {"agent": True}),                     # drives became readable
    (True, {"agent": False, "second": False}),   # a new agent account
    (None, {"agent": False}),                    # no longer reported: not the state that was accepted
])
async def test_a_changed_exposure_raises_the_warning_again(api, interop, accounts):
    ctx = api.app.state.ctx
    exposure_node(ctx, True, {"agent": False})
    accepted = {"interop_enabled": True, "accounts": {"agent": False}}
    choice = {"dismissed": True, "accepted": accepted, "expected_revision": (await lab(api))["revision"]}
    assert (await api.put("/api/nodes/lab/wsl-isolation", json=choice)).status_code == 200
    assert (await lab(api))["dismissed"] is True
    exposure_node(ctx, interop, accounts)
    state = await lab(api)
    assert state["dismissed"] is False and state["dismissal_outdated"] is True
    exposure_node(ctx, True, {"agent": False})  # back to exactly what was accepted
    assert (await lab(api))["dismissed"] is True


async def test_a_dismissal_saved_before_exposure_binding_no_longer_hides_the_warning(api):
    from acc.server.models import Setting

    ctx = api.app.state.ctx
    exposure_node(ctx, True, {"agent": False})
    with ctx.db() as db:
        db.add(Setting(key="node.wsl_isolation.lab", value={"dismissed": True}, updated_by="alex"))
        db.commit()
    assert (await lab(api))["dismissed"] is False


async def test_wsl_isolation_survives_authenticated_handshake_and_refresh(api, env, live):  # noqa: F811
    import asyncio
    import json

    from helpers import wait_for
    from test_multihost import StandInNode, enroll

    key, node_id = await enroll(api)
    node = StandInNode(env["settings"].broker_socket, key, node_id, info={
        "os": "linux", "wsl": "2", "wsl_distro": "Ubuntu-24.04",
        "wsl_isolation": {"interop_enabled": True, "accounts": {"agent": False}, "dismissed": True}})
    task = asyncio.create_task(node.run(live))
    async def row():
        return next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "pc")
    try:
        await wait_for(lambda: hasattr(node, "ws"))
        assert (await row())["info"]["wsl_distro"] == "Ubuntu-24.04"
        assert (await row())["wsl_isolation"]["warning"]
        assert not (await row())["wsl_isolation"]["dismissed"]
        await node.ws.send(json.dumps({"t": "update-state", "info": {
            "wsl_isolation": {"interop_enabled": False, "accounts": {"agent": False}}}}))
        async def restricted():
            return (await row())["wsl_isolation"]["interop_enabled"] is False
        await wait_for(restricted)
        assert not (await row())["wsl_isolation"]["warning"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_isolation_replaces_a_wider_directory_mask(tmp_path):
    # dmask= overrides umask= for directories: a kept dmask=022 would leave drive folders listable by agents.
    from acc.wsl import merged_config

    after = merged_config('[automount]\noptions = "uid=1000,dmask=022,case=off"\n', False)
    import configparser
    config = configparser.ConfigParser()
    config.read_string(after)
    options = config["automount"]["options"].strip('"').split(",")
    assert "dmask=022" not in options
    assert "dmask=077" in options and "case=off" in options


async def test_owner_saves_on_a_wsl_node_with_agent_accounts_do_not_lock_the_database(api):
    # The row answer used to re-open the write transaction for the dismissal setting, then read logins in a second
    # session: SQLite answered "database is locked" for any WSL machine with agent accounts.
    ctx = api.app.state.ctx
    exposure_node(ctx, True, {"agent": False})
    assert (await api.post("/api/nodes/lab/policy", json={"role": "always_on"})).status_code == 200
    put = await api.put("/api/nodes/lab/wsl-isolation", json={"dismissed": False, "expected_revision": (await lab(api))["revision"]})
    assert put.status_code == 200, put.text


@pytest.mark.parametrize("options", ["uid =1000,dmask =022,case=off", "UID\t=1000,UMASK =000,case=off"])
def test_isolation_removes_spaced_permission_overrides(tmp_path, options):
    """A later whitespace-padded uid/mask must not override the restrictive options."""
    from acc.wsl import configure

    path = tmp_path / "wsl.conf"
    path.write_text(f'[automount]\noptions="{options}"\n')
    configure(path)
    after = path.read_text()
    assert 'options="metadata,uid=0,gid=0,umask=077,fmask=077,dmask=077,case=off"' in after


@pytest.mark.parametrize("before", ["[ interop ]\nenabled=true\n", "[automount ]\noptions=uid=1000\n"])
def test_ambiguous_padded_sections_are_refused_without_writes(tmp_path, before):
    from acc.wsl import configure

    path = tmp_path / "wsl.conf"
    path.write_text(before)
    with pytest.raises(ValueError, match="unparsable"):
        configure(path)
    assert path.read_text() == before
    assert list(tmp_path.iterdir()) == [path]


# What a WSL node that has not reported isolation shows, and so what a dismissal of it accepts.
NO_REPORT = {"interop_enabled": None, "accounts": {}}


async def test_stale_wsl_dismissal_cannot_overwrite_another_owner(api):
    from sqlalchemy import select

    from acc.server.models import AuditLog, Node

    ctx = api.app.state.ctx
    with ctx.db() as db:
        db.add(Node(id="build-box", name="build-box", status="active", info={"os":"linux", "wsl":"2"}))
        db.commit()
    snapshot = (await api.get("/api/nodes")).json()[0]["wsl_isolation"]
    # A missing revision must not let an older client bypass the concurrency check.
    assert (await api.put("/api/nodes/build-box/wsl-isolation", json={"dismissed":True})).status_code == 422
    revision = snapshot["revision"]
    first = await api.put("/api/nodes/build-box/wsl-isolation", json={"dismissed":True,"expected_revision":revision,
                                                                       "accepted":NO_REPORT})
    assert first.status_code == 200, first.text
    assert first.json()["wsl_isolation"]["revision"] != revision
    stale = await api.put("/api/nodes/build-box/wsl-isolation", json={"dismissed":False,"expected_revision":revision})
    assert stale.status_code == 409
    assert (await api.get("/api/nodes")).json()[0]["wsl_isolation"]["dismissed"] is True
    with ctx.db() as db:
        assert len(list(db.scalars(select(AuditLog).where(AuditLog.action == "node.wsl_isolation")))) == 1


async def test_concurrent_wsl_choices_have_one_winner_and_no_aba(api):
    import asyncio

    from acc.server.models import Node

    with api.app.state.ctx.db() as db:
        db.add(Node(id="build-box", name="build-box", status="active", info={"os":"linux", "wsl":"2"}))
        db.commit()
    url = "/api/nodes/build-box/wsl-isolation"
    revision = (await api.get("/api/nodes")).json()[0]["wsl_isolation"]["revision"]
    results = await asyncio.gather(*(api.put(url, json={"dismissed": choice, "expected_revision":revision, "accepted":NO_REPORT})
                                     for choice in (True, False)))
    assert sorted(r.status_code for r in results) == [200, 409]
    current = next(r.json()["wsl_isolation"] for r in results if r.status_code == 200)
    # Even an idempotent choice consumes its revision: an ABA tab cannot regain authority.
    result = await api.put(url, json={"dismissed": current["dismissed"], "expected_revision":current["revision"],
                                     "accepted":NO_REPORT})
    assert result.status_code == 200
    assert result.json()["wsl_isolation"]["revision"] != current["revision"]
    assert (await api.put(url, json={"dismissed":True,"expected_revision":revision,
                                         "accepted":NO_REPORT})).status_code == 409


@pytest.mark.parametrize("contents", [b"\xef\xbb\xbf[interop]\nenabled=true\n", b"#" * 65537, b"\xff\xfe[interop]"])
def test_bom_invalid_utf8_and_oversize_config_are_refused_unchanged(tmp_path, contents):
    from acc.wsl import configure

    path = tmp_path / "wsl.conf"
    path.write_bytes(contents)
    with pytest.raises(ValueError):
        configure(path)
    assert path.read_bytes() == contents
    assert list(tmp_path.iterdir()) == [path]


def test_unreadable_config_is_refused_without_writes(tmp_path):
    from acc.wsl import configure

    if os.geteuid() == 0:
        pytest.skip("root bypasses file read permissions")
    path = tmp_path / "wsl.conf"
    before = b"[interop]\nenabled=true\n"
    path.write_bytes(before)
    path.chmod(0)
    try:
        with pytest.raises(PermissionError):
            configure(path)
        assert list(tmp_path.iterdir()) == [path]
    finally:
        path.chmod(0o600)
    assert path.read_bytes() == before


async def test_authenticated_node_cannot_poison_machine_json_with_surrogates(api, env, live):  # noqa: F811
    import asyncio
    import json

    from helpers import wait_for
    from test_multihost import StandInNode, enroll

    key, node_id = await enroll(api)
    node = StandInNode(env["settings"].broker_socket, key, node_id, info={
        "os": "linux", "wsl": "2", "hostname": "lab\ud800box", "windows": "Windows\udfff",
        "wsl_distro": "Ubuntu\ud800", "version": "0.14.0", "build": "a" * 64})
    task = asyncio.create_task(node.run(live))
    try:
        await wait_for(lambda: hasattr(node, "ws"))
        response = await api.get("/api/nodes")
        assert response.status_code == 200
        row = next(n for n in response.json() if n["name"] == "pc")
        assert row["info"]["hostname"] == "lab?box"
        assert row["info"]["windows"] == "Windows?"
        assert row["info"]["wsl_distro"] == "Ubuntu?"
        assert row["system"]["kind"] == "wsl"
        from acc.server.models import Node
        with api.app.state.ctx.db() as db:
            assert db.get(Node, node_id).info["hostname"] == "lab?box"
        await node.ws.send(json.dumps({"t":"update-state", "info":{"build":"b" * 64, "version":"next🙂\udfff"}}))
        async def updated():
            with api.app.state.ctx.db() as db:
                return db.get(Node, node_id).info.get("build") == "b" * 64
        await wait_for(updated)
        with api.app.state.ctx.db() as db:
            assert db.get(Node, node_id).info["version"] == "next🙂?"
        row = next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "pc")
        assert row["info"]["version"] == "next🙂?"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_previously_saved_invalid_node_text_does_not_break_machine_list(api):
    from acc.server.models import Node

    with api.app.state.ctx.db() as db:
        db.add(Node(id="legacy", name="legacy-box", status="active", info={
            "os":"linux", "wsl":"2", "windows":"Windows\ud800", "hostname":"lab\udfffbox",
            "accounts":["agent\ud800"], "update":{"ok":False, "detail":"failed\ud800"}}))
        db.commit()
    response = await api.get("/api/nodes")
    assert response.status_code == 200
    row = response.json()[0]
    assert row["info"]["hostname"] == "lab?box"
    assert row["system"]["windows"] == "Windows?"
    assert row["accounts"] == []  # "agent?" isn't an account name; it isn't shown (security review 7, SR7-5)
    assert row["update"]["detail"] == "failed?"
