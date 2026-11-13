"""deploy/packages/acc-packages (the root helper) and /api/packages: only allow-listed packages and fixed actions,
state for the dashboard, daily updates only where auto-update is on."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "deploy/packages/acc-packages"


@pytest.fixture
def helper(tmp_path, monkeypatch):
    monkeypatch.setenv("ACC_PACKAGES_ROOT", str(tmp_path / "packages"))
    (tmp_path / "packages/requests").mkdir(parents=True)
    loader = importlib.machinery.SourceFileLoader("acc_packages", str(HELPER))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("acc_packages", loader))
    loader.exec_module(module)
    world = {"installed": {"codex": "0.163.0"}, "latest": {"codex": "0.170.0", "copilot": "1.0.103", "claude": "2.1.300",
                                                          "ollama": "0.40.7"}, "commands": []}

    def install(spec):
        name = next(k for k, v in module.CATALOG.items() if v is spec)
        world["commands"].append(("install", name))
        world["installed"][name] = world["latest"][name]
        return f"{spec['label']} {world['installed'][name]} installed"

    def remove(spec):
        name = next(k for k, v in module.CATALOG.items() if v is spec)
        world["commands"].append(("remove", name))
        world["installed"].pop(name, None)
        return f"{spec['label']} removed"
    monkeypatch.setattr(module, "install", install)
    monkeypatch.setattr(module, "remove", remove)
    monkeypatch.setattr(module, "npm_installed", lambda: {})
    monkeypatch.setattr(module, "installed_version",
                        lambda spec, npm=None: world["installed"].get(next(k for k, v in module.CATALOG.items() if v is spec)))
    monkeypatch.setattr(module, "latest_version",
                        lambda spec: world["latest"].get(next(k for k, v in module.CATALOG.items() if v is spec)))
    def run(argv, **kwargs):
        import subprocess
        assert argv[:2] == ["npm", "view"]
        name = next(k for k, v in module.CATALOG.items() if v.get("package") == argv[2])
        return subprocess.CompletedProcess(argv, 0, world["latest"].get(name, ""), "")
    monkeypatch.setattr(module, "run", run)
    module.world = world
    return module


def submit(helper, **request) -> dict:
    path = helper.ROOT / "requests" / f"{request['id']}.json"
    path.write_text(json.dumps(request))
    return helper.process(helper.load_state(), path)


def test_installs_allow_listed_packages_and_records_state(helper):
    result = submit(helper, id="pkg-000000000001", package="copilot", action="install")
    assert result["ok"] and helper.world["commands"] == [("install", "copilot")]
    state = json.loads((helper.ROOT / "state.json").read_text())
    copilot = state["packages"]["copilot"]
    assert copilot["installed"] == "1.0.103" and copilot["auto_update"] is True and copilot["last"]["ok"]
    assert json.loads((helper.ROOT / "results/pkg-000000000001.json").read_text())["ok"]
    assert not (helper.ROOT / "requests/pkg-000000000001.json").exists()


@pytest.mark.parametrize("request_fields", [
    {"package": "curl; rm -rf /", "action": "install"}, {"package": "copilot", "action": "exec"},
    {"package": "../codex", "action": "install"},
])
def test_refuses_anything_off_the_list(helper, request_fields):
    result = submit(helper, id="pkg-000000000002", **request_fields)
    assert not result["ok"] and helper.world["commands"] == []


def test_daily_updates_only_what_is_behind_with_auto_update_on(helper):
    helper.world["installed"].update(copilot="1.0.90", claude="2.1.292")
    state = helper.load_state()
    helper.act(state, "claude", "auto", False)  # an owner turned it off
    helper.save_state(state)
    helper.daily(helper.load_state())
    assert ("install", "codex") in helper.world["commands"] and ("install", "copilot") in helper.world["commands"]
    assert ("install", "claude") not in helper.world["commands"]  # auto-update off
    assert ("install", "ollama") not in helper.world["commands"]  # not installed: never installed by the daily run
    state = json.loads((helper.ROOT / "state.json").read_text())
    assert state["packages"]["codex"]["installed"] == "0.170.0" and state["daily_at"]


def test_scripts_cannot_be_removed_from_the_dashboard(helper):
    helper.world["installed"]["ollama"] = "0.40.7"
    result = submit(helper, id="pkg-000000000003", package="ollama", action="remove")
    assert not result["ok"] and "by hand" in result["detail"]


async def test_api_queues_requests_for_owners(api, env):
    root = env["settings"].packages_dir
    (root / "requests").mkdir(parents=True)
    (root / "results").mkdir()
    (root / "state.json").write_text(json.dumps({"packages": {"codex": {"installed": "0.163.0", "latest": "0.170.0"}}}))
    view = (await api.get("/api/packages")).json()
    assert view["packages"]["codex"]["latest"] == "0.170.0" and set(view["packages"]) == {"restic", "claude", "codex", "copilot", "antigravity", "cursor", "opencode", "ollama"}
    assert view["packages"]["restic"]["group"] == "backup"  # the main ACC keeps it, under Backup tools
    assert {p["group"] for n, p in view["packages"].items() if n != "restic"} == {"agent"}
    assert all(p["installable"] and "guide" not in p for p in view["packages"].values())
    queued = await api.post("/api/packages/copilot/install")
    assert queued.status_code == 202
    request = json.loads(next((root / "requests").glob("*.json")).read_text())
    assert request["package"] == "copilot" and request["action"] == "install"
    assert (await api.get(f"/api/packages/requests/{queued.json()['request_id']}")).json() == {"done": False, "queued": True}
    assert (await api.post("/api/packages/bash/install")).status_code == 404
    assert (await api.post("/api/packages/codex/exec")).status_code == 404
    assert (await api.post("/api/packages/codex/auto", json={"enabled": False})).status_code == 202


# ---------------------------------------------------------------------------------------------- OpenCode (npm-binary)

def _tarball(files: dict[str, bytes]) -> bytes:
    import io
    import tarfile
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _integrity(data: bytes) -> str:
    import base64
    import hashlib
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


@pytest.fixture
def opencode(tmp_path, monkeypatch):
    """The real helper with OpenCode's entry pointed at tmp_path and the npm registry served from memory."""
    import io
    monkeypatch.setenv("ACC_PACKAGES_ROOT", str(tmp_path / "packages"))
    loader = importlib.machinery.SourceFileLoader("acc_packages_raw", str(HELPER))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("acc_packages_raw", loader))
    loader.exec_module(module)
    spec = module.CATALOG["opencode"]
    spec.update(dest=str(tmp_path / "bin/opencode"), root=str(tmp_path / "opt/opencode"))
    (tmp_path / "bin").mkdir()
    registry = "https://registry.npmjs.org"
    good = _tarball({"package/package.json": b"{}", "package/bin/opencode": b"#!/bin/sh\necho opencode v2.1.4\n"})
    served = {
        f"{registry}/@opencode%2fcli": {"versions": {v: {} for v in (
            "1.19.4", "2.0.32", "2.1.0", "2.1.2", "2.1.3", "2.1.4", "2.1.5-beta.1", "2.2.0", "2.10.1")}},
        f"{registry}/@opencode%2fcli-linux-x64/2.1.4": {"dist": {
            "tarball": f"{registry}/@opencode/cli-linux-x64/-/cli-linux-x64-2.1.4.tgz", "integrity": _integrity(good)}},
        f"{registry}/@opencode/cli-linux-x64/-/cli-linux-x64-2.1.4.tgz": good,
    }
    opened = []

    def open_url(url, timeout):
        opened.append(url)
        if url not in served:
            raise OSError(f"404 {url}")
        body = served[url]
        return io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
    monkeypatch.setattr(module, "open_url", open_url)
    monkeypatch.setattr(module, "npm_binary_platform", lambda: "linux-x64")
    module.served, module.opened, module.registry = served, opened, registry
    return module


