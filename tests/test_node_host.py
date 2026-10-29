"""Another machine's host details over its link (docs/MULTIHOST.md, "Host details"): the shared collector, what the
primary keeps of what a node sends, the request rate, older nodes, and who may ask."""
from __future__ import annotations

import asyncio
import base64
import importlib.machinery
import importlib.util
import json
import plistlib
import time
from collections import namedtuple
from pathlib import Path

import pytest
import websockets
from helpers import wait_for
from test_auth import live  # noqa: F401 (fixture)
from test_member_views import member  # noqa: F401 (fixture)
from test_multihost import enroll

from acc import hostinfo
from acc.node import client as node_client
from acc.server import node_host
from acc.server.api.nodes import MAX_TEXT

ROOT = Path(__file__).resolve().parents[1]


def snapshot(**overrides) -> dict:
    base = {"collected_at": time.time(), "hostname": "lab-box", "kernel": "6.8.0-example", "system": "linux",
            "uptime_seconds": 86400.0, "reboot_required": False,
            "disks": [{"mount": "/", "device": "/dev/vda1", "total": 100e9, "used": 40e9, "free": 60e9, "percent": 40.0}],
            "gpu": None, "services": {"units": {"acc-node": "active"}, "failed": []},
            "tailscale": None, "smart": None, "containers": None, "accounts": None,
            "live": {"cpu_percent": 12.0, "cpu_count": 8, "load": [0.5, 0.4, 0.3],
                     "memory": {"total": 16e9, "used": 4e9, "percent": 25.0}, "swap_percent": 0.0, "measured_at": time.time()},
            "unavailable": ["smart", "containers", "accounts", "gpu", "tailscale"]}
    base.update(overrides)
    return base


# ------------------------------------------------------------------ the shared collector


MAC_PARTITIONS = """\
/, /dev/disk3s1s1, apfs, ro,local,rootfs,dovolfs,journaled,multilabel
/System/Volumes/VM, /dev/disk3s6, apfs, rw,noexec,local,dovolfs,dontbrowse,journaled,multilabel,noatime
/System/Volumes/Preboot, /dev/disk3s2, apfs, rw,local,dovolfs,dontbrowse,journaled,multilabel
/System/Volumes/Update, /dev/disk3s4, apfs, rw,local,dovolfs,dontbrowse,journaled,multilabel
/System/Volumes/xarts, /dev/disk1s2, apfs, rw,noexec,local,dovolfs,dontbrowse,journaled,multilabel,noatime
/System/Volumes/iSCPreboot, /dev/disk1s1, apfs, rw,local,dovolfs,dontbrowse,journaled,multilabel
/System/Volumes/Hardware, /dev/disk1s3, apfs, rw,local,dovolfs,dontbrowse,journaled,multilabel
/System/Volumes/Data, /dev/disk3s5, apfs, rw,local,dovolfs,dontbrowse,journaled,multilabel
/private/var/run/com.apple.security.cryptexd/mnt/watchOS_24R362, /dev/disk5s1, apfs, ro,nosuid,local,dovolfs,dontbrowse,journaled,multilabel,noatime
/private/var/run/com.apple.security.cryptexd/mnt/com.apple.iPhoneOS.SimulatorRuntime-v24.1.94425.0.SveBXn, /dev/disk7s1, apfs, ro,local,dovolfs,dontbrowse,journaled,multilabel
"""
Partition = namedtuple("Partition", "mountpoint device fstype opts")
Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def mac_partitions(monkeypatch):
    partitions = [Partition(*line.split(", ", 3)) for line in MAC_PARTITIONS.splitlines()]
    monkeypatch.setattr(hostinfo.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hostinfo.shutil, "which", lambda name: None)
    monkeypatch.setattr(hostinfo.psutil, "disk_partitions", lambda all: partitions)
    monkeypatch.setattr(hostinfo.os.path, "isdir", lambda path: True)

    def run(*argv, **kwargs):
        assert argv == ("hdiutil", "info", "-plist")
        return 0, plistlib.dumps({"images": []}).decode()

    monkeypatch.setattr(hostinfo, "run", run)
    usage = {"/": Usage(1000, 20, 980), "/System/Volumes/Data": Usage(1000, 250, 750)}
    monkeypatch.setattr(hostinfo.shutil, "disk_usage", lambda mount: usage.get(mount, Usage(1000, 980, 20)))
    return partitions


