"""Free text a node reports about itself (SR6-11): no control, bidi or invisible characters reach storage or display."""
import asyncio

import pytest
from helpers import wait_for, write_db
from test_auth import live  # noqa: F401 (fixture)

HOSTILE = "lab\x1b[31m-pc\r\nroot# ‮gnp.exe⁦​­﻿ box\x00"


@pytest.mark.parametrize("raw, clean", [
    (HOSTILE, "lab[31m-pc root# gnp.exe box"),
    ("  Ubuntu   24.04 LTS\t", "Ubuntu 24.04 LTS"),
    ("Café · 東京", "Café · 東京"),   # ordinary non-ASCII text stays
    (12, "12"),
])
def test_node_text_drops_controls_bidi_and_invisible_characters(raw, clean):
    from acc.server.nodes import node_text

    assert node_text(raw) == clean


def test_node_text_cuts_after_cleaning():
    from acc.server.nodes import node_text

    assert node_text("‮" * 500 + "a" * 300, 120) == "a" * 120


async def test_enroll_and_hello_store_only_clean_text(api, env, live):  # noqa: F811
    from test_multihost import StandInNode, keypair

    added = (await api.post("/api/nodes", json={"name": "pc"})).json()
    key, public = keypair()
    enrolled = await api.post("/api/nodes/enroll", json={"name": "pc", "code": added["code"], "public_key": public,
                                                         "info": {"os": "linux", "hostname": HOSTILE}})
    assert enrolled.status_code == 200, enrolled.text

    async def row():
        return next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "pc")
    assert (await row())["info"]["hostname"] == "lab[31m-pc root# gnp.exe box"

    node = StandInNode(env["settings"].broker_socket, key, enrolled.json()["node_id"], info={
        "os": "linux", "hostname": "build‮-box", "platform": "Linux\x07-6.8", "release": "6.8⁩.0\n"})
    task = asyncio.create_task(node.run(live))
    try:
        await wait_for(lambda: hasattr(node, "ws"))
        async def reported():
            info = (await row())["info"]
            return info if info.get("platform") else None
        info = await wait_for(reported)
        assert (info["hostname"], info["platform"], info["release"]) == ("build-box", "Linux-6.8", "6.8.0")
    finally:
        task.cancel()


async def test_text_stored_before_cleaning_is_cleaned_for_display(api):
    from acc.server.models import Node

    ctx = api.app.state.ctx
    with write_db(ctx.db) as db:
        db.add(Node(id="old", name="old-pc", status="active", info={"os": "linux", "hostname": "old‮-pc\x1b"}))
        db.commit()
    row = next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "old-pc")
    assert row["info"]["hostname"] == "old-pc"


def test_host_snapshot_text_is_clean():
    from acc.server.node_host import clean_snapshot

    snapshot = clean_snapshot({"collected_at": 1_700_000_000, "hostname": "lab‮-pc\n", "kernel": "6.8\x1b[0m"})
    assert (snapshot["hostname"], snapshot["kernel"]) == ("lab-pc", "6.8[0m")


# Security review 7: SR6-11's cleaning missed these (SR7-5 to SR7-7).
def test_reported_account_names_must_be_account_names():
    from acc.server.nodes import reported_accounts

    sent = ["agent", "ev‮il", "Bad Name", "a" * 33, 7, None, "agent-2", "x​"] + [f"u{i}" for i in range(30)]
    assert reported_accounts(sent)[:2] == ["agent", "agent-2"] and len(reported_accounts(sent)) == 20
    assert reported_accounts("agent") == [] and reported_accounts(None) == []


async def test_stored_accounts_and_system_label_are_cleaned_for_display(api):
    from acc.server.models import Node

    with write_db(api.app.state.ctx.db) as db:
        db.add(Node(id="dirty", name="dirty-pc", status="active", info={
            "os": "linux", "wsl": "2", "windows": "Windows‮ 11\x1b[31m", "version": "1.0​‮",
            "accounts": ["agent", "ad‮min"]}))
        db.commit()
    row = next(n for n in (await api.get("/api/nodes")).json() if n["name"] == "dirty-pc")
    assert row["accounts"] == ["agent"]
    assert row["system"]["windows"] == "Windows 11[31m"  # what node_text keeps: no escape or override
    assert "‮" not in json_text(row["system"]) and "\x1b" not in json_text(row["system"])


def json_text(value):
    import json
    return json.dumps(value, ensure_ascii=False)


async def test_host_machine_view_cleans_stored_version_and_os(api):
    from acc.server.models import Node

    with write_db(api.app.state.ctx.db) as db:
        db.add(Node(id="dirty2", name="dirty-two", status="active", info={"os": "linux\x07", "version": "2.0‮\x1b"}))
        db.commit()
    view = (await api.get("/api/host", params={"machine": "dirty-two"})).json()
    assert (view["version"], view["os"]) == ("2.0", "linux")


@pytest.mark.parametrize("field", ["version", "id", "detail"])
def test_update_results_are_cleaned_and_capped_before_audit(field):
    from acc.server.nodes import update_result

    result = {"ok": True, "version": "1.2", "id": "u1", field: "x‮" + "y" * 70000 + "\x1b", "at": 5}
    clean = update_result(result, None)
    assert "‮" not in clean[field] and "\x1b" not in clean[field] and len(clean[field]) <= (300 if field == "detail" else 120)
    assert clean["ok"] is True and "at" not in clean
    refused = update_result(result, {"detail": "no‮\n" + "z" * 1000})
    assert refused == {"ok": False, "detail": "no " + "z" * 197}