def test_opencode_is_pinned_to_its_verified_release_line(opencode):
    spec = opencode.CATALOG["opencode"]
    assert spec["method"] == "npm-binary" and spec["line"] == "2.1" and spec["minimum"] == "2.1.3"
    # the newest plain 2.1.x: never a pre-release, 2.2 or 2.10 (a new line needs an ACC change), nor below the minimum
    assert opencode.npm_binary_version(spec) == "2.1.4"
    opencode.served[f"{opencode.registry}/@opencode%2fcli"] = {"versions": {"2.1.2": {}, "2.2.0": {}}}
    assert opencode.npm_binary_version(spec) is None
    assert opencode.latest_version(spec) is None


def test_opencode_install_checks_the_registry_checksum_and_links_atomically(opencode, tmp_path):
    spec = opencode.CATALOG["opencode"]
    (tmp_path / "opt/opencode/versions/2.1.3").mkdir(parents=True)  # an older version: removed after the switch
    assert opencode.install(spec) == "OpenCode 2.1.4 installed"
    binary = tmp_path / "opt/opencode/versions/2.1.4/opencode"
    assert binary.read_bytes().startswith(b"#!/bin/sh") and binary.stat().st_mode & 0o777 == 0o755
    assert Path(spec["dest"]).resolve() == binary and opencode.installed_version(spec) == "2.1.4"
    assert sorted(p.name for p in (tmp_path / "opt/opencode/versions").iterdir()) == ["2.1.4"]
    assert not (tmp_path / "opt/opencode/versions/2.1.4/package.json").exists()  # only the binary is unpacked
    assert opencode.remove(spec) == "OpenCode removed" and opencode.installed_version(spec) is None