def test_mac_reports_data_volume_not_full_system_or_simulator_disks(mac_partitions, monkeypatch):
    # The other three runtimes were described, not supplied as exact psutil rows.
    mac_partitions.extend(
        Partition(f"/private/var/run/com.apple.security.cryptexd/mnt/com.apple.{name}.SimulatorRuntime-v24",
                  f"/dev/disk{number}s1", "apfs", "ro,local,dovolfs,dontbrowse,journaled,multilabel")
        for number, name in enumerate(("AppleTVOS", "XROS", "iPhoneOS"), 8)
    )
    expected = [{"mount": "/System/Volumes/Data", "device": "/dev/disk3s5",
                 "total": 1000, "used": 250, "free": 750, "percent": 25.0}]
    assert hostinfo.disks() == expected
    monkeypatch.setattr(hostinfo, "live", lambda **kwargs: {
        "cpu_percent": 1, "memory": {"percent": 30}, "load": [0, 0, 0], "cpu_count": 8})
    light = hostinfo.light()
    assert light["disk"] == {"mount": "/System/Volumes/Data", "percent": 25.0, "free": 750}
    assert node_host.light_from({"disks": hostinfo.disks()})["disk"] == light["disk"]
    assert node_host.alerts(light) == []
    assert node_host.alerts(light, {"disks": hostinfo.disks()}) == []


def test_mac_keeps_writable_external_volumes_and_deduplicates_apfs_containers(mac_partitions):
    mac_partitions[:0] = [
        Partition("/Volumes/Data alias", "/dev/disk3s5", "apfs", "rw,local"),
        Partition("/Volumes/Shared space", "/dev/disk3s7", "apfs", "rw,local"),
    ]
    mac_partitions.extend([
        Partition("/Volumes/External", "/dev/disk10s1", "apfs", "rw,local"),
        Partition("/Volumes/External alias", "/dev/disk10s1", "apfs", "rw,local"),
        Partition("/Volumes/External sibling", "/dev/disk10s2", "apfs", "rw,local"),
        Partition("/Volumes/USB", "/dev/disk11s1", "exfat", "rw,local"),
        Partition("/Volumes/USB second partition", "/dev/disk11s2", "exfat", "rw,local"),
        Partition("/Volumes/SomeDMG", "/dev/disk12s1", "hfs", "ro,local"),
        Partition("/dev", "devfs", "devfs", "rw,local"),
        Partition("/Volumes/map", "map auto_home", "autofs", "rw,automounted"),
        Partition("/private/var/run/com.apple.security.cryptexd/mnt/runtime", "/dev/disk13s1", "apfs", "rw,local"),
    ])
    assert hostinfo.mounts() == [
        ("/System/Volumes/Data", "/dev/disk3s5"),
        ("/Volumes/External", "/dev/disk10s1"),
        ("/Volumes/USB", "/dev/disk11s1"),
        ("/Volumes/USB second partition", "/dev/disk11s2"),
    ]
    assert node_host.alerts(None, {"disks": hostinfo.disks()}) == [
        {"severity": "critical", "title": "Disk /Volumes/External is 98% full"},
        {"severity": "critical", "title": "Disk /Volumes/USB is 98% full"},
        {"severity": "critical", "title": "Disk /Volumes/USB second partition is 98% full"},
    ]


