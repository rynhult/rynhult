"""Add machine before v1.0 (no Windows or Mac installer yet): the from-source commands for a Windows PC (in Ubuntu on
WSL) and a Mac. They are built from checked values only, carry a private CA inside the command and check it against
its fingerprint before trusting it, and run against a local HTTPS server here with a fake installer and acc-node."""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from test_installer import (  # noqa: F401 (stubs: a fixture)
    ARGV_RECORDER,
    CODE,
    FakePrimary,
    argv_chain,
    fingerprint_of,
    make_ca,
    paste,
    pem,
    run,
    stubs,
)

from acc import __version__
from acc.server.api import nodes as nodes_api
from acc.server.api.nodes import (
    WSL_KEEPALIVE,
    ca_der,
    ca_fingerprint,
    prerelease,
    release_links,
    source_command,
    update_recovery,
)

ORIGIN = "https://acc.example.lan"


def der(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def script_of(command: str) -> str:
    command = re.sub(r"^echo [0-9a-f-]{27} \| ", "", command)  # the code, piped in: never part of the script
    prefix = "sudo sh -c '" if command.startswith("sudo ") else "sh -c '"
    assert command.startswith(prefix) and command.endswith("'")
    return command[len(prefix):-1]


@pytest.mark.parametrize("system", ["wsl", "mac"])
@pytest.mark.parametrize("private", [False, True])
def test_commands_parse_and_have_no_inner_quotes(system, private):
    ca = der(make_ca("Root")[1]) if private else None
    command = source_command(system, ORIGIN, ca, CODE, "build-box")
    script = script_of(command)
    assert "'" not in script and '"' not in script  # pastes intact, also through PowerShell to WSL
    assert subprocess.run(["sh", "-n", "-c", command]).returncode == 0
    assert f"{ORIGIN}/api/nodes/bundle" in script
    assert " -k " not in script and "--insecure" not in script
    # the code goes in on stdin: it is in no command line, so not in the process list or sudo's log either
    assert command.startswith(f"echo {CODE} | ") and command.count(CODE) == 1 and CODE not in script
    assert "read code" in script and "echo $code | " in script and "--code - --name build-box" in script
    if private:
        pin = hashlib.sha256(ca).hexdigest()
        assert f"--ca-fingerprint {pin}" in script and "--cacert $work/ca.pem" in script
        assert base64.b64encode(ca).decode() in script
        # the fingerprint check comes before the only download
        assert script.index(pin) < script.index("/api/nodes/bundle")
    else:
        assert "--trust system" in script and "--cacert" not in script and "openssl" not in script
    if system == "wsl":
        assert command.startswith(f"echo {CODE} | sudo sh -c '") and "deploy/node/install.sh --install-prereqs" in script
        assert ("sha256sum" in script) == private
    else:
        assert command.startswith(f"echo {CODE} | sh -c '") and "sudo" not in command
        assert "deploy/node/macos/install.sh" in script and ("shasum -a 256" in script) == private


@pytest.mark.parametrize("primary, code, name", [
    ("https://acc.example.lan'", CODE, "pc"),
    ("https://acc.example.lan/x", CODE, "pc"),
    ("https://user:pw@acc.example.lan", CODE, "pc"),
    ("http://acc.example.lan", CODE, "pc"),
    ("https://acc.example.lan?x=1", CODE, "pc"),
    ("https://acc.example.lan", "1a2b3c; id", "pc"),
    ("https://acc.example.lan", CODE, "pc;id"),
    ("https://acc.example.lan", CODE, "pc'"),
    ("https://acc.example.lan", CODE, 'pc"'),
    ("https://acc.example.lan", CODE, "$(id)"),
    ("https://acc.example.lan", CODE, "Build-Box"),
    ("https://acc.example.lan", CODE, "pc\nid"),
])
@pytest.mark.parametrize("system", ["wsl", "mac"])
def test_hostile_values_are_refused(system, primary, code, name):
    with pytest.raises(ValueError):
        source_command(system, primary, None, code, name)


def test_odd_ca_is_refused():
    with pytest.raises(ValueError):
        source_command("wsl", ORIGIN, b"x", CODE, "pc")


def test_ca_der_reads_one_certificate_as_the_fingerprint_does(tmp_path):
    first, second = make_ca("First")[1], make_ca("Second")[1]
    path = tmp_path / "ca.pem"
    path.write_bytes(pem(first))
    assert ca_der(path) == der(first) and ca_fingerprint(path) == fingerprint_of(first)
    path.write_bytes(pem(first) + pem(second))  # never two CAs in one command
    assert ca_der(path) is None and ca_fingerprint(path) is None
    assert ca_der(tmp_path / "missing.pem") is None


# --------------------------------------------------------------- the commands against a local HTTPS server --


def fake_bundle(record: Path) -> bytes:
    """A bundle whose installers record their arguments, user and folder instead of installing."""
    installer = f'printf "%s\\n" "install $*" >> {record}\nprintf "%s" "$0" > {record}.path\n'.encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in ("deploy/node/install.sh", "deploy/node/macos/install.sh"):
            member = tarfile.TarInfo(f"acc-{__version__}/{path}")
            member.size = len(installer)
            tar.addfile(member, io.BytesIO(installer))
    return buffer.getvalue()


@pytest.fixture
def setup(tmp_path, stubs):  # noqa: F811
    if not (shutil.which("curl") and shutil.which("openssl")):
        pytest.skip("needs curl and openssl")
    server = FakePrimary(tmp_path)
    record = tmp_path / "record"
    server.routes["/api/nodes/bundle"] = fake_bundle(record)
    home = tmp_path / "home"
    node = home / "Library/Application Support/ACC/bin/acc-node"
    node.parent.mkdir(parents=True)
    node.write_text(f'#!/bin/sh\nprintf "%s\\n" "acc-node $*" >> {record}\n'
                    f'case " $* " in *" --code - "*) read -r code; printf "stdin %s\\n" "$code" >> {record} ;; esac\n'
                    + ARGV_RECORDER)
    node.chmod(0o755)
    yield server, record, node, stubs, home
    server.close()


def execute(command: str, system: str, node: Path, tools: Path, home: Path) -> subprocess.CompletedProcess:
    if system == "wsl":  # the enroll step's fixed path, pointed at the fake
        assert "/usr/local/bin/acc-node enroll" in command
        command = command.replace("/usr/local/bin/acc-node", str(node).replace(" ", "\\ "))
    # pasted: the line is read from stdin, so only what it starts has command lines (recorded by the fake acc-node)
    return paste(command, PATH=f"{tools}:{os.environ['PATH']}", HOME=str(home), ACC_TEST_ARGV=str(home.parent / "argv"))


@pytest.mark.parametrize("system", ["wsl", "mac"])
def test_private_ca_command_checks_the_ca_then_installs_and_enrolls(setup, system):
    server, record, node, tools, home = setup
    command = source_command(system, server.url, der(server.ca), CODE, "pc")
    result = execute(command, system, node, tools, home)
    assert result.returncode == 0, result.stderr
    pin = fingerprint_of(server.ca)
    installer = "install --install-prereqs --no-wsl-isolate" if system == "wsl" else "install "
    assert record.read_text().splitlines() == [
        installer, f"acc-node enroll --primary {server.url} --ca-fingerprint {pin} --code - --name pc", f"stdin {CODE}"]
    assert server.hits == ["/api/nodes/bundle"]  # nothing fetched unverified: the CA came with the command
    assert CODE not in result.stdout + result.stderr
    assert CODE not in argv_chain(home.parent / "argv")  # acc-node, the sh -c script, sudo: no command line has it


def test_mac_enrolls_when_the_home_folder_has_a_space(setup):
    """No quotes are allowed in the script, so the enroll step must not split an unquoted $HOME."""
    server, record, node, tools, home = setup
    spaced = home.with_name("home with space")
    home.rename(spaced)
    result = execute(source_command("mac", server.url, der(server.ca), CODE, "pc"), "mac", node, tools, spaced)
    assert result.returncode == 0, result.stderr
    assert record.read_text().splitlines()[-2:] == [
        f"acc-node enroll --primary {server.url} --ca-fingerprint {fingerprint_of(server.ca)} --code - --name pc",
        f"stdin {CODE}"]


def test_the_code_is_not_in_sudos_command_line(setup):
    """sudo logs the command line it runs (auth log, journal): the WSL command's sudo never gets the code."""
    server, record, node, tools, home = setup
    log = tools.parent / "sudo.log"
    (tools / "sudo").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\nexec "$@"\n')
    result = execute(source_command("wsl", server.url, der(server.ca), CODE, "pc"), "wsl", node, tools, home)
    assert result.returncode == 0, result.stderr
    assert "sh -c set -eu; read code;" in log.read_text() and CODE not in log.read_text()
    assert record.read_text().splitlines()[-1] == f"stdin {CODE}"


@pytest.mark.parametrize("system", ["wsl", "mac"])
def test_a_tampered_ca_stops_before_anything_is_downloaded(setup, system):
    server, record, node, tools, home = setup
    command = source_command(system, server.url, der(server.ca), CODE, "pc")
    other = base64.b64encode(der(make_ca("Other Root")[1])).decode()
    command = command.replace(base64.b64encode(der(server.ca)).decode(), other)
    result = execute(command, system, node, tools, home)
    assert result.returncode != 0 and "does not match its fingerprint" in result.stderr
    assert server.hits == [] and not record.exists()


def test_a_cut_off_ca_stops_before_anything_is_downloaded(setup):
    """openssl can't read what's left: the command stops there (set -e), never trusting or fetching anything."""
    server, record, node, tools, home = setup
    command = source_command("mac", server.url, der(server.ca), CODE, "pc")
    encoded = base64.b64encode(der(server.ca)).decode()
    result = execute(command.replace(encoded, encoded[:40]), "mac", node, tools, home)
    assert result.returncode != 0
    assert server.hits == [] and not record.exists()


def test_a_server_with_another_ca_is_not_trusted(setup):
    """A server in the middle with its own CA: the command's CA doesn't verify its certificate, so nothing runs."""
    server, record, node, tools, home = setup
    real = make_ca("Real Root")[1]
    command = source_command("mac", server.url, der(real), CODE, "pc")
    result = execute(command, "mac", node, tools, home)
    assert result.returncode != 0 and not record.exists()


def test_system_trust_uses_curls_own_ca_bundle(setup, tmp_path):
    server, record, node, tools, home = setup
    command = source_command("wsl", server.url, None, CODE, "pc")
    bundle = tmp_path / "system-ca.pem"
    bundle.write_bytes(pem(server.ca))
    command = command.replace("/usr/local/bin/acc-node", str(node).replace(" ", "\\ "))
    result = run(["sh", "-c", command], PATH=f"{tools}:{os.environ['PATH']}", HOME=str(home),
                 CURL_CA_BUNDLE=str(bundle))
    assert result.returncode == 0, result.stderr
    assert record.read_text().splitlines()[-2:] == [
        f"acc-node enroll --primary {server.url} --trust system --code - --name pc", f"stdin {CODE}"]
    # and without that bundle the self-signed server is refused
    record.unlink()
    result = execute(source_command("wsl", server.url, None, CODE, "pc"), "wsl", node, tools, home)
    assert result.returncode != 0 and not record.exists()


@pytest.mark.parametrize("ok", [True, False])
def test_the_temporary_folder_is_removed(setup, ok):
    server, record, node, tools, home = setup
    if not ok:  # the enroll step fails: the download is still cleaned up
        node.write_text("#!/bin/sh\nexit 3\n")
    result = execute(source_command("mac", server.url, der(server.ca), CODE, "pc"), "mac", node, tools, home)
    assert result.returncode == (0 if ok else 3), result.stderr
    work = Path(Path(f"{record}.path").read_text().split("/source/")[0])
    assert work.name.startswith("rynhult-add.") and not work.exists()


# ------------------------------------------------------------------------------------------------------ API --


async def test_add_machine_returns_from_source_commands(api, tmp_path):
    settings = api.app.state.ctx.settings
    object.__setattr__(settings, "allowed_origins", (ORIGIN,))
    pub = (await api.post("/api/nodes", json={"name": "pub"})).json()
    assert pub["installers"] == {}  # pre-v1: the tabs show these instead
    assert pub["from_source"]["windows"] == source_command("wsl", ORIGIN, None, pub["code"], "pub")
    assert pub["from_source"]["mac"] == source_command("mac", ORIGIN, None, pub["code"], "pub")
    assert pub["from_source"]["windows_keepalive"] == WSL_KEEPALIVE

    root = make_ca("Test Root")[1]
    (tmp_path / "root.pem").write_bytes(pem(root))
    object.__setattr__(settings, "node_ca_file", tmp_path / "root.pem")
    priv = (await api.post("/api/nodes", json={"name": "priv"})).json()
    assert f"--ca-fingerprint {fingerprint_of(root)}" in priv["from_source"]["windows"]
    assert base64.b64encode(der(root)).decode() in priv["from_source"]["mac"]
    audit = (await api.get("/api/audit?action=node.add")).json()
    assert priv["code"] not in str(audit)


@pytest.mark.parametrize("version, links", [
    ("1.0.0", True), ("0.15.0", False), ("1.2.3+build.5", True), ("0.15.0-rc.1", False), ("2.0.0-beta.2+x", False), ("0.14.0", False)])
def test_release_candidates_get_no_installer_links(version, links):
    """Server RCs have public source assets, not native installers: do not advertise absent packages."""
    found = release_links("https://github.com/example-org/acc", version)
    assert bool(found) is links
    assert prerelease(version) is ("-" in version.split("+")[0])


async def test_add_machine_on_a_release_candidate_keeps_the_from_source_commands(api, monkeypatch):
    settings = api.app.state.ctx.settings
    object.__setattr__(settings, "allowed_origins", (ORIGIN,))
    object.__setattr__(settings, "source_url", "https://github.com/example-org/acc")
    monkeypatch.setattr(nodes_api, "__version__", "0.15.0-rc.1")
    rc = (await api.post("/api/nodes", json={"name": "rc"})).json()
    assert rc["version"] == "0.15.0-rc.1" and rc["prerelease"] is True
    assert rc["installers"] == {}
    assert rc["from_source"]["windows"] == source_command("wsl", ORIGIN, None, rc["code"], "rc")
    assert rc["from_source"]["mac"] == source_command("mac", ORIGIN, None, rc["code"], "rc")
    monkeypatch.setattr(nodes_api, "__version__", "1.0.0")
    final = (await api.post("/api/nodes", json={"name": "final"})).json()
    assert final["prerelease"] is False
    assert final["installers"]["mac"].endswith("/releases/download/v1.0.0/Rynhult-1.0.0.pkg")


def test_mac_update_on_a_release_candidate_is_the_source_command(monkeypatch):
    monkeypatch.setattr(nodes_api, "__version__", "0.15.0-rc.1")
    installer = release_links("https://github.com/example-org/acc").get("mac")
    assert installer is None
    recovery = update_recovery({"os": "darwin"}, Settings((ORIGIN,)), installer)
    assert recovery["command"] == source_command("mac", ORIGIN, None, None, None)


async def test_add_machine_shows_no_source_command_for_an_unusable_origin(api, tmp_path):
    settings = api.app.state.ctx.settings
    object.__setattr__(settings, "allowed_origins", ("https://user:SECRET@acc.example.lan/path",))
    added = (await api.post("/api/nodes", json={"name": "odd"})).json()
    assert added["from_source"] == {"windows": None, "mac": None, "windows_keepalive": WSL_KEEPALIVE}
    assert "SECRET" not in str(added["from_source"])


async def test_hostile_machine_names_are_refused_by_the_api(api):
    for name in ("pc;id", "pc'", "$(id)", "a b"):
        assert (await api.post("/api/nodes", json={"name": name})).status_code == 422


# ----------------------------------------------------------------------- the Mac's update without an installer --


class Settings:
    def __init__(self, origins, ca_file=None):
        self.allowed_origins, self.node_ca_file = origins, ca_file


def test_mac_update_is_only_a_mac_command_without_code_or_name():
    with pytest.raises(ValueError):
        source_command("wsl", ORIGIN, None, None, None)  # WSL's update is update_recovery's own command
    with pytest.raises(ValueError):
        source_command("mac", ORIGIN, None, CODE, None)
    script = script_of(source_command("mac", ORIGIN, None, None, None))
    assert "'" not in script and '"' not in script and "enroll" not in script and "sudo" not in script


def test_mac_update_runs_the_installer_again_and_keeps_the_enrollment(setup):
    server, record, node, tools, home = setup
    recovery = update_recovery({"os": "darwin"}, Settings((server.url,)), None)
    assert "keeps the existing enrollment" in recovery["detail"].lower()
    # the recovery builds its command for the main's own CA file; here the test server's CA, embedded and checked
    command = source_command("mac", server.url, der(server.ca), None, None)
    result = execute(command, "mac", node, tools, home)
    assert result.returncode == 0, result.stderr
    assert record.read_text().splitlines() == ["install "]  # no enroll step, no code
    assert server.hits == ["/api/nodes/bundle"]
    work = Path(Path(f"{record}.path").read_text().split("/source/")[0])
    assert work.name.startswith("rynhult-update.") and not work.exists()


def test_mac_update_with_a_tampered_ca_installs_nothing(setup):
    server, record, node, tools, home = setup
    command = source_command("mac", server.url, der(server.ca), None, None)
    command = command.replace(base64.b64encode(der(server.ca)).decode(),
                              base64.b64encode(der(make_ca("Other Root")[1])).decode())
    result = execute(command, "mac", node, tools, home)
    assert result.returncode != 0 and "does not match its fingerprint" in result.stderr
    assert server.hits == [] and not record.exists()


def test_mac_recovery_follows_the_mains_trust(tmp_path):
    root = make_ca("Main Root")[1]
    (tmp_path / "root.pem").write_bytes(pem(root))
    private = update_recovery({"os": "darwin"}, Settings((ORIGIN,), tmp_path / "root.pem"), None)
    assert private["command"] == source_command("mac", ORIGIN, der(root), None, None)
    assert fingerprint_of(root) in private["command"]
    (tmp_path / "broken.pem").write_text("not a certificate")
    broken = update_recovery({"os": "darwin"}, Settings((ORIGIN,), tmp_path / "broken.pem"), None)
    assert "command" not in broken and "CA" in broken["detail"]
    odd = update_recovery({"os": "darwin"}, Settings(("https://user:SECRET@acc.example.lan/x",)), None)
    assert "command" not in odd and "SECRET" not in str(odd)
    installer = update_recovery({"os": "darwin"}, Settings((ORIGIN,)), "https://example.com/Rynhult-0.15.0.pkg")
    assert "command" not in installer and "installer" in installer["detail"]


def test_wsl_keepalive_is_the_installers_task_with_no_values_filled_in():
    assert "-TaskName 'Rynhult node (WSL)'" in WSL_KEEPALIVE
    assert "-d Ubuntu-24.04 --exec /bin/sleep infinity" in WSL_KEEPALIVE
    assert "New-ScheduledTaskTrigger -AtLogOn -User $user" in WSL_KEEPALIVE
    assert "-RunLevel Limited" in WSL_KEEPALIVE and "-LogonType Interactive" in WSL_KEEPALIVE
    # the same task, action and settings Rynhult Setup registers (deploy/windows/AccWindows.psm1)
    module = (Path(__file__).resolve().parents[1] / "deploy/windows/AccWindows.psm1").read_text()
    for fragment in ("-ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries",
                     "-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)",
                     "-Description 'Keeps WSL (and Rynhult inside it) running while you are signed in.'",
                     "--exec /bin/sleep infinity"):
        assert fragment in module.replace("`\n            ", "") and fragment in WSL_KEEPALIVE
    assert CODE not in WSL_KEEPALIVE and "acc.example" not in WSL_KEEPALIVE


def test_setup_and_add_machine_use_one_keepalive_task_and_setup_migrates_the_old_one():
    """Rynhult Setup, the node installer and Add machine's block all register 'Rynhult node (WSL)'. An upgrade retires
    the old task only after the replacement starts; uninstall removes both."""
    import re
    root = Path(__file__).resolve().parents[1]
    setup = (root / "deploy/windows/install-acc.ps1").read_text()
    node = (root / "deploy/node/windows/install.ps1").read_text()
    module = (root / "deploy/windows/AccWindows.psm1").read_text()
    name = re.search(r"-TaskName '([^']+)'", WSL_KEEPALIVE).group(1)
    assert name == "Rynhult node (WSL)"
    assert re.search(r"\$NodeTask = '([^']+)'", setup).group(1) == name
    assert re.search(r"\[string\]\$TaskName = '([^']+)'", node).group(1) == name
    assert "LegacyNodeTask = 'ACC node (WSL)'" in module
    for script in (setup, node):
        assert "Register-AccNodeKeepAliveTask -TaskName" in script
        assert "Remove-AccLegacyNodeTask" not in script[script.index("try {"):].split("if ($Uninstall) {")[0]
    uninstall = setup[setup.index("if ($Uninstall) {"):]
    uninstall = uninstall[:uninstall.index("Clear-ResumeState")]
    assert "Unregister-AccNodeKeepAliveTask -TaskName $NodeTask" in uninstall and "Remove-AccLegacyNodeTask" in uninstall
    workflow = (root / ".github/workflows/node-packages.yml").read_text()
    assert "'Rynhult node (WSL)'" in workflow and "'ACC node (WSL)'" in workflow


def test_node_task_upgrade_is_create_first_and_uses_owned_task_validation():
    root = Path(__file__).resolve().parents[1]
    module = (root / 'deploy/windows/AccWindows.psm1').read_text()
    assert 'function Register-AccNodeKeepAliveTask' in module
    migrate = module.split('function Register-AccNodeKeepAliveTask', 1)[1].split('\nfunction ', 1)[0]
    assert migrate.index('Register-AccKeepAliveTask -TaskName') < migrate.index('Remove-AccLegacyNodeTask')
    assert 'Get-AccOwnedNodeKeepAliveTask' in migrate
    assert migrate.index('Wait-AccNodeKeepAliveTask -TaskName') < migrate.index('Remove-AccLegacyNodeTask')
    assert 'Diagnostics.Stopwatch' in module
    validate = module.split('function Get-AccOwnedNodeKeepAliveTask', 1)[1].split('\nfunction ', 1)[0]
    for field in ('TaskPath', 'UserId', 'Actions', 'Arguments', 'Execute', 'Get-AccKeepAliveIdentity'):
        assert field in validate
    for script in ('deploy/windows/install-acc.ps1', 'deploy/node/windows/install.ps1'):
        source = (root / script).read_text()
        assert 'Register-AccNodeKeepAliveTask -TaskName' in source


def test_machine_removal_guide_does_not_assign_the_legacy_task_to_current_installers():
    guide = (Path(__file__).resolve().parents[1] / 'docs/wiki/User-Guide/Nodes.md').read_text()
    assert '**ACC node (WSL)** if you used the Windows installer' not in guide
    assert '**ACC node (WSL)** on older installs' in guide


def test_generated_wsl_commands_do_not_depend_on_relayed_interactive_prompts():
    from acc.server.config import Settings
    script = script_of(source_command('wsl', ORIGIN, None, CODE, 'pc'))
    assert 'install.sh --install-prereqs --no-wsl-isolate' in script
    assert 're-run the installer with --wsl-isolate on a dedicated node' in script
    recovery = update_recovery({'os':'linux', 'wsl':2, 'wsl_distro':'Ubuntu-24.04'}, Settings(allowed_origins=(ORIGIN,)), None)
    assert recovery['command'].startswith("wsl -d 'Ubuntu-24.04' -u root --exec sh -c '")
    assert 'install.sh --update --no-wsl-isolate' in recovery['command']