@pytest.mark.parametrize("breakage, message", [
    ("checksum", "checksum doesn't match"),
    ("address", "unexpected download address"),
    ("layout", "unexpected package layout"),
])
def test_opencode_install_refuses_a_bad_download_and_installs_nothing(opencode, tmp_path, breakage, message):
    spec = opencode.CATALOG["opencode"]
    meta = opencode.served[f"{opencode.registry}/@opencode%2fcli-linux-x64/2.1.4"]["dist"]
    tarball_url = meta["tarball"]
    if breakage == "checksum":
        opencode.served[tarball_url] = opencode.served[tarball_url][:-10] + b"tampered!!"
    elif breakage == "address":
        meta["tarball"] = "https://elsewhere.example.com/cli.tgz"
    else:
        other = _tarball({"package/bin/something-else": b"x"})
        opencode.served[tarball_url], meta["integrity"] = other, _integrity(other)
    with pytest.raises(opencode.Refused, match=message):
        opencode.install(spec)
    assert not Path(spec["dest"]).exists() and not (tmp_path / "opt/opencode/versions/2.1.4").exists()


def test_restic_checksum_refusal_preserves_installed_binary(helper, tmp_path, monkeypatch):
    import bz2
    import io
    installed = tmp_path / 'restic'
    installed.write_bytes(b'previous binary')
    spec = {**helper.CATALOG['restic'], 'dest': str(installed), 'root': str(tmp_path / 'qualified-restic')}
    monkeypatch.setattr(helper, 'open_url', lambda *a, **k: io.BytesIO(bz2.compress(b'untrusted binary')))
    with pytest.raises(helper.Refused, match='checksum'):
        helper.install_restic(spec)
    assert installed.read_bytes() == b'previous binary'
    assert not (tmp_path / 'qualified-restic/versions/0.19.1/restic').exists()


@pytest.mark.parametrize("name, target", [("codex", "0.167.1"), ("copilot", "1.0.103")])
def test_npm_installs_acc_target_even_when_upstream_has_advanced(opencode, monkeypatch, name, target):
    import subprocess

    helper = opencode
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        if argv[1] == "view":
            out = "99.0.0\n"
        elif argv[1] == "ls":
            out = json.dumps({"dependencies": {helper.CATALOG[name]["package"]: {"version": target}}})
        else:
            out = ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(helper, "run", run)
    monkeypatch.setattr(helper.shutil, "which", lambda _: "/fake/npm")
    spec = helper.CATALOG[name]
    helper.install(spec)
    assert next(c for c in commands if c[1] == "install")[-1] == f"{spec['package']}@{target}"
    state = {"packages": {}}
    helper.refresh(state, [name], latest=True)
    entry = state["packages"][name]
    assert entry["latest"] == target and entry["upstream_latest"] == "99.0.0"
    assert target in entry["note"] and "99.0.0" in entry["note"]
    assert entry["channel"] == "pinned"


def test_rolling_updates_require_explicit_opt_in_including_legacy_defaults(helper):
    helper.world["installed"]["claude"] = "2.1.292"
    state = {"packages": {"claude": {"auto_update": True}}}
    helper.daily(state)
    assert ("install", "claude") not in helper.world["commands"]
    assert state["packages"]["claude"]["auto_update"] is False
    helper.act(state, "claude", "auto", True)
    helper.daily(state)
    assert ("install", "claude") in helper.world["commands"]