def test_mac_skips_writable_disk_images(mac_partitions, monkeypatch):
    mac_partitions.append(Partition("/Volumes/WritableDMG", "/dev/disk14s1", "apfs", "rw,local"))
    monkeypatch.setattr(hostinfo, "run", lambda *args, **kwargs: (0, plistlib.dumps({
        "images": [{"system-entities": [{"dev-entry": "/dev/disk14"}, {"mount-point": "/Volumes/WritableDMG"}]}]
    }).decode()))
    assert hostinfo.mounts() == [("/System/Volumes/Data", "/dev/disk3s5")]


def test_mac_keeps_writable_user_network_volume(mac_partitions):
    mac_partitions.append(Partition("/Volumes/Team", "//files.example.lan/team", "smbfs", "rw,nosuid,nodev"))
    mac_partitions.append(Partition("/Volumes/Auto", "map auto_home", "autofs", "rw,automounted"))
    assert hostinfo.mounts() == [
        ("/System/Volumes/Data", "/dev/disk3s5"),
        ("/Volumes/Team", "//files.example.lan/team"),
    ]


def test_mac_does_not_fall_back_to_read_only_root(mac_partitions):
    mac_partitions[:] = [mac_partitions[0]]
    assert hostinfo.disks() == []


@pytest.mark.parametrize("result", [(127, "hdiutil unavailable"), (0, "not a plist")])
def test_mac_logs_image_detection_failure_without_losing_data_disk(mac_partitions, monkeypatch, caplog, result):
    monkeypatch.setattr(hostinfo, "run", lambda *args, **kwargs: result)
    assert hostinfo.mounts() == [("/System/Volumes/Data", "/dev/disk3s5")]
    assert "disk images" in caplog.text


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_non_mac_psutil_filtering_is_unchanged(monkeypatch, system):
    monkeypatch.setattr(hostinfo.platform, "system", lambda: system)
    monkeypatch.setattr(hostinfo.shutil, "which", lambda name: None)
    monkeypatch.setattr(hostinfo.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(hostinfo.psutil, "disk_partitions", lambda all: [
        Partition("/", "/dev/vda1", "ext4", "ro"),
        Partition("/alias", "/dev/vda1", "ext4", "rw"),
        Partition("/srv", "/dev/vdb1", "ext4", "rw"),
        Partition("/snap/core", "/dev/loop0", "squashfs", "ro"),
        Partition("/mnt/wsl", "/dev/sdc", "ext4", "rw"),
    ])
    assert hostinfo.mounts() == [("/", "/dev/vda1"), ("/srv", "/dev/vdb1")]


@pytest.fixture
def synthetic_collector(tmp_path, monkeypatch):
    """Exercise the actual collector/parsers against test mounts and fake host commands."""
    from types import SimpleNamespace
    monkeypatch.setattr(hostinfo.psutil, "disk_partitions", lambda all: [
        SimpleNamespace(mountpoint=str(tmp_path), device="/dev/example")])
    monkeypatch.setattr(hostinfo.shutil, "which", lambda name: "/usr/bin/" + name)
    original_is_dir = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda path: True if str(path) == "/run/systemd/system"
                        else original_is_dir(path))

    def run(*argv, **kwargs):
        if argv[0] == "findmnt":
            return 0, f"{tmp_path} /dev/example"
        if argv[0] == "systemctl":
            return 0, "active" if argv[1] == "is-active" else ""
        if argv[0] == "tailscale":
            return 0, json.dumps({"BackendState": "Running", "Self": {"Online": True, "TailscaleIPs": ["100.64.0.2"]}})
        if argv[0] == "nvidia-smi":
            return 1, "not installed in this fixture"
        pytest.fail(f"unexpected host command {argv[0]}")
    monkeypatch.setattr(hostinfo, "run", run)
    monkeypatch.setattr(hostinfo, "live", lambda **kwargs: {"cpu_percent": 1, "cpu_count": 4, "load": [0, 0, 0],
                        "memory": {"total": 16 * 1024**3, "percent": 25}})


