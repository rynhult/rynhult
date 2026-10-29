"""acc-node's side of agent software (acc.node.packages): strict frames, the helper's request file, rate limits, and
the Mac's allow-list of user-level installs."""
from __future__ import annotations

import asyncio
import json

import pytest
from helpers import wait_for

from acc.node import packages
from acc.node.client import Node
from acc.node.config import NodeConfig
from acc.node.packages import HelperPackages, MacPackages, RateLimit, Refused, load_catalog, parse
from acc.server.api.packages import PACKAGES

CATALOG = load_catalog()


def frame(**changes) -> dict:
    return {"t": "packages", "id": "pkg-0123456789ab", "action": "install", "package": "codex", **changes}


def test_the_catalog_is_the_helpers_and_the_primarys():
    assert tuple(CATALOG) == PACKAGES
    mac = {name for name, spec in CATALOG.items() if spec.get("mac_ok")}
    assert mac == {"codex", "copilot"} and all(CATALOG[n]["method"] == "npm" for n in mac)


@pytest.mark.parametrize("message", [
    None, [], "install codex", {**frame(), "command": "curl x | sh"}, {**frame(), "version": "1.0"},
    {k: v for k, v in frame().items() if k != "id"}, frame(t="packages-state"), frame(id="../../etc/passwd"),
    frame(id="pkg-0123456789AB"), frame(id=7), frame(action="auto"), frame(action="exec"), frame(action=["install"]),
    frame(package="codex; rm -rf /"), frame(package="@openai/codex"), frame(package="https://example.com/x.sh"),
    frame(package={"name": "codex"}),
])
def test_only_catalog_names_and_fixed_actions(message):
    with pytest.raises(Refused):
        parse(message, CATALOG)


def test_parse_accepts_exactly_the_shape():
    assert parse(frame(action="check", package="ollama"), CATALOG) == ("pkg-0123456789ab", "check", "ollama")


@pytest.fixture
def helper(tmp_path):
    (tmp_path / "requests").mkdir()
    return HelperPackages(CATALOG, tmp_path)


def test_the_node_writes_exactly_the_helpers_request_file(helper, tmp_path):
    helper.handle(frame())
    assert [p.name for p in (tmp_path / "requests").iterdir()] == ["pkg-0123456789ab.json"]
    assert json.loads((tmp_path / "requests/pkg-0123456789ab.json").read_text()) == \
        {"id": "pkg-0123456789ab", "package": "codex", "action": "install"}
    state = helper.message()
    assert state["t"] == "packages-state" and state["kind"] == "helper" and state["helper"]
    assert state["pending"] == [{"id": "pkg-0123456789ab", "package": "codex", "action": "install"}]
    assert set(state["packages"]) == set(CATALOG) and "refused" not in state


def test_refusals_are_reported_and_write_nothing(helper, tmp_path):
    helper.handle(frame())
    helper.handle(frame(id="pkg-0123456789ac", action="update"))  # codex is still waiting
    assert helper.refused["id"] == "pkg-0123456789ac" and "already has a request waiting" in helper.refused["detail"]
    helper.handle(frame(id="pkg-0123456789ad", package="nmap"))
    assert helper.refused == {"id": "pkg-0123456789ad", "detail": "This machine's Rynhult doesn't know that tool. Update Rynhult on that machine.", "at": helper.refused["at"]}
    helper.handle({"t": "packages", "id": "../x", "action": "install", "package": "codex"})
    assert helper.refused["id"] == ""  # a bad id isn't echoed back
    assert [p.name for p in (tmp_path / "requests").iterdir()] == ["pkg-0123456789ab.json"]
    assert helper.message()["refused"]["detail"] == "This machine's Rynhult didn't understand the request. Update Rynhult on that machine."


def test_without_the_helper_nothing_is_written(tmp_path):
    missing = HelperPackages(CATALOG, tmp_path / "nowhere")
    missing.handle(frame())
    assert "isn't installed" in missing.refused["detail"] and not missing.message()["helper"]
    assert not (tmp_path / "nowhere").exists()


def test_pending_requests_are_capped(helper, tmp_path):
    for n, name in enumerate(CATALOG):
        helper.handle(frame(id=f"pkg-00000000000{n}", package=name))
    for n in range(10, 13):  # stand-ins for more waiting requests
        (tmp_path / f"requests/pkg-0000000000{n}.json").write_text(json.dumps({"package": "codex", "action": "check"}))
    helper.rate = RateLimit(limit=100)
    helper.handle(frame(id="pkg-0000000000ff", package="claude", action="update"))
    assert "Too many requests are waiting" in helper.refused["detail"]


def test_requests_are_rate_limited(helper):
    now = [0.0]
    helper.rate = RateLimit(limit=3, window=60, clock=lambda: now[0])
    for n in range(4):
        helper.handle(frame(id=f"pkg-00000000000{n}", action="check", package=list(CATALOG)[n]))
    assert helper.refused["detail"] == "Too many requests. Wait a minute and try again." and len(helper.pending()) == 3
    now[0] = 61
    helper.handle(frame(id="pkg-000000000009", action="check", package="ollama"))
    assert len(helper.pending()) == 4


def test_the_helper_state_is_read_back_bounded(helper, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"packages": {"codex": {"installed": "0.170.0"}}, "daily_at": 5}))
    state = helper.message()
    assert state["packages"]["codex"] == {"installed": "0.170.0"} and state["daily_at"] == 5
    (tmp_path / "state.json").write_text("{" * 300000)
    assert helper.message()["packages"]["codex"]["label"] == "Codex"  # unreadable: the catalog's own entries