def test_daily_does_not_downgrade_a_newer_manually_installed_cli(helper):
    helper.world["installed"]["codex"] = "99.0.0"
    helper.daily({"packages": {}})
    assert ("install", "codex") not in helper.world["commands"]


@pytest.fixture
def apt_helper(tmp_path, monkeypatch):
    """The real helper's apt path for Claude Code, with /etc/apt/sources.list.d and the repository script in tmp_path."""
    import subprocess
    monkeypatch.setenv("ACC_PACKAGES_ROOT", str(tmp_path / "packages"))
    loader = importlib.machinery.SourceFileLoader("acc_packages_apt", str(HELPER))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("acc_packages_apt", loader))
    loader.exec_module(module)
    sources = tmp_path / "sources.list.d"
    sources.mkdir()
    monkeypatch.setattr(module, "APT_SOURCES", sources)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "sbin/acc-packages"))  # the script is its sibling
    (tmp_path / "sbin").mkdir()
    commands, script = [], {"ok": True}

    def run(argv, **kwargs):
        commands.append(argv[:2] if argv[0] == "sh" else argv)
        if argv[0] == "sh":  # stands in for acc-claude-apt-repo
            if not script["ok"]:
                return subprocess.CompletedProcess(argv, 1, "", "Claude Code signing key fingerprint mismatch (got BAD); nothing changed")
            (sources / "claude-code.list").write_text("deb ...\n")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(module, "run", run)
    module.commands, module.script, module.sbin = commands, script, tmp_path / "sbin"
    return module


def test_claude_install_adds_a_missing_apt_repository_first(apt_helper):
    (apt_helper.sbin / "acc-claude-apt-repo").write_text("#!/bin/sh\n")
    apt_helper.install(apt_helper.CATALOG["claude"])
    assert apt_helper.commands[0] == ["sh", str(apt_helper.sbin / "acc-claude-apt-repo")]
    assert ["apt-get", "install", "-y", "-qq", "claude-code"] in apt_helper.commands
    apt_helper.commands.clear()
    apt_helper.install(apt_helper.CATALOG["claude"])  # there now: not added again
    assert not any(c[0] == "sh" for c in apt_helper.commands)


def test_claude_install_refuses_when_the_key_fingerprint_does_not_match(apt_helper):
    (apt_helper.sbin / "acc-claude-apt-repo").write_text("#!/bin/sh\n")
    apt_helper.script["ok"] = False
    with pytest.raises(apt_helper.Refused, match="fingerprint mismatch"):
        apt_helper.install(apt_helper.CATALOG["claude"])
    assert not any(c[0] in ("apt-get", "apt-mark") for c in apt_helper.commands)  # nothing installed


def test_claude_install_without_the_repository_script_says_how_to_fix_it(apt_helper):
    with pytest.raises(apt_helper.Refused, match="no script to add it"):
        apt_helper.install(apt_helper.CATALOG["claude"])
    assert apt_helper.commands == []


# ---------------------------------------------------------------------------------------------- by-hand install steps


def test_the_primary_knows_which_entries_a_mac_installs():
    from acc.node.packages import load_catalog
    from acc.server.agent_setup import BACKUP_TOOLS, GUIDES
    from acc.server.api.packages import MAC_INSTALLS, PACKAGES
    catalog = load_catalog()
    assert set(MAC_INSTALLS) == {name for name, spec in catalog.items() if spec.get("mac_ok")}
    assert set(PACKAGES) == set(catalog)
    assert set(GUIDES) | set(BACKUP_TOOLS) == set(catalog)  # every agent CLI has steps


def test_every_step_is_an_official_command_or_page():
    """Commands and links come from the providers' documentation (cited in acc.server.agent_setup)."""
    from acc.server.agent_setup import GUIDES, SYSTEMS, guide
    official = ("https://claude.ai/", "https://chatgpt.com/codex/", "https://gh.io/", "https://antigravity.google/",
                "https://cursor.com/", "https://opencode.ai/", "https://ollama.com/")
    for name in GUIDES:
        for system in SYSTEMS:
            steps = guide(name, system, "sam-agent")
            assert steps["docs"].startswith("https://") and steps["commands"], (name, system)
            for command in steps["commands"]:
                text = command["command"]
                assert text.startswith(("brew install ", "curl ", "https://")), text
                if not text.startswith("brew "):
                    assert any(o in text for o in official), text
            if system != "macos":
                assert not any(c["command"].startswith("brew ") for c in steps["commands"])
                assert steps["shell_command"] == "sudo -iu sam-agent"
            else:
                assert steps["shell_command"] is None  # sessions run as the signed-in user