def test_collector_reports_this_machine_and_names_what_it_cannot_read(synthetic_collector):
    snap = hostinfo.snapshot()
    assert snap["disks"] and all(0 <= d["percent"] <= 100 for d in snap["disks"])
    assert snap["live"]["memory"]["total"] > 0 and snap["system"] in ("linux", "wsl", "mac", "windows")
    for key in ("smart", "containers", "accounts"):  # root-only on the main: unavailable, never empty or zero
        assert snap[key] is None and key in snap["unavailable"]
    for key in ("gpu", "services", "tailscale"):
        assert (snap[key] is None) == (key in snap["unavailable"])
    assert len(json.dumps(snap)) < node_client.MAX_HOST_TEXT
    light = hostinfo.light()
    assert set(light) == {"collected_at", "cpu_percent", "memory_percent", "load", "cpu_count", "uptime_seconds", "disk"}


def test_collector_caps_mounts_and_skips_wsl_and_snap_mounts(monkeypatch, tmp_path):
    rows = "\n".join(f"{tmp_path} /dev/vd{i}" for i in range(100))
    rows += f"\n/snap/core /dev/loop0\n/mnt/wsl /dev/sdb\n{tmp_path} /dev/vda1[/bind]"
    monkeypatch.setattr(hostinfo.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hostinfo.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(hostinfo, "run", lambda *argv, **kw: (0, rows))
    found = hostinfo.mounts()
    assert len(found) == hostinfo.MAX_DISKS and not any(m.startswith(("/snap", "/mnt/wsl")) for m, _ in found)


def test_collector_without_systemd_or_nvidia_says_unavailable(synthetic_collector, monkeypatch):
    """A Mac or a WSL machine without systemd: what the collector can't ask is None, not 0 or an empty list."""
    monkeypatch.setattr(hostinfo.shutil, "which", lambda name: None)
    snap = hostinfo.snapshot()
    assert snap["services"] is None and snap["gpu"] is None and snap["tailscale"] is None
    assert {"services", "gpu", "tailscale"} <= set(snap["unavailable"]) and snap["disks"]


def test_collector_keeps_acc_hoststates_field_names():
    """One Host page shows both: the node's readers keep the root collector's shapes."""
    loader = importlib.machinery.SourceFileLoader("acc_hoststate", str(ROOT / "deploy/hoststate/acc-hoststate"))
    spec = importlib.util.spec_from_loader("acc_hoststate", loader)
    script = importlib.util.module_from_spec(spec)
    loader.exec_module(script)
    source = Path(loader.path).read_text()
    for key in ("mount", "device", "total", "used", "free", "percent", "utilization", "memory_used_mb",
                "memory_total_mb", "temperature", "power_w", "units", "failed", "ips", "online"):
        assert f'"{key}"' in source and f'"{key}"' in Path(hostinfo.__file__).read_text(), key
    assert script.DISK_FILESYSTEMS == hostinfo.DISK_FILESYSTEMS


# ------------------------------------------------------------------ what the primary keeps


def test_primary_keeps_only_known_bounded_fields():
    hostile = snapshot(
        hostname="x" * 5000, system="plan9", extra={"secret": 1},
        disks=[{"mount": f"/m{i}", "device": "d", "total": 10, "used": 1, "free": 9, "percent": 10.0, "evil": "y"}
               for i in range(500)] + [{"mount": "/bad", "percent": "full"}, "nonsense"],
        gpu=[{"name": "card", "temperature": float("nan"), "utilization": True}] * 50,
        services={"units": {f"u{i}": "active" for i in range(500)}, "failed": ["a" * 1000] * 500},
        smart=[{"device": "sda", "passed": True}], containers=[{"name": "c"}], accounts=[{"account": "root"}],
        live={"cpu_percent": 900, "memory": {"percent": -5}, "load": "high"})
    clean = node_host.clean_snapshot(hostile)
    assert len(clean["hostname"]) == 120 and clean["system"] is None and "extra" not in clean
    assert len(clean["disks"]) == node_host.MAX_DISKS and all(set(d) == {"mount", "device", "total", "used", "free", "percent"} for d in clean["disks"])
    assert len(clean["gpu"]) == node_host.MAX_GPUS and clean["gpu"][0]["temperature"] is None and clean["gpu"][0]["utilization"] is None
    assert len(clean["services"]["units"]) == node_host.MAX_UNITS and len(clean["services"]["failed"][0]) == 120
    assert clean["smart"] is None and clean["containers"] is None and clean["accounts"] is None  # never from a node
    assert clean["live"]["cpu_percent"] is None and clean["live"]["memory"]["percent"] is None and clean["live"]["load"] is None
    assert {"smart", "containers", "accounts"} <= set(clean["unavailable"])
    assert node_host.clean_snapshot({"hostname": "no timestamp"}) is None and node_host.clean_snapshot([1]) is None
    assert node_host.clean_light({"collected_at": 1, "cpu_percent": 50, "disk": {"mount": "/", "percent": 101}})["disk"] is None


def test_node_alerts_follow_the_mains_pressure_rules():
    light = {"memory_percent": 95.0, "disk": {"mount": "/", "percent": 97.0, "free": 1}}
    found = node_host.alerts(light)
    assert {"severity": "critical", "title": "Disk / is 97% full"} in found and any("Memory" in a["title"] for a in found)
    assert node_host.alerts({"memory_percent": 30.0, "disk": {"mount": "/", "percent": 20.0}}) == []
    assert node_host.alerts(None) == []


# ------------------------------------------------------------------ the node's side


class Socket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, text):
        self.sent.append(json.loads(text))