class FakeNpm:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.installed: dict[str, str] = {}

    async def __call__(self, argv, timeout=0):
        self.calls.append(argv)
        if argv[1:3] == ["install", "-g"]:
            package, version = argv[-1].rsplit("@", 1)
            self.installed[package] = version
        elif argv[1:3] == ["uninstall", "-g"]:
            self.installed.pop(argv[-1], None)
        elif argv[1:2] == ["ls"]:
            return 0, json.dumps({"dependencies": {k: {"version": v} for k, v in self.installed.items()}})
        elif argv[1:2] == ["view"]:
            return 0, "0.171.0\n"
        return 0, ""


@pytest.fixture
def mac(tmp_path):
    npm = FakeNpm()
    return MacPackages(CATALOG, tmp_path, npm=tmp_path / "npm", run=npm), npm


async def test_a_mac_installs_only_mac_ok_entries_as_the_user(mac, tmp_path):
    packages_, npm = mac
    for name in ("claude", "cursor", "antigravity", "ollama"):
        packages_.handle(frame(id=f"pkg-{len(name):012x}", package=name))
        assert "can't be installed on a Mac from Rynhult." in packages_.refused["detail"]
    assert npm.calls == []
    packages_.handle(frame())
    await packages_.worker
    assert npm.calls[0] == [str(tmp_path / "npm"), "install", "-g", "--ignore-scripts", "--no-audit", "--no-fund",
                            "@openai/codex@0.163.0"]
    state = packages_.message()
    assert state["kind"] == "mac" and state["pending"] == []
    codex = state["packages"]["codex"]
    assert codex["installed"] == "0.163.0" and codex["latest"] == "0.163.0" and codex["upstream_latest"] == "0.171.0" and codex["last"]["ok"] and codex["mac_ok"]
    assert not state["packages"]["claude"]["mac_ok"] and "install it there by hand" in state["packages"]["claude"]["note"]
    assert json.loads((tmp_path / "packages.json").read_text())["packages"]["codex"]["installed"] == "0.163.0"

    packages_.handle(frame(id="pkg-0123456789ac", action="remove"))
    await packages_.worker
    assert npm.calls[-2] == [str(tmp_path / "npm"), "uninstall", "-g", "@openai/codex"]
    assert packages_.message()["packages"]["codex"]["installed"] is None


async def test_a_mac_checks_a_cli_installed_by_hand(mac, tmp_path, monkeypatch):
    """After the by-hand steps, Check now reads the version; installing it from Rynhult stays refused."""
    packages_, npm = mac
    for action in ("install", "update", "remove"):
        packages_.handle(frame(id="pkg-00000000000c", action=action, package="cursor"))
        assert packages_.refused["detail"].startswith("Cursor CLI can't be installed on a Mac from Rynhult.")
    packages_.refused = None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "cursor-agent").write_text("#!/bin/sh\n")
    (bin_dir / "cursor-agent").chmod(0o755)
    monkeypatch.setattr(packages, "MAC_PATH", str(bin_dir))
    packages_.handle(frame(id="pkg-00000000000d", action="check", package="cursor"))
    assert packages_.refused is None
    await packages_.worker
    assert npm.calls[-1] == [str(bin_dir / "cursor-agent"), "--version"]  # its own --version, nothing installed
    cursor = packages_.message()["packages"]["cursor"]
    assert cursor["installed"] == "installed" and cursor["last"]["ok"] and cursor["last"]["action"] == "check"


async def test_a_mac_without_homebrew_npm_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(packages, "MAC_NPM", (tmp_path / "missing-npm",))
    bare = MacPackages(CATALOG, tmp_path)
    bare.handle(frame())
    assert bare.refused["detail"] == "npm isn't installed on this Mac. Install it with brew install node, then try again." and bare.worker is None
    assert not bare.message()["helper"]


class FakeWs:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, text):
        self.sent.append(json.loads(text))


async def test_acc_node_answers_packages_frames_and_ignores_the_rest(helper, tmp_path):
    config = NodeConfig(primary="https://acc.example.lan", node_id="ab", socket_dir=tmp_path)
    node = Node(config, key=None, packages=helper)
    ws = FakeWs()
    await node.handle_text(ws, json.dumps(frame()))
    assert ws.sent[-1]["t"] == "packages-state" and ws.sent[-1]["pending"][0]["package"] == "codex"
    await node.send_packages(ws)  # unchanged: nothing sent again
    assert len(ws.sent) == 1
    await node.handle_text(ws, json.dumps({**frame(id="pkg-0123456789ac", package="copilot"), "pad": "x" * 600}))
    await node.handle_text(ws, json.dumps({"t": "packages", "pad": "x" * 5000}))
    assert len(ws.sent) == 1 and len(helper.pending()) == 1  # oversized frames aren't even parsed
    off = Node(config, key=None, packages=None)
    await off.handle_text(ws, json.dumps(frame()))  # no catalog on this node: ignored
    assert len(ws.sent) == 1


async def test_acc_node_reports_on_connect(helper, tmp_path, monkeypatch):
    monkeypatch.setattr("acc.node.client.PACKAGES_EVERY", 0.01)
    node = Node(NodeConfig(primary="https://acc.example.lan", node_id="ab", socket_dir=tmp_path), key=None,
                packages=helper)
    ws = FakeWs()
    task = asyncio.create_task(node.report_packages(ws))
    async def sent(count):
        return len(ws.sent) >= count
    try:
        await wait_for(lambda: sent(1))
        assert len(ws.sent) == 1 and ws.sent[0]["kind"] == "helper"
        (tmp_path / "state.json").write_text(json.dumps({"packages": {"codex": {"installed": "1.0.0"}}}))
        await wait_for(lambda: sent(2))
        assert len(ws.sent) == 2 and ws.sent[1]["packages"]["codex"]["installed"] == "1.0.0"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