async def test_no_helper_here_means_steps_not_buttons(api, env):
    view = (await api.get("/api/packages")).json()  # no requests folder: deploy/install.sh never ran
    assert not view["helper"]
    claude = view["packages"]["claude"]
    assert not claude["installable"] and claude["guide"]["system"] in ("linux", "wsl")
    assert "restic" in view["packages"] and "guide" not in view["packages"]["restic"]  # no by-hand steps for it


def test_the_guide_page_shows_the_same_commands_and_pages():
    """Every vendor install command has a copyable fence; downloads and docs remain links."""
    import re
    from pathlib import Path

    from acc.server.agent_setup import GUIDES
    page = (Path(__file__).resolve().parents[1] / "docs/wiki/User-Guide/Connect-an-Agent.md").read_text()
    commands = {block.strip() for block in re.findall(r"```(?:sh|bash)?\n(.*?)\n```", page, re.S)}
    links = set(re.findall(r"\]\(([^)]+)\)", page))
    for name, entry in GUIDES.items():
        for system in ("macos", "linux"):
            for _how, command in entry[system]:
                assert command in (links if command.startswith("https://") else commands), (name, system, command)
            assert entry['docs'][system] in links, (name, system)


# Claude Code where apt isn't the package manager: Anthropic's official native release, pinned like the other providers.

def test_claude_uses_the_pinned_native_release_only_without_apt(helper):
    assert helper.claude_spec(apt=True)["method"] == "apt"
    native = helper.claude_spec(apt=False)
    assert native["method"] == "claude-native" and native["bin"] == "claude"
    assert re.fullmatch(r"\d+\.\d+\.\d+", native["version"])
    # SHA-256 from Anthropic's published manifest for that version (downloads.claude.ai/.../manifest.json).
    assert set(native["checksums"]) == {"linux-x64", "linux-arm64"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in native["checksums"].values())
    assert native["url"].startswith("https://downloads.claude.ai/claude-code-releases/{version}/{platform}/claude")


@pytest.fixture
def unpatched(tmp_path, monkeypatch):
    monkeypatch.setenv("ACC_PACKAGES_ROOT", str(tmp_path / "packages"))
    loader = importlib.machinery.SourceFileLoader("acc_packages_unpatched", str(HELPER))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("acc_packages_unpatched", loader))
    loader.exec_module(module)
    return module


@pytest.fixture
def native(unpatched, tmp_path, monkeypatch):
    import hashlib
    helper = unpatched
    payload = b"\x7fELF official claude"
    spec = {**helper.claude_spec(apt=False), "dest": str(tmp_path / "bin/claude"), "root": str(tmp_path / "acc-claude"),
            "checksums": {"linux-x64": hashlib.sha256(payload).hexdigest(), "linux-arm64": "0" * 64}}
    (tmp_path / "bin").mkdir()
    downloads = []

    def open_url(url, timeout):
        import io
        downloads.append(url)
        return io.BytesIO(native.body)
    monkeypatch.setattr(helper, "open_url", open_url)
    monkeypatch.setattr(helper.os, "uname", lambda: type("U", (), {"machine": "x86_64"})())
    native = type("N", (), {"spec": spec, "body": payload, "downloads": downloads})
    return native


def test_claude_native_install_checks_the_published_checksum_and_links_atomically(unpatched, native):
    helper = unpatched
    helper.install_claude_native(native.spec)
    version = native.spec["version"]
    assert native.downloads == [f"https://downloads.claude.ai/claude-code-releases/{version}/linux-x64/claude"]
    dest = Path(native.spec["dest"])
    assert dest.is_symlink() and dest.resolve() == Path(native.spec["root"]) / "versions" / version / "claude"
    assert dest.read_bytes() == native.body and os.stat(dest).st_mode & 0o777 == 0o755
    assert helper.installed_version(native.spec) == version and helper.latest_version(native.spec) == version