async def test_node_answers_host_requests_at_most_every_ten_seconds(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from acc.node.config import NodeConfig
    config = NodeConfig(primary="https://acc.example.com", node_id="ab12", name="pc", trust="system", socket_dir=tmp_path)
    node = node_client.Node(config, Ed25519PrivateKey.generate(), packages=None)
    calls = []
    node.host_collect = (lambda: calls.append(1) or snapshot(), hostinfo.light)
    ws = Socket()
    await node.handle_text(ws, json.dumps({"t": "host", "id": "host-1"}))
    await wait_for(lambda: ws.sent)
    await node.handle_text(ws, json.dumps({"t": "host", "id": "host-2"}))
    await wait_for(lambda: len(ws.sent) == 2)
    assert [m["id"] for m in ws.sent] == ["host-1", "host-2"] and all(m["t"] == "host-state" for m in ws.sent)
    assert len(calls) == 1  # the second request got the last snapshot: collected at most every HOST_MIN_INTERVAL
    await node.handle_text(ws, json.dumps({"t": "host", "id": "x" * 100}))  # an odd id: ignored
    await asyncio.sleep(0.05)
    assert len(ws.sent) == 2


async def test_an_oversized_snapshot_drops_its_bulky_parts(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from acc.node.config import NodeConfig
    config = NodeConfig(primary="https://acc.example.com", node_id="ab12", name="pc", trust="system", socket_dir=tmp_path)
    node = node_client.Node(config, Ed25519PrivateKey.generate(), packages=None)
    node.host_collect = (lambda: snapshot(services={"units": {f"u{i}" * 40: "active" for i in range(2000)}, "failed": []}),
                         hostinfo.light)
    ws = Socket()
    await node.send_host(ws, "host-1")
    assert ws.sent[0]["snapshot"]["services"] is None and "services" in ws.sent[0]["snapshot"]["unavailable"]


# ------------------------------------------------------------------ over the link, end to end


async def connect(live, key, node_id, capabilities, answers: list, system="linux"):  # noqa: F811
    """A node that answers "host" requests with `snapshot()` and counts them; returns (task, connected event)."""
    ready = asyncio.Event()

    async def run():
        async with websockets.connect(f"{live}/api/nodes/connect") as ws:
            challenge = json.loads(await ws.recv())
            signature = key.sign(base64.b64decode(challenge["nonce"]) + challenge["primary"].encode())
            await ws.send(json.dumps({"t": "hello", "node_id": node_id, "signature": base64.b64encode(signature).decode(),
                                      "info": {"os": "linux", "version": "0.12.0", "capabilities": capabilities},
                                      "accounts": []}))
            assert json.loads(await ws.recv()) == {"t": "ready"}
            await ws.send(json.dumps({"t": "host-light", "light": {"collected_at": time.time(), "cpu_percent": 7.0,
                                                                   "memory_percent": 30.0, "disk": {"mount": "/", "percent": 40.0, "free": 6e10}}}))
            await ws.send("x" * (MAX_TEXT + 1))  # too large: dropped, the link stays
            ready.set()
            async for message in ws:
                data = json.loads(message) if isinstance(message, str) else None
                if isinstance(data, dict) and data.get("t") == "host":
                    answers.append(data["id"])
                    await ws.send(json.dumps({"t": "host-state", "id": data["id"], "snapshot": snapshot(system=system)}))
    return asyncio.create_task(run()), ready


async def test_owner_sees_a_nodes_details_and_requests_are_rate_limited(api, env, live):  # noqa: F811
    key, node_id = await enroll(api)
    answers: list[str] = []
    task, ready = await connect(live, key, node_id, ["preview-v1", "host-v1"], answers)
    try:
        await asyncio.wait_for(ready.wait(), 5)
        link = api.app.state.ctx.broker.hub.get("pc")
        await wait_for(lambda: link.host_light is not None)
        view = (await api.get("/api/host?machine=pc")).json()
        assert view["supported"] and view["connected"] and view["snapshot"]["hostname"] == "lab-box"
        assert view["live"]["cpu_percent"] == 12.0 and view["snapshot"]["smart"] is None
        assert "smart" in view["snapshot"]["unavailable"] and view["version"] == "0.12.0"
        # Preparing a backup uses the same reported filesystem capacity, before commands are copied.
        node = next(n for n in (await api.get('/api/nodes')).json() if n['id'] == node_id)
        assert node['backup_default_max_size_bytes'] == 27 * 2**30  # floor(60 GB / 2) in GiB
        target = (await api.post('/api/host/backups/targets', json={'node_id': node_id})).json()
        assert target['max_size_bytes'] == 27 * 2**30
        again = (await api.get("/api/host?machine=pc")).json()
        assert again["snapshot"]["collected_at"] == view["snapshot"]["collected_at"]
        assert len(answers) == 1  # within MIN_REFRESH: served from the link, the node isn't asked again
        link.host_snapshot_at -= node_host.MIN_REFRESH
        await api.get("/api/host?machine=pc")
        assert len(answers) == 2
        assert not link.closed  # the oversized frame was dropped, not fatal
        assert (await api.get("/api/host?machine=nope")).status_code == 404
        local = (await api.get("/api/host")).json()  # this machine: as before
        assert "machine" not in local and local["live"]["memory"]["total"] > 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_an_older_node_is_asked_nothing_and_shows_update(api, env, live):  # noqa: F811
    key, node_id = await enroll(api)
    answers: list[str] = []
    task, ready = await connect(live, key, node_id, ["preview-v1"], answers)
    try:
        await asyncio.wait_for(ready.wait(), 5)
        view = (await api.get("/api/host?machine=pc")).json()
        assert view["connected"] and not view["supported"] and view["snapshot"] is None and view["live"] is None
        await asyncio.sleep(0.2)
        assert answers == []  # no request frame went to a node that doesn't know it
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await wait_for(lambda: "pc" not in api.app.state.ctx.broker.hub.links)
    gone = (await api.get("/api/host?machine=pc")).json()
    assert not gone["connected"] and not gone["supported"]


async def test_a_silent_node_gets_a_timely_answer(api, env, live, monkeypatch):  # noqa: F811
    monkeypatch.setattr(node_host, "REQUEST_TIMEOUT", 0.3)
    key, node_id = await enroll(api)
    ws = await websockets.connect(f"{live}/api/nodes/connect")
    challenge = json.loads(await ws.recv())
    signature = key.sign(base64.b64decode(challenge["nonce"]) + challenge["primary"].encode())
    await ws.send(json.dumps({"t": "hello", "node_id": node_id, "signature": base64.b64encode(signature).decode(),
                              "info": {"capabilities": ["host-v1"]}, "accounts": []}))
    assert json.loads(await ws.recv()) == {"t": "ready"}
    try:
        view = (await asyncio.wait_for(api.get("/api/host?machine=pc"), 3)).json()
        assert view["supported"] and view["snapshot"] is None  # nothing yet: the page says so, nothing invented
    finally:
        await ws.close()


async def test_members_cannot_see_other_machines(api, env, member):  # noqa: F811
    await enroll(api)
    refused = await member.get("/api/host?machine=pc")
    assert refused.status_code == 403
    assert (await member.get("/api/host")).status_code == 200  # their own scoped view, as before


@pytest.mark.parametrize("message", [{"t": "host-state", "id": "nope", "snapshot": "x"}, {"t": "host-light", "light": 5}])
async def test_junk_host_frames_are_ignored(api, env, live, message):  # noqa: F811
    key, node_id = await enroll(api)
    ws = await websockets.connect(f"{live}/api/nodes/connect")
    challenge = json.loads(await ws.recv())
    signature = key.sign(base64.b64decode(challenge["nonce"]) + challenge["primary"].encode())
    await ws.send(json.dumps({"t": "hello", "node_id": node_id, "signature": base64.b64encode(signature).decode(),
                              "info": {"capabilities": ["host-v1"]}, "accounts": []}))
    assert json.loads(await ws.recv()) == {"t": "ready"}
    try:
        await ws.send(json.dumps(message))
        await ws.send(json.dumps({"t": "ping"}))
        assert json.loads(await asyncio.wait_for(ws.recv(), 3)) == {"t": "pong"}
        link = api.app.state.ctx.broker.hub.get("pc")
        assert link.host_snapshot is None and link.host_light is None
    finally:
        await ws.close()


@pytest.mark.parametrize('chosen', [None, 'always_on', 'workstation', 'at_enrollment', 'legacy_policy'])
async def test_wsl_snapshot_corrects_legacy_machine_without_overwriting_owner_role(api, env, live, chosen):  # noqa: F811
    from sqlalchemy import select

    from acc.server.models import Node
    if chosen == 'at_enrollment':
        from test_multihost import keypair
        added = (await api.post('/api/nodes', json={'name': 'pc', 'role': 'always_on'})).json()
        key, public = keypair()
        enrolled = await api.post('/api/nodes/enroll', json={'name': 'pc', 'code': added['code'],
            'public_key': public, 'info': {'os': 'linux'}})
        node_id = enrolled.json()['node_id']
    else:
        key, node_id = await enroll(api)
    # Simulate the role migration's default for a node that did not yet report WSL.
    with api.app.state.ctx.db() as s:
        node = s.scalar(select(Node).where(Node.id == node_id))
        node.role = 'always_on'
        s.commit()
    if chosen and chosen != 'at_enrollment':
        response = await api.post('/api/nodes/pc/policy', json={'role': 'always_on' if chosen == 'legacy_policy' else chosen})
        assert response.status_code == 200
        if chosen == 'legacy_policy':
            with api.app.state.ctx.db() as s:
                node = s.get(Node, node_id)
                node.info = {k: v for k, v in node.info.items() if k != 'role_chosen'}
                s.commit()
    task, ready = await connect(live, key, node_id, ['host-v1'], [], system='wsl')
    try:
        await asyncio.wait_for(ready.wait(), 5)
        view = (await api.get('/api/host?machine=pc')).json()
        assert view['snapshot']['system'] == 'wsl'
        row = next(n for n in (await api.get('/api/nodes')).json() if n['name'] == 'pc')
        assert row['system']['kind'] == 'wsl'
        assert row['role'] == ('always_on' if chosen in ('at_enrollment', 'legacy_policy') else chosen or 'workstation')
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