@pytest.mark.parametrize("breakage", ["tampered", "oversized", "architecture"])
def test_claude_native_install_refuses_and_keeps_what_was_there(unpatched, native, monkeypatch, breakage):
    helper = unpatched
    dest = Path(native.spec["dest"])
    dest.write_bytes(b"previous claude")
    if breakage == "tampered":
        native.body = b"\x7fELF something else"
    elif breakage == "oversized":
        monkeypatch.setattr(helper, "CLAUDE_MAX_BYTES", 8)
    else:
        monkeypatch.setattr(helper.os, "uname", lambda: type("U", (), {"machine": "riscv64"})())
    with pytest.raises(helper.Refused):
        helper.install_claude_native(native.spec)
    assert dest.read_bytes() == b"previous claude"
    assert not (Path(native.spec["root"]) / "versions" / native.spec["version"] / "claude").exists()


@pytest.mark.parametrize('name', ['codex', 'copilot'])
@pytest.mark.parametrize('installed', [None, '0.1.0', 'pin', '99.0.0', '0.167.1-dev', 'unknown'])
def test_explicit_install_never_downgrades_pinned_cli(unpatched, monkeypatch, name, installed):
    import subprocess

    spec = unpatched.CATALOG[name]
    installed = spec['pin'] if installed == 'pin' else installed
    commands = []
    monkeypatch.setattr(unpatched, 'installed_version', lambda spec: installed)
    monkeypatch.setattr(unpatched.shutil, 'which', lambda _: '/fake/npm')
    monkeypatch.setattr(unpatched, 'run', lambda argv, **kw: (
        commands.append(argv) or subprocess.CompletedProcess(argv, 0, '', '')))
    refused = installed is not None and (installed in ('99.0.0', '0.167.1-dev', 'unknown'))
    if refused:
        with pytest.raises(unpatched.Refused, match='Refusing to downgrade') as error:
            unpatched.install(spec)
        assert installed in str(error.value) and spec['pin'] in str(error.value)
        assert 'left unchanged' in str(error.value) and commands == []
    else:
        unpatched.install(spec)
        assert commands[0][-1] == f"{spec['package']}@{spec['pin']}"


def test_deploy_install_reports_downgrade_refusal(unpatched, monkeypatch, capsys):
    monkeypatch.setattr(unpatched.shutil, 'which', lambda _: '/fake/npm')
    monkeypatch.setattr(unpatched, 'installed_version', lambda spec: '99.0.0')
    monkeypatch.setattr(unpatched, 'run', lambda *a, **k: pytest.fail('must not invoke npm'))
    assert unpatched.main(['--install', 'codex']) == 0
    output = capsys.readouterr().out
    assert 'codex KEPT' in output and '99.0.0' in output and '0.167.1' in output
    state = unpatched.load_state()['packages']['codex']
    assert not state['last']['ok'] and 'Refusing to downgrade' in state['last']['detail']


def test_missing_npm_is_reported_before_version_probe(unpatched, monkeypatch):
    monkeypatch.setattr(unpatched.shutil, 'which', lambda _: None)
    monkeypatch.setattr(unpatched, 'run', lambda *a, **k: pytest.fail('npm must not run'))
    with pytest.raises(unpatched.Refused, match="npm isn't installed"):
        unpatched.install(unpatched.CATALOG['codex'])


@pytest.mark.parametrize('stdout,code', [
    ('not JSON', 0), ('', 0), ('null', 0), ('[]', 0), ('{"dependencies": []}', 0),
    ('{"dependencies": {"@openai/codex": {}}}', 0),
    ('{"dependencies": {"@openai/codex": {"version": null}}}', 0),
    ('{"dependencies": {"@openai/codex": "0.171.0"}}', 0),
    ('{"error": {"summary": "failed"}}', 0), ('{}', 1),
])
def test_unknown_npm_inventory_refuses_install(unpatched, monkeypatch, stdout, code):
    import subprocess

    monkeypatch.setattr(unpatched.shutil, 'which', lambda _: '/fake/npm')
    commands = []

    def run(argv, **kw):
        commands.append(argv)
        assert argv[:2] == ['npm', 'ls']
        return subprocess.CompletedProcess(argv, code, stdout, '')

    monkeypatch.setattr(unpatched, 'run', run)
    assert unpatched.main(['--install', 'codex']) == 1
    assert len(commands) == 1  # no mutation, including for otherwise valid JSON with a failed exit
    last = unpatched.load_state()['packages']['codex']['last']
    assert not last['ok'] and 'could not determine installed version' in last['detail']


@pytest.mark.parametrize('stdout', ['{}', '{"dependencies": {}}'])
def test_valid_empty_npm_inventory_allows_fresh_install(unpatched, monkeypatch, stdout):
    import subprocess

    monkeypatch.setattr(unpatched.shutil, 'which', lambda _: '/fake/npm')
    commands = []

    def run(argv, **kw):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout if argv[1] == 'ls' else '', '')

    monkeypatch.setattr(unpatched, 'run', run)
    unpatched.install(unpatched.CATALOG['codex'])
    assert any(c[1] == 'install' for c in commands)


@pytest.mark.parametrize('newer', [True, False], ids=['kept', 'real-install-failure'])
def test_install_sh_kept_cli_continues_to_brokers_folders_and_restart(tmp_path, newer):
    """Actual installer segment under its strict shell options, with host effects stubbed."""
    import shlex
    import subprocess
    import sys

    repo = HELPER.parents[2]
    npm = tmp_path / 'npm'
    npm.write_text(f'#!{sys.executable}\n'
                   'import json, sys\n'
                   'from pathlib import Path\n'
                   f'calls = Path({str(tmp_path / "npm-calls")!r})\n'
                   'with calls.open("a") as f: f.write(sys.argv[1] + "\\n")\n'
                   'if sys.argv[1] == "ls":\n'
                   f' print(json.dumps({{"dependencies": {{"@openai/codex": {{"version": {"99.0.0" if newer else "0.1.0"!r}}}}}}}))\n'
                   'elif sys.argv[1] == "install":\n'
                   ' print("fixture npm install failure", file=sys.stderr)\n'
                   ' sys.exit(42)\n')
    npm.chmod(0o755)
    safe = tmp_path / 'safe-host-command'
    safe.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DEPLOY_CALLS"\n')
    safe.chmod(0o755)
    installer = (repo / 'deploy/install.sh').read_text()
    strict = next(line for line in installer.splitlines() if line == 'set -euo pipefail')
    start = installer.index('clis=$(setting ACC_AGENT_CLIS')
    end = installer.index('\n', installer.index('systemctl restart accd.service', start))
    segment = installer[start:end]
    segment = segment.replace('/usr/local/sbin/acc-packages',
                              shlex.quote(sys.executable) + ' ' + shlex.quote(str(HELPER)))
    for executable in ['/usr/local/sbin/acc-agent-add', '/usr/local/sbin/acc-agent-share', '/usr/bin/python3']:
        segment = segment.replace(executable, shlex.quote(str(safe)))
    script = strict + '\n' + '''
setting() { echo codex; }
install() { :; }
dpkg() { return 0; }
apt-mark() { :; }
systemctl() { "$SAFE_HOST_COMMAND" "$@"; }
REPO=/fixture/repo
AGENT_USER=example-agent
OWNER=example
''' + segment
    env = {k: v for k, v in os.environ.items() if not k.startswith(('ACC_', 'RYNHULT_'))}
    env.update(PATH=str(tmp_path) + os.pathsep + env.get('PATH', ''), ACC_PACKAGES_ROOT=str(tmp_path / 'packages'),
               SAFE_HOST_COMMAND=str(safe), DEPLOY_CALLS=str(tmp_path / 'deploy-calls'))
    result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=20)
    calls = (tmp_path / 'deploy-calls').read_text() if (tmp_path / 'deploy-calls').exists() else ''
    if newer:
        assert result.returncode == 0, result.stderr
        assert 'codex KEPT' in result.stdout and '99.0.0' in result.stdout
        assert '/fixture/repo/deploy/folders/install.py' in calls
        assert 'example-agent' in calls and 'restart accd.service' in calls
        assert (tmp_path / 'npm-calls').read_text() == 'ls\n'
    else:
        assert result.returncode == 1
        assert 'codex FAILED' in result.stdout and 'npm install failed' in result.stdout
        assert not calls  # real failure still stops before brokers/folders/service restart
        assert (tmp_path / 'npm-calls').read_text() == 'ls\ninstall\n'
