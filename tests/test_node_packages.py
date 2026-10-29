"""The node installers (deploy/node): dry runs and rendered files, without root, systemd or launchd."""
from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest
from isolated_installers import dry_run_subject, restore_source_names

REPO = Path(__file__).resolve().parents[1]
NODE = REPO / "deploy/node"


def stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def run(cmd: list[str], **env: str) -> subprocess.CompletedProcess:
    environment = {**os.environ, **env}
    with dry_run_subject(cmd, environment) as (command, replacements):
        result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=60)
        return restore_source_names(result, replacements)


@pytest.fixture(autouse=True)
def isolated_broker_settings(monkeypatch, tmp_path):
    # Dry-run installer tests must not discover this workstation's real broker accounts.
    monkeypatch.setenv("ACC_INSTALL_TEST_BROKER_ENV", str(tmp_path / "no-broker-settings"))


@pytest.fixture
def stubs(tmp_path):
    """A bin folder put first on PATH: a uv that does nothing (the dry run never calls it)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir, "uv", "exit 0")
    # the macOS installer refuses root (it installs for the signed-in user); suites also run as root
    stub(bin_dir, "id", 'case "$1" in -u) echo 501 ;; -un) /usr/bin/id -un ;; *) /usr/bin/id "$@" ;; esac')
    return bin_dir


def test_linux_dry_run_prints_every_step_and_changes_nothing(stubs, tmp_path):
    result = run(["bash", str(NODE / "install.sh"), "--dry-run", "--agent", "alex-agent"],
                 PATH=f"{stubs}:{os.environ['PATH']}")
    out = result.stdout
    assert "(dry run: nothing is changed)" in out
    assert "useradd --system --user-group" in out or "ok: user acc-node" in out
    for step in ("/opt/acc/app", "uv sync --frozen --no-dev",
                 "acc-broker@.socket.d/node.conf", "acc-broker@.service.d/node.conf",
                 "env ACC_AGENT_CLIENT=acc-node ACC_AGENT_PREFIX=/opt/acc /usr/local/sbin/acc-agent-add alex-agent",
                 "/etc/systemd/system/acc-node.service", "systemctl enable acc-node.service",
                 "install -d -o acc-node -g acc-node -m 0750 /srv/acc/packages/requests",
                 "/usr/local/sbin/acc-packages", "acc-packages.service.d/node.conf",
                 "deploy/lib/claude-apt-repo.sh /usr/local/sbin/acc-claude-apt-repo",
                 "systemctl enable --now acc-packages.path acc-packages-daily.timer",
                 "sudo acc-node enroll --primary"):
        assert step in out, step
    # Exit 2 exactly when the dry run found something a real install would stop on (here maybe tmux or systemd).
    assert result.returncode == (2 if "WOULD FAIL" in out else 0), out + result.stderr


def test_linux_refuses_bad_names_and_non_root():
    bad = run(["bash", str(NODE / "install.sh"), "--dry-run", "--agent", "Alex!"])
    assert bad.returncode == 1 and "bad agent account name" in bad.stderr
    if os.getuid() != 0:
        real = run(["bash", str(NODE / "install.sh")])
        assert real.returncode == 1 and "run as root" in real.stderr


def test_node_units_point_at_opt_acc():
    unit = (NODE / "acc-node.service").read_text()
    assert "User=acc-node" in unit
    assert "ExecStart=/opt/acc/app/.venv/bin/python -I -m acc.node run" in unit
    assert "ConditionPathExists=/etc/acc-node.env" in unit
    # agent software: acc-node writes only the helper's request folder; the helper's files are group acc-node
    assert "\nReadWritePaths=-/srv/acc/packages/requests -/srv/acc/updates/requests\n" in unit and "ProtectSystem=strict" in unit
    assert "Environment=ACC_PACKAGES_GROUP=acc-node" in (NODE / "acc-packages.service.d-node.conf").read_text()
    for limit in ("MemoryMax=", "TasksMax=", "LimitNOFILE="):  # bounds whatever a primary asks of it
        assert f"\n{limit}" in unit, limit
    assert "SocketGroup=acc-node" in (NODE / "acc-broker@.socket.d-node.conf").read_text()
    service = (NODE / "acc-broker@.service.d-node.conf").read_text()
    assert "ExecStart=\nExecStart=/opt/acc/app/.venv/bin/python -I -m acc.broker.server" in service


@pytest.fixture
def mac(tmp_path, stubs):
    """Enough of a Mac for the dry run: uname says Darwin, a Homebrew prefix with python3, a HOME of our own."""
    prefix = tmp_path / "homebrew"
    (prefix / "bin").mkdir(parents=True)
    os.symlink(sys.executable, prefix / "bin/python3")
    stub(stubs, "uname", "echo Darwin")
    stub(stubs, "sw_vers", "echo 15.0")
    stub(stubs, "brew", f'[ "$1" = --prefix ] && echo {prefix} && exit 0; echo "brew $*"')
    stub(stubs, "tmux", "exit 0")
    # Short: macOS limits socket paths to 104 bytes, and the installer checks ~/Library/Application Support/ACC/sock.
    home = tempfile.mkdtemp(prefix="acc-home-", dir="/tmp")
    yield {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": home}
    shutil.rmtree(home)


def test_macos_dry_run_renders_both_launch_agents(mac, tmp_path):
    rendered = tmp_path / "rendered"
    result = run(["bash", str(NODE / "macos/install.sh"), "--dry-run", "--render-dir", str(rendered)], **mac)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WOULD FAIL" not in result.stdout
    assert not (Path(mac["HOME"]) / "Library").exists()  # nothing written outside the render folder
    acc = f"{mac['HOME']}/Library/Application Support/ACC"
    account = run(["id", "-un"]).stdout.strip()

    broker = plistlib.loads((rendered / "local.acc.broker.plist").read_bytes())
    assert broker["Label"] == "local.acc.broker"
    assert broker["ProgramArguments"] == [f"{acc}/app/.venv/bin/python", "-I", "-m", "acc.broker.server"]
    env = broker["EnvironmentVariables"]
    assert env["ACC_BROKER_SOCKET"] == f"{acc}/sock/{account}.sock"
    assert env["ACC_ALLOWED_UIDS"] == "501"  # the id stub (stubs fixture): the installer reads id -u
    assert env["ACC_HOOK_PATH"] == f"{acc}/bin/acc-hook"
    assert broker["KeepAlive"] is True and broker["AbandonProcessGroup"] is True

    node = plistlib.loads((rendered / "local.acc.node.plist").read_bytes())
    assert node["ProgramArguments"][-2:] == ["acc.node", "run"]
    assert node["RunAtLoad"] is False
    assert node["KeepAlive"] == {"PathState": {f"{acc}/acc-node.env": True}}
    assert node["EnvironmentVariables"]["ACC_NODE_SOCKET_DIR"] == f"{acc}/sock"
    assert "@" not in (rendered / "local.acc.node.plist").read_text()  # every placeholder filled
    assert "launchctl bootstrap gui/" in result.stdout


def test_macos_python_scripts_run_with_homebrew_python(mac):
    """/usr/bin/python3 on a Mac is Apple's 3.9: acc-guard failed there on every tool call, so Copilot refused them."""
    result = run(["bash", str(NODE / "macos/install.sh"), "--dry-run"], **mac)
    assert result.returncode == 0, result.stdout + result.stderr
    for script in ("acc-guard", "acc-request", "acc-mcp-team"):
        assert (REPO / "bin" / script).read_text().splitlines()[0] in ("#!/usr/bin/python3 -I", "#!/usr/bin/env python3")
        line = next(x.strip() for x in result.stdout.splitlines()
                    if x.startswith("+ sed") and x.strip().endswith(f"/bin/{script}"))
        plain = line.replace("\\", "")
        assert "1s|^#!/usr/bin/python3|#!" in plain and "1s|^#!/usr/bin/env python3|#!" in plain, line
        assert plain.count("/homebrew/bin/python3|") == 2, line  # both forms repointed at Homebrew's python3


def test_macos_dry_run_reports_missing_homebrew(tmp_path, stubs):
    stub(stubs, "uname", "echo Darwin")
    path = f"{stubs}:" + ":".join(p for p in os.environ["PATH"].split(":") if not (Path(p) / "brew").exists())
    home = tmp_path / "home"
    home.mkdir()
    result = run(["bash", str(NODE / "macos/install.sh"), "--dry-run"], PATH=path, HOME=str(home))
    if Path("/opt/homebrew/bin/brew").exists() or Path("/usr/local/bin/brew").exists():
        pytest.skip("Homebrew is installed here")
    assert result.returncode == 2
    assert "Homebrew missing" in result.stdout


def test_plist_template_is_a_valid_plist():
    template = plistlib.loads((NODE / "macos/acc.plist.in").read_bytes())
    assert template["Label"] == "local.acc.@JOB@"


@pytest.mark.parametrize("before, changed", [
    ("", True),
    ("[network]\nhostname=pc\n", True),
    ("[boot]\nsystemd=false\ncommand=echo hi\n", True),
    ("[boot]\nsystemd = true\n", False),
])
def test_wsl_helper_turns_on_systemd_once(tmp_path, before, changed):
    conf = tmp_path / "wsl.conf"
    conf.write_text(before)
    first = run(["sh", str(NODE / "windows/wsl-node.sh"), "systemd"], ACC_WSL_CONF=str(conf))
    assert first.returncode == (10 if changed else 0)
    text = conf.read_text()
    assert text.count("systemd") == 1 and ("systemd=true" in text or "systemd = true" in text)
    assert text.index("[boot]") < text.index("systemd")
    second = run(["sh", str(NODE / "windows/wsl-node.sh"), "systemd"], ACC_WSL_CONF=str(conf))
    assert second.returncode == 0 and conf.read_text() == text


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_node_scripts_pass_shellcheck():
    scripts = [NODE / "install.sh", NODE / "acc-node", NODE / "macos/install.sh", NODE / "macos/acc-node",
               NODE / "windows/wsl-node.sh", REPO / "deploy/broker/acc-agent-add",
               *sorted((REPO / "deploy/lib").glob("*.sh")),
               *sorted((REPO / "deploy/macos").glob("*.sh")), *sorted((REPO / "deploy/macos/scripts").iterdir())]
    result = run(["shellcheck", *map(str, scripts)])
    assert result.returncode == 0, result.stdout


# ------------------------------------------------------------------ root runs only root's tooling (review #8, #10)


def linux_dry_run(path: str, **env: str) -> subprocess.CompletedProcess:
    return run(["bash", str(NODE / "install.sh"), "--dry-run", "--agent", "alex-agent"], PATH=path, **env)


def path_without(tool: str, shadows: Path) -> str:
    """PATH with `tool` gone: a folder holding it is replaced by a copy of links to everything else in it (dropping
    the folder would drop bash and coreutils too when uv lives in /usr/bin)."""
    parts = []
    for index, folder in enumerate(os.environ["PATH"].split(":")):
        if not (Path(folder) / tool).exists():
            parts.append(folder)
            continue
        shadow = shadows / str(index)
        shadow.mkdir(parents=True)
        for entry in Path(folder).iterdir():
            if entry.name != tool:
                (shadow / entry.name).symlink_to(entry)
        parts.append(str(shadow))
    return ":".join(parts)


def test_a_uv_in_a_home_folder_is_never_used(tmp_path):
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    stub(home / ".local/bin", "uv", "exit 0")
    plain = path_without("uv", tmp_path / "shadow")
    result = linux_dry_run(f"{home / '.local/bin'}:{plain}", HOME=str(home), SUDO_USER="",
                           ACC_INSTALL_TEST_OWNER=str(os.getuid()))  # even when its owner would count as root
    assert f"ok: uv at {home}" not in result.stdout
    assert f"not using {home}/.local/bin/uv" in result.stderr
    if not any(Path(p).exists() for p in ("/usr/bin/uv", "/usr/local/bin/uv")):  # the installer's fixed places
        assert "WOULD FAIL: uv missing (or not root's)" in result.stdout
        assert result.returncode == 2


def test_a_root_owned_uv_is_used_quoted(tmp_path):
    tools = tmp_path / "tools dir"  # a space: the path must survive as one argument
    tools.mkdir(mode=0o755)
    tools.chmod(0o755)
    stub(tools, "uv", "exit 0")
    result = linux_dry_run(f"{tools}:{os.environ['PATH']}", ACC_INSTALL_TEST_OWNER=str(os.getuid()))
    assert f"ok: uv at {tools}/uv" in result.stdout, result.stdout + result.stderr
    assert (f"+ env UV_CACHE_DIR=/opt/acc/.uv-cache UV_PYTHON_DOWNLOADS=never {tools}/uv sync --frozen --no-dev "
            "--python /usr/bin/python3 --directory /opt/acc/app") in result.stdout
    assert "cd /opt/acc/app &&" not in result.stdout  # no shell string around it


@pytest.mark.parametrize("weak", ["file", "folder"])
def test_a_uv_others_can_write_is_refused(tmp_path, weak):
    tools = tmp_path / "tools"
    tools.mkdir()
    stub(tools, "uv", "exit 0")
    (tools / "uv" if weak == "file" else tools).chmod(0o777 if weak == "file" else 0o775)
    result = linux_dry_run(f"{tools}:{os.environ['PATH']}", ACC_INSTALL_TEST_OWNER=str(os.getuid()))
    assert f"ok: uv at {tools}/uv" not in result.stdout
    assert f"not using {tools}/uv" in result.stderr


def test_a_uv_owned_by_someone_else_is_refused(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    stub(tools, "uv", "exit 0")
    result = linux_dry_run(f"{tools}:{os.environ['PATH']}")  # owned by the test user, who isn't root
    if os.getuid() == 0:
        pytest.skip("running as root: the stub is root's")
    assert f"ok: uv at {tools}/uv" not in result.stdout


def test_the_test_owner_knob_only_works_in_a_dry_run():
    script = (NODE / "install.sh").read_text()
    assert '[ -n "$DRY" ] && OWNER=${ACC_INSTALL_TEST_OWNER:-0}' in script
    assert "OWNER=0\n" in script


def test_the_installed_code_is_normalized_to_root(stubs):
    out = linux_dry_run(f"{stubs}:{os.environ['PATH']}").stdout
    steps = [line for line in out.splitlines() if line.startswith("+ ")]
    extract = next(i for i, line in enumerate(steps) if "tar -C /opt/acc/app" in line)
    chown = steps.index("+ chown -R -h root:root /opt/acc/app")
    chmod = steps.index("+ chmod -R go-w /opt/acc/app")
    sync = next(i for i, line in enumerate(steps) if " sync --frozen" in line)
    assert extract < chown < chmod < sync  # before root runs anything from it (the kept .venv included)


# ------------------------------------------------------------------ enrolling as root (review #8)


@pytest.fixture
def root_wrapper(tmp_path):
    """deploy/node/acc-node with its fixed paths pointed into tmp_path, and stubs: id says root, runuser just runs."""
    bin_dir = tmp_path / "rbin"
    bin_dir.mkdir()
    stub(bin_dir, "id", 'case "$1" in -u) echo 0 ;; *) /usr/bin/id "$@" ;; esac')
    stub(bin_dir, "runuser", f'echo "runuser $*" >> {tmp_path}/runuser.log; shift 3; exec "$@"')
    stub(bin_dir, "chown", f'echo "chown $*" >> {tmp_path}/chown.log')
    stub(bin_dir, "systemctl", "exit 0")
    # -I deliberately ignores PYTHONPATH. Bootstrap this checkout explicitly,
    # not the shared interpreter's editable install of another worktree.
    python = bin_dir / 'isolated-python'
    bootstrap = f"""import importlib.util,runpy,sys
assert sys.argv[1:4] == ['-I', '-m', 'acc.node']
spec = importlib.util.spec_from_file_location('acc', {str(REPO / 'src/acc/__init__.py')!r})
module = importlib.util.module_from_spec(spec)
sys.modules['acc'] = module
spec.loader.exec_module(module)
sys.argv = ['acc.node', *sys.argv[4:]]
runpy.run_module('acc.node', run_name='__main__', alter_sys=True)
"""
    stub(bin_dir, python.name, f'exec {shlex.quote(sys.executable)} -I -c {shlex.quote(bootstrap)} "$@"')
    script = (NODE / "acc-node").read_text()
    assert script.count("/opt/acc/app/.venv/bin/python") == 1 and script.count("/var/lib/acc-node") >= 1
    script = (script.replace("/opt/acc/app/.venv/bin/python", str(python))
              .replace("/var/lib/acc-node", str(tmp_path / "state"))
              .replace("/run/acc-node-enroll", str(tmp_path / "staging")))
    wrapper = tmp_path / "acc-node"
    wrapper.write_text(script)
    (tmp_path / "state").mkdir(mode=0o700)
    return {"wrapper": wrapper, "python": python,
            "env": {"PATH": f"{bin_dir}:{os.environ['PATH']}", "PYTHONPATH": str(REPO / "src"),
                                        "ACC_NODE_CONFIG": str(tmp_path / "acc-node.env")}}


def test_root_enroll_hands_key_and_ca_to_the_node_user(root_wrapper, tmp_path):
    pytest.importorskip("cryptography")  # CI runs this file with the standard library only
    from test_node import CA, TLS, EnrollServer, pasted

    tls = TLS(CA(tmp_path / "ca"))
    planted = tmp_path / "outside"
    planted.write_text("not yours")
    (tmp_path / "state" / "node.key").symlink_to(planted)  # what a compromised daemon might leave there
    with EnrollServer(tls) as server:
        result = run(["sh", str(root_wrapper["wrapper"]), "enroll", "--primary", server.url,
                      "--ca-fingerprint", pasted(tls.ca.fingerprint), "--code", "c0de", "--name", "lab-1"],
                     **root_wrapper["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    state = tmp_path / "state"
    assert (state / "node.key").is_file() and not (state / "node.key").is_symlink()
    assert planted.read_text() == "not yours"  # replaced, never followed
    assert oct((state / "node.key").stat().st_mode & 0o777) == "0o600"
    assert (state / "ca.pem").read_bytes() == tls.ca.pem
    config = (tmp_path / "acc-node.env").read_text()
    assert f"ACC_NODE_CA_FILE={state}/ca.pem" in config and "staging" not in config
    log = (tmp_path / "runuser.log").read_text().splitlines()
    assert log == [f"runuser -u acc-node -- {root_wrapper['python']} -I -m acc.node adopt --state-dir {state} node.key",
                   f"runuser -u acc-node -- {root_wrapper['python']} -I -m acc.node adopt --state-dir {state} ca.pem"]
    assert not (tmp_path / "chown.log").exists()  # no ownership changes at all
    assert not list(tmp_path.glob("staging.*"))  # the staging folder is gone


def test_refused_root_enroll_changes_nothing(root_wrapper, tmp_path):
    pytest.importorskip("cryptography")  # CI runs this file with the standard library only
    from test_node import CA, TLS, EnrollServer

    tls = TLS(CA(tmp_path / "ca"))
    with EnrollServer(tls, status=403) as server:
        result = run(["sh", str(root_wrapper["wrapper"]), "enroll", "--primary", server.url,
                      "--ca-fingerprint", tls.ca.fingerprint, "--code", "c0de", "--name", "lab-1"], **root_wrapper["env"])
    assert result.returncode != 0
    assert not list((tmp_path / "state").iterdir()) and not (tmp_path / "runuser.log").exists()
    assert not list(tmp_path.glob("staging.*"))


# ------------------------------------------------------------------ the macOS installer (docs/INSTALLER.md, piece 4)

MACOS = REPO / "deploy/macos"
SETUP = MACOS / "acc-setup.sh"
SAMPLE = ("acc-node enroll --primary https://acc.example.com --trust system --code 3fa21c-0b9d4e-77aa01-c3d2e1 "
          "--name studio-mac")


def enroll_examples() -> list[tuple[str, str, str]]:
    """tests/data/enroll-commands.txt: (accept|reject, command, expected values), as its header describes."""
    examples = []
    for line in (REPO / "tests/data/enroll-commands.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        kind, _, rest = line.partition("\t")
        command, _, expected = rest.partition("\t")
        examples.append((kind, command, expected))
    return examples


@pytest.mark.parametrize("kind, command, expected", enroll_examples())
def test_the_setup_parser_accepts_only_add_machine_commands(kind, command, expected):
    result = run(["bash", str(SETUP), "parse", command])
    if kind == "reject":
        assert result.returncode == 2 and result.stderr.strip() and not result.stdout, result.stdout
        return
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in result.stdout.splitlines())
    primary, trust, fingerprint, code, name = expected.split(" ")
    assert values == {"primary": primary, "trust": trust, "fingerprint": "" if fingerprint == "-" else fingerprint,
                      "code": code, "name": name}


def test_the_setup_parser_self_test_passes():
    """The same examples through the script's own self-test (CI runs it under macOS's bash 3.2)."""
    result = run(["bash", str(SETUP), "self-test", str(REPO / "tests/data/enroll-commands.txt")])
    assert result.returncode == 0, result.stdout
    assert f"{len(enroll_examples()) + 2} examples, 0 failed" in result.stdout


def test_the_parser_never_evaluates_what_it_is_given(tmp_path):
    """Sourced, the parser function on its own: shell syntax in a value stays text."""
    marker = tmp_path / "ran"
    for value in (f"$(touch {marker})", f"`touch {marker}`", f"x;touch {marker}"):
        script = f'source "{SETUP}"; parse_enroll "$1" && echo accepted || echo "refused: $P_ERROR"'
        result = run(["bash", "-c", script, "bash", SAMPLE.replace("studio-mac", value)])
        assert result.stdout.startswith("refused: "), result.stdout + result.stderr
    assert not marker.exists()


def test_setup_dry_run_runs_the_whole_flow_and_changes_nothing(mac):
    result = run(["bash", str(SETUP), "run", "--dry-run", SAMPLE], **mac)
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    assert "(dry run: nothing is changed)" in out and "WOULD FAIL" not in out
    assert "ok: macOS, user" in out
    assert "== installing the Rynhult node (deploy/node/macos/install.sh)" in out
    assert "launchctl bootstrap gui/501" in out  # install.sh's own dry run
    acc = f"{mac['HOME']}/Library/Application Support/ACC"
    node = acc.replace(" ", "\\ ")
    assert (f"+ {node}/bin/acc-node enroll --primary https://acc.example.com --trust system --code <the code> "
            "--name studio-mac") in out
    assert "3fa21c-0b9d4e" not in out  # the one-time code stays out of logs
    assert "launchctl kickstart -k gui/501/local.acc.node" in out
    assert out.rstrip().endswith("dry run done: nothing was changed")
    assert not (Path(mac["HOME"]) / "Library").exists()  # no log, no LaunchAgents, no Rynhult folder


def test_setup_refuses_a_bad_command_before_installing_anything(mac):
    result = run(["bash", str(SETUP), "run", "--dry-run", SAMPLE + "; rm -rf ~"], **mac)
    assert result.returncode == 2
    assert "That isn't the command from Add machine: it holds characters" in result.stderr
    assert "install.sh" not in result.stdout


def test_setup_explains_how_to_get_homebrew(tmp_path, stubs):
    stub(stubs, "uname", "echo Darwin")
    path = f"{stubs}:" + ":".join(p for p in os.environ["PATH"].split(":") if not (Path(p) / "brew").exists())
    if Path("/opt/homebrew/bin/brew").exists() or Path("/usr/local/bin/brew").exists():
        pytest.skip("Homebrew is installed here")
    result = run(["bash", str(SETUP), "check"], PATH=path, HOME=str(tmp_path))
    assert result.returncode == 3
    assert "Rynhult needs Homebrew" in result.stderr and "https://brew.sh" in result.stderr
    log = tmp_path / "Library/Logs/ACC/setup.log"
    assert str(log) in result.stderr
    assert "Rynhult needs Homebrew" in log.read_text()


def test_setup_refuses_root(mac, stubs):
    stub(stubs, "id", 'case "$1" in -u) echo 0 ;; *) /usr/bin/id "$@" ;; esac')
    result = run(["bash", str(SETUP), "run", "--dry-run", SAMPLE], **mac)
    assert result.returncode == 4 and "not as root" in result.stderr
    assert "install.sh" not in result.stdout


@pytest.fixture
def installed_mac(mac, stubs, tmp_path):
    """The mac fixture after install.sh: an acc-node stub that records its arguments, and a launchctl stub."""
    acc = Path(mac["HOME"]) / "Library/Application Support/ACC"
    (acc / "bin").mkdir(parents=True)
    calls = tmp_path / "calls.log"
    stub(acc / "bin", "acc-node", f'echo "acc-node $*" >> {calls}; [ "$1" != enroll ] || echo "Enrolled as $9."')
    stub(stubs, "launchctl", f'echo "launchctl $*" >> {calls}')
    return {"env": mac, "acc": acc, "calls": calls, "log": Path(mac["HOME"]) / "Library/Logs/ACC/setup.log"}


def test_setup_enrolls_with_exactly_the_parsed_values(installed_mac):
    pasted = ("  acc-node enroll --name studio-mac --code 3FA21C-0B9D4E-77AA01-C3D2E1 --ca-fingerprint "
              + "AB" * 32 + " --primary https://acc.example.com:8443/\n")
    result = run(["bash", str(SETUP), "enroll", "--log", "--", pasted], **installed_mac["env"])
    assert result.returncode == 0, result.stdout + result.stderr + installed_mac["log"].read_text()
    # --log: everything goes to the log; the one line Rynhult Setup.app shows comes out.
    assert result.stdout.startswith("This Mac is now the machine studio-mac of https://acc.example.com:8443")
    assert str(installed_mac["log"]) in result.stdout and not result.stderr
    calls = installed_mac["calls"].read_text().splitlines()
    assert calls == [
        "acc-node enroll --primary https://acc.example.com:8443 --ca-fingerprint " + "ab" * 32
        + " --code 3fa21c-0b9d4e-77aa01-c3d2e1 --name studio-mac",
        "launchctl kickstart -k gui/501/local.acc.node",  # a re-enrollment takes effect at once
        "acc-node status",
    ]
    log = installed_mac["log"].read_text()
    assert "=== Rynhult Setup enroll" in log and "Enrolled as" in log
    assert "3fa21c" not in log.replace("Enrolled as", "")  # the code is only in acc-node's arguments


def test_setup_reports_a_failed_enroll_with_its_reason(installed_mac):
    acc = installed_mac["acc"]
    stub(acc / "bin", "acc-node", 'echo "acc-node: the primary refused enrollment: HTTP 403" >&2; exit 1')
    result = run(["bash", str(SETUP), "enroll", "--log", SAMPLE], **installed_mac["env"])
    assert result.returncode == 1
    assert "Enrolling failed: acc-node: the primary refused enrollment: HTTP 403" in result.stderr
    assert str(installed_mac["log"]) in result.stderr
    assert "the primary refused" in installed_mac["log"].read_text()


def test_setup_status_says_when_this_mac_is_enrolled(installed_mac):
    assert run(["bash", str(SETUP), "status"], **installed_mac["env"]).stdout == ""
    (installed_mac["acc"] / "acc-node.env").write_text(
        "ACC_NODE_PRIMARY=https://acc.example.com\nACC_NODE_ID=ab12\nACC_NODE_NAME=studio-mac\n")
    status = run(["bash", str(SETUP), "status"], **installed_mac["env"])
    assert status.stdout.strip() == "enrolled as studio-mac with https://acc.example.com"


def test_build_pkg_dry_run_stages_only_the_tracked_source(tmp_path):
    work, out_dir = tmp_path / "work", tmp_path / "out"
    result = run(["bash", str(MACOS / "build-pkg.sh"), "--dry-run", "--work", str(work), "--out", str(out_dir),
                  "--version", "9.8.7"])
    out = result.stdout
    assert result.returncode == (2 if "WOULD FAIL" in out else 0), out + result.stderr  # Linux: no pkgbuild
    src = work / "root/Library/Application Support/ACC/src"
    staged = sorted(str(p.relative_to(src)) for p in src.rglob("*") if p.is_file() or p.is_symlink())
    tracked = run(["git", "-C", str(REPO), "ls-files", "--cached"]).stdout.splitlines()
    assert staged == sorted(f for f in tracked if (REPO / f).exists() or (REPO / f).is_symlink())
    for gone in (".git", ".venv", "node_modules", "data", "web/node_modules"):
        assert not (src / gone).exists(), gone
    assert os.access(src / "deploy/node/macos/install.sh", os.X_OK)
    assert os.access(src / "deploy/macos/acc-setup.sh", os.X_OK)
    steps = [line for line in out.splitlines() if line.startswith("+ ")]
    order = ["+ osacompile -o", "+ codesign --force --sign -", "+ pkgbuild --analyze", "+ pkgbuild --root",
             "+ productbuild --distribution"]
    positions = [next(i for i, step in enumerate(steps) if step.startswith(prefix)) for prefix in order]
    assert positions == sorted(positions), steps
    assert "--identifier local.acc.pkg --version 9.8.7 --install-location /" in out
    assert f"{out_dir}/Rynhult-9.8.7.pkg" in out and not out_dir.exists()  # nothing built
    import xml.etree.ElementTree as ET
    distribution = ET.parse(work / "distribution.xml").getroot()
    assert distribution.find("welcome").get("file") == "welcome.html"
    assert distribution.findall("pkg-ref")[-1].get("version") == "9.8.7"
    assert sorted(p.name for p in (work / "scripts").iterdir()) == ["postinstall", "preinstall"]


def test_the_welcome_page_says_what_a_mac_can_be():
    welcome = (MACOS / "resources/welcome.html").read_text()
    check = (MACOS / "check-pkg.sh").read_text()
    for phrase in ("additional machine only", "Sessions run as you"):  # what check-pkg.sh looks for
        assert phrase in welcome and phrase in check
    assert "not a main Rynhult" in welcome


@pytest.fixture
def postinstall(tmp_path):
    """Stubs for what the postinstall calls: the console user is alex (uid 501); launchctl and sudo only record."""
    bin_dir = tmp_path / "pbin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    console = tmp_path / "console"
    console.write_text("alex")
    stub(bin_dir, "stat", f'[ "$1 $2 $3" = "-f %Su /dev/console" ] && cat {console}')
    stub(bin_dir, "id", 'case "$1 $2" in "-u alex") echo 501 ;; "-u root") echo 0 ;; *) exit 1 ;; esac')
    for tool in ("launchctl", "sudo", "open"):
        stub(bin_dir, tool, f'echo "{tool} $*" >> {calls}')

    def go(target: str = "/", **env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        result = run(["/bin/sh", str(MACOS / "scripts/postinstall"), "/tmp/ACC.pkg", target, target, "/"],
                     **{"PATH": f"{bin_dir}:{os.environ['PATH']}", "COMMAND_LINE_INSTALL": "", **env})
        return result, calls.read_text().splitlines() if calls.exists() else []
    go.console = console
    return go


def test_postinstall_opens_acc_setup_as_the_signed_in_user(postinstall):
    result, calls = postinstall()
    assert result.returncode == 0, result.stderr
    # launchctl asuser puts it in alex's GUI session, sudo -u makes it alex's process: nothing of it runs as root.
    assert calls == ["launchctl asuser 501 sudo -u alex -H open -a /Applications/ACC Setup.app"]
    assert "opening Rynhult Setup for alex" in result.stdout


def test_postinstall_never_runs_the_install_itself():
    script = (MACOS / "scripts/postinstall").read_text()
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert "install.sh" not in code and "acc-setup.sh" not in code and "acc-node" not in code
    assert code.count("launchctl asuser") == 1 and 'sudo -u "$user"' in code


@pytest.mark.parametrize("user", ["root", "loginwindow", "_mbsetupuser", ""])
def test_postinstall_with_nobody_at_the_screen_only_says_how_to_finish(postinstall, user):
    postinstall.console.write_text(user)
    result, calls = postinstall()
    assert result.returncode == 0 and calls == []
    assert "opens Rynhult Setup from Applications" in result.stdout


@pytest.mark.parametrize("target, env", [("/", {"COMMAND_LINE_INSTALL": "1"}), ("/Volumes/Other", {})])
def test_postinstall_from_the_command_line_or_to_another_volume_opens_nothing(postinstall, target, env):
    result, calls = postinstall(target, **env)
    assert result.returncode == 0 and calls == []


def test_preinstall_removes_only_the_previous_source(tmp_path):
    src = tmp_path / "Library/Application Support/ACC/src"
    (src / "deploy").mkdir(parents=True)
    (src / "deploy/old-file").write_text("gone in the new version")
    user_acc = tmp_path / "Users/alex/Library/Application Support/ACC"
    user_acc.mkdir(parents=True)
    result = run(["/bin/sh", str(MACOS / "scripts/preinstall"), "/tmp/ACC.pkg", str(tmp_path), str(tmp_path), "/"])
    assert result.returncode == 0, result.stderr
    assert not src.exists() and user_acc.exists()
    # a link there is never followed
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("x")
    src.symlink_to(elsewhere)
    assert run(["/bin/sh", str(MACOS / "scripts/preinstall"), "x", str(tmp_path), str(tmp_path), "/"]).returncode == 0
    assert (elsewhere / "keep").exists()


def test_the_applet_uses_only_what_acc_setup_offers():
    applet = (MACOS / "acc-setup.applescript").read_text()
    assert applet.isascii()  # osacompile reads plain text
    assert 'property setupScript : "/Library/Application Support/ACC/src/deploy/macos/acc-setup.sh"' in applet
    for call in ('" check"', '" status"', '" parse " & quoted form of pasted', 'setup & " install --log"',
                 '" enroll --log -- " & quoted form of pasted'):
        assert call in applet, call
    assert "do shell script pasted" not in applet  # the paste is only ever an argument
    usage = SETUP.read_text()
    for command in ("parse", "check", "status", "install", "enroll"):
        assert f"acc-setup.sh {command}" in usage


@pytest.fixture
def setup_tree(mac, tmp_path):
    """acc-setup.sh in a tree of its own, with an install.sh stub there (its last line is what Rynhult Setup shows)."""
    tree = tmp_path / "tree"
    (tree / "deploy/macos").mkdir(parents=True)
    (tree / "deploy/node/macos").mkdir(parents=True)
    shutil.copy(SETUP, tree / "deploy/macos/acc-setup.sh")
    (tree / "deploy/lib").mkdir()
    shutil.copy(REPO / "deploy/lib/acc-env.sh", tree / "deploy/lib/acc-env.sh")
    (tree / "pyproject.toml").write_text('version = "9.8.7"\n')
    install = tree / "deploy/node/macos"

    def installer(body: str) -> None:
        stub(install, "install.sh", body)
    installer('echo "== prerequisites"; echo "== done"')
    return {"setup": tree / "deploy/macos/acc-setup.sh", "installer": installer, "env": mac,
            "log": Path(mac["HOME"]) / "Library/Logs/ACC/setup.log"}


def test_setup_install_says_what_it_installed(setup_tree):
    result = run(["bash", str(setup_tree["setup"]), "install", "--log"], **setup_tree["env"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Rynhult 9.8.7 is installed on this Mac.") and not result.stderr
    assert "== prerequisites" in setup_tree["log"].read_text()


def test_setup_install_failure_shows_install_sh_last_words(setup_tree):
    setup_tree["installer"]('echo "== prerequisites"; echo "error: launchctl could not load x.plist" >&2; exit 1')
    result = run(["bash", str(setup_tree["setup"]), "install", "--log"], **setup_tree["env"])
    assert result.returncode == 1 and not result.stdout
    assert "Installing failed: error: launchctl could not load x.plist" in result.stderr
    assert str(setup_tree["log"]) in result.stderr


def test_setup_without_log_prints_and_logs(setup_tree):
    result = run(["bash", str(setup_tree["setup"]), "install"], **setup_tree["env"])
    assert result.returncode == 0
    assert "== prerequisites" in result.stdout and "== prerequisites" in setup_tree["log"].read_text()


# ------------------------------------------------------------------ the Windows installer (deploy/windows)
# Nothing here runs Windows: these check what can be checked on Linux. The installer itself is built and run (as a
# dry run) by .github/workflows/installers-windows.yml.

WINDOWS = REPO / "deploy/windows"
ENROLL_CASES = REPO / "tests/data/enroll-commands.json"


def iss_text() -> str:
    return (WINDOWS / "acc.iss").read_text(encoding="utf-8-sig")


def iss_setting(name: str) -> str:
    match = re.search(rf"^{name}=(.*)$", iss_text(), re.M)
    assert match, name
    return match.group(1).strip()


def test_installer_references_files_that_exist():
    text = iss_text()
    sources = re.findall(r'^Source: "([^"]+)"', text, re.M)
    assert sources
    for source in sources:
        path = WINDOWS / source.replace("\\", "/")
        if "*" in path.name:
            assert path.parent.is_dir(), source
        else:
            assert path.is_file(), source
    assert (WINDOWS / iss_setting("LicenseFile").replace("\\", "/")).is_file()
    # what the [Code] section runs, by its installed path ({app}\src is the repository)
    for script in re.findall(r"\{app\}\\src\\([^'\"]+\.ps1)", text):
        assert (REPO / script.replace("\\", "/")).is_file(), script
    assert "{app}\\src\\deploy\\windows\\install-acc.ps1" in text
    # install-acc.ps1 reaches these through the installed tree
    for relative in ("deploy/windows/AccWindows.psm1", "deploy/node/windows/wsl-node.sh", "deploy/setup.sh",
                     "deploy/node/install.sh", "deploy/node/acc-node"):
        assert (REPO / relative).is_file(), relative


def test_installer_bundles_the_tree_like_a_release():
    excludes = re.search(r'^#define Excludes "([^"]+)"', iss_text(), re.M).group(1).split(",")
    for left_out in (".git", ".venv", "node_modules", "\\data", ".env", "__pycache__"):
        assert left_out in excludes, left_out
    assert 'Source: "..\\..\\*"; DestDir: "{app}\\src"; Excludes: "{#Excludes}"; Flags: recursesubdirs' in iss_text()


def test_installer_version_comes_from_pyproject():
    text = iss_text()
    assert iss_setting("AppVersion") == "{#AppVersion}"
    assert iss_setting("OutputBaseFilename") == "Rynhult-Setup-{#AppVersion}"
    assert '"..\\..\\pyproject.toml"' in text
    assert not re.search(r"\d+\.\d+\.\d+", re.sub(r"EncodeVer\([^)]*\)", "", text))  # no version written in
    # What the preprocessor does: the first line starting with version = "..., minus that and the closing quote.
    lines = (REPO / "pyproject.toml").read_text().splitlines()
    line = next(line for line in lines if line[:11] == 'version = "')
    assert "Copy(VersionLine, 1, 11) == 'version = \"'" in text
    assert "Copy(VersionLine, 12, Len(VersionLine) - 12)" in text
    extracted = line[11:11 + len(line) - 12]
    assert extracted == tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]


def test_installer_passes_answers_in_the_environment_only():
    text = iss_text()
    for name in ("ACC_HOSTNAME", "ACC_OWNER", "ACC_ZONE", "CF_API_TOKEN", "ACC_AGENT_CLIS", "ACC_ENROLL_COMMAND"):
        assert f"SetEnvironmentVariable('{name}'" in text, name
    # the command lines it builds hold fixed words and paths only
    calls = re.findall(r"ScriptCommand\(ExpandConstant[^\n]*\n[^\n]*", text)
    assert len(calls) == 3  # validate, install, uninstall
    for arguments in calls:
        assert "Values[" not in arguments and "SelectedClis" not in arguments, arguments
    script = (WINDOWS / "install-acc.ps1").read_text()
    for name in ("ACC_HOSTNAME", "ACC_OWNER", "ACC_ZONE", "ACC_AGENT_CLIS", "ACC_ENROLL_COMMAND", "CF_API_TOKEN"):
        if name.startswith("ACC_"):
            assert f'Get-AccEnvironmentValue "{name}"' in script, name
        else:
            assert f"$env:{name}" in script, name
    assert "GetEnvironmentVariables('Process')" in (WINDOWS / "AccWindows.psm1").read_text()
    assert "CloudflareToken" not in script  # the token is never a parameter
    assert "CF_API_TOKEN=(hidden)" in script.replace("\"$_=(hidden)\"", "CF_API_TOKEN=(hidden)")


def test_powershell_files_are_ascii():
    """Windows PowerShell 5.1 reads a file without a byte order mark as ANSI (and PSScriptAnalyzer warns)."""
    files = [*WINDOWS.rglob("*.ps1"), *WINDOWS.rglob("*.psm1"), REPO / "deploy/node/windows/install.ps1"]
    assert len(files) >= 5
    for path in files:
        assert path.read_bytes().isascii(), path


def powershell_enroll_pattern() -> re.Pattern:
    """The parser's regular expression, from AccWindows.psm1, in Python's dialect (.NET writes (?<name>...))."""
    module = (WINDOWS / "AccWindows.psm1").read_text()
    block = re.search(r"\$script:EnrollPattern = @\((.*?)\) -join ''", module, re.S).group(1)
    pattern = "".join(re.findall(r"'([^']*)'", block))
    assert "(?<=" not in pattern and "(?<!" not in pattern
    return re.compile(pattern.replace("(?<", "(?P<"))


def parse_like_powershell(command: str) -> dict | None:
    """ConvertFrom-AccEnrollCommand, step for step (trim, length, the pattern, the port, normalizing)."""
    text = command.strip()
    if not text or len(text) > 1024:
        return None
    match = powershell_enroll_pattern().match(text)
    if not match or match.end() != len(text):
        return None
    port = match["port"]
    if port and not 1 <= int(port) <= 65535:
        return None
    return {"primary": f"https://{match['host'].lower()}" + (f":{int(port)}" if port else ""),
            "trust": "system" if match["trust"] else "ca", "fingerprint": (match["fingerprint"] or "").lower(),
            "code": match["code"], "name": match["name"]}


def test_enroll_command_cases_hold_for_the_powershell_parser():
    cases = json.loads(ENROLL_CASES.read_text())
    assert len(cases["accepted"]) >= 3 and len(cases["rejected"]) >= 10
    for case in cases["accepted"]:
        parsed = parse_like_powershell(case["command"])
        assert parsed == {key: case[key] for key in ("primary", "trust", "fingerprint", "code", "name")}, case
    for case in cases["rejected"]:
        assert parse_like_powershell(case["command"]) is None, case


def test_enroll_command_parser_matches_what_acc_makes_and_accepts():
    pattern = powershell_enroll_pattern().pattern
    # the name as acc-node checks it (src/acc/node/config.py), the code as the primary makes it (src/acc/server/nodes.py)
    node_name = re.search(r'NODE_NAME = re\.compile\(r"([^"]+)"\)', (REPO / "src/acc/node/config.py").read_text()).group(1)
    assert f"(?P<name>{node_name})$" in pattern
    assert 'code = "-".join(secrets.token_hex(3) for _ in range(4))' in (REPO / "src/acc/server/nodes.py").read_text()
    # the command Add machine shows, filled in, is accepted
    api = (REPO / "src/acc/server/api/nodes.py").read_text()
    assert 'command = f"rynhult-node enroll --primary {primary} {trust} --code {code} --name {body.name}"' in api
    assert 'trust = f"--ca-fingerprint {pin}" if pin else "--trust system"' in api
    for trust in ("--trust system", "--ca-fingerprint " + "ab" * 32):
        command = f"rynhult-node enroll --primary https://acc.example.com {trust} --code 0a1b2c-3d4e5f-a0b1c2-d3e4f5 --name pc"
        assert parse_like_powershell(command), command


def test_the_powershell_self_test_and_ci_use_the_shared_cases():
    self_test = (WINDOWS / "tests/Test-EnrollParser.ps1").read_text()
    assert "tests\\data\\enroll-commands.json" in self_test
    assert "ConvertFrom-AccEnrollCommand" in self_test
    workflow = (REPO / ".github/workflows/installers-windows.yml").read_text()
    assert workflow.count("./deploy/windows/tests/Test-EnrollParser.ps1") == 2  # PowerShell 7 and 5.1
    assert "Invoke-SetupDryRun.ps1" in workflow and "Invoke-ScriptAnalyzer" in workflow


def test_installer_workflow_pins_actions_and_triggers():
    workflow = (REPO / ".github/workflows/installers-windows.yml").read_text()
    uses = re.findall(r"uses: (\S+)", workflow)
    assert uses
    for action in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), action
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    for trigger in ("tags: ['v[1-9]*']", "branches: ['installer/**']", "workflow_dispatch:"):
        assert trigger in workflow, trigger
    assert "choco install innosetup" in workflow and "runs-on: windows-latest" in workflow


@pytest.mark.parametrize("before, expected", [
    ("", "\n[user]\ndefault=alex\n"),
    ("[boot]\nsystemd=true\n", "[boot]\nsystemd=true\n\n[user]\ndefault=alex\n"),
    ("[user]\n", "[user]\ndefault=alex\n"),
    ("[user]\ndefault=alex-agent\n", "[user]\ndefault=alex\n"),
])
def test_wsl_helper_owner_account(tmp_path, stubs, before, expected):
    conf = tmp_path / "wsl.conf"
    conf.write_text(before)
    stub(stubs, "id", 'case "$1" in alex) exit 1 ;; *) /usr/bin/id "$@" ;; esac')
    stub(stubs, "useradd", f'echo "useradd $*" >> {tmp_path}/useradd.log')
    for tool in ("usermod", "passwd", "chage"):
        stub(stubs, tool, f'echo "{tool} $*" >> {tmp_path}/account.log')
    env = {"ACC_WSL_CONF": str(conf), "PATH": f"{stubs}:{os.environ['PATH']}"}
    result = run(["sh", str(NODE / "windows/wsl-node.sh"), "owner", "alex"], **env)
    assert result.returncode == 0, result.stderr
    assert conf.read_text() == expected
    assert (tmp_path / "account.log").read_text().splitlines() == [
        "passwd -l alex", "usermod -aG sudo alex"]
    assert (tmp_path / "useradd.log").read_text() == "useradd --create-home --shell /bin/bash alex\n"
    again = run(["sh", str(NODE / "windows/wsl-node.sh"), "owner", "alex"], **env)
    assert again.returncode == 0 and conf.read_text() == expected


@pytest.mark.parametrize("name", ["root", "", "Alex", "-x", "a b", "x;id"])
def test_wsl_helper_refuses_bad_owner_names(tmp_path, name):
    result = run(["sh", str(NODE / "windows/wsl-node.sh"), "owner", name], ACC_WSL_CONF=str(tmp_path / "wsl.conf"))
    assert result.returncode == 1 and "bad owner name" in result.stderr
    assert not (tmp_path / "wsl.conf").exists()


def test_wsl_helper_main_needs_the_answers(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "ACC_OWNER"}
    result = subprocess.run(["sh", str(NODE / "windows/wsl-node.sh"), "main"], capture_output=True, text=True,
                            env=env, timeout=60)
    assert result.returncode != 0 and "ACC_OWNER is missing" in result.stderr


def test_wsl_helper_uninstall_dry_run_changes_nothing(stubs, tmp_path):
    stub(stubs, "systemctl", f'echo "systemctl $*" >> {tmp_path}/systemctl.log; '
         'case "$1" in list-unit-files) printf "accd.service enabled\\nacc-broker@.socket indirect\\n" ;; '
         'list-units) printf "acc-broker@alex-agent.socket loaded active listening\\n" ;; esac')
    stub(stubs, "rm", f'echo "rm $*" >> {tmp_path}/rm.log')
    result = run(["sh", str(NODE / "windows/wsl-node.sh"), "uninstall", "--dry-run"],
                 PATH=f"{stubs}:{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "(dry run: nothing is changed)" in out
    assert "+ systemctl disable --now accd.service acc-broker@alex-agent.socket" in out
    assert "acc-broker@.socket" not in out.split("+ systemctl disable --now")[1].splitlines()[0]  # templates aren't disabled
    assert "+ systemctl daemon-reload" in out and "kept: /srv/acc/data" in out
    assert 'public-aliases.py --role auto --rollback-aliases' in out
    assert not (tmp_path / "rm.log").exists()  # printed, never run
    logged = (tmp_path / "systemctl.log").read_text()
    assert "disable" not in logged and "daemon-reload" not in logged

@pytest.mark.parametrize("arch, asset", [("x86_64", "Linux_x86_64"), ("aarch64", "Linux_arm64")])
def test_github_mcp_installer_selects_a_pinned_verified_release(tmp_path, arch, asset):
    tools = tmp_path / "tools"
    tools.mkdir()
    stub(tools, "uname", f"echo {arch}")
    result = run(["sh", str(REPO / "deploy/lib/install-github-mcp.sh"), "--dry-run"],
                 PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr
    assert f"v2.0.2/github-mcp-server_{asset}.tar.gz" in result.stdout
    assert "sha256" in result.stdout and "root:root 0755" in result.stdout


def test_github_mcp_checksum_mismatch_installs_nothing(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    marker = tmp_path / "installed"
    stub(tools, "uname", "echo x86_64")
    stub(tools, "curl", 'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then shift; printf corrupt > "$1"; fi; shift; done')
    stub(tools, "install", f'touch "{marker}"')
    result = run(["sh", str(REPO / "deploy/lib/install-github-mcp.sh")],
                 PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode == 1, result.stderr
    assert "checksum mismatch" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("actual", ["BAD", "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"])
def test_apt_key_fingerprint_check(tmp_path, actual):
    tools = tmp_path / "tools"
    tools.mkdir()
    stub(tools, "gpg", f"printf 'pub::::::::::\\nfpr:::::::::{actual}:\\n'")
    result = run(["sh", str(REPO / "deploy/lib/verify-apt-key.sh"), str(tmp_path / "key"),
                  "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"], PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode == (0 if actual != "BAD" else 1), result.stderr

@pytest.mark.parametrize("checksum", ["", "not-a-hash", "0" * 64])
def test_cloudflare_caddy_never_installs_unverified_download(tmp_path, checksum):
    tools = tmp_path / "tools"
    tools.mkdir()
    marker = tmp_path / "installed"
    stub(tools, "curl", 'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then shift; printf corrupt > "$1"; fi; shift; done')
    stub(tools, "install", f'touch "{marker}"')
    result = run(["sh", str(REPO / "deploy/lib/install-caddy-cloudflare.sh"), "amd64"],
                 PATH=f"{tools}:{os.environ['PATH']}", ACC_CADDY_SHA256=checksum)
    assert result.returncode == 1, result.stderr
    assert "checksum" in result.stderr.lower()
    assert not marker.exists()


def test_apt_key_rejects_an_extra_primary_key(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    stub(tools, "gpg", "printf 'pub::::::::::\\nfpr:::::::::9DC858229FC7DD38854AE2D88D81803C0EBFCD88:\\npub::::::::::\\nfpr:::::::::BAD:\\n'")
    result = run(["sh", str(REPO / "deploy/lib/verify-apt-key.sh"), str(tmp_path / "key"),
                  "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"], PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode == 1, result.stderr


def test_github_mcp_clean_install_yields_an_executable_wrapper_target(tmp_path):
    import hashlib
    import io
    import tarfile
    tools = tmp_path / "tools"
    tools.mkdir()
    release = tmp_path / "release.tar.gz"
    payload = b'#!/bin/sh\nprintf "github-mcp:%s" "$*"\n'
    with tarfile.open(release, "w:gz") as archive:
        member = tarfile.TarInfo("github-mcp-server")
        member.mode = 0o755
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    checksum = hashlib.sha256(release.read_bytes()).hexdigest()
    stub(tools, "uname", "echo x86_64")
    stub(tools, "curl", f'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then shift; cp "{release}" "$1"; fi; shift; done')
    stub(tools, "install", 'if [ "$1" = -d ]; then shift 5; exec /usr/bin/install -d "$@"; fi; shift 4; exec /usr/bin/install "$@"')
    destination = tmp_path / "installed"
    script = (REPO / "deploy/lib/install-github-mcp.sh").read_text()
    script = script.replace("815e27d931c796af0c97f24fd211e14f8a4607d4528377719a5ad2352086b0b0", checksum)
    script = script.replace("/srv/acc/tools", str(destination))
    result = run(["sh", "-c", script], PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr
    assert os.access(destination / "github-mcp-server", os.X_OK)
    stub(tools, "gh", "echo EXAMPLE")
    wrapper = (REPO / "bin/acc-mcp-github").read_text().replace("/srv/acc/tools", str(destination))
    wrapped = run(["sh", "-c", wrapper], PATH=f"{tools}:{os.environ['PATH']}")
    assert wrapped.returncode == 0, wrapped.stderr
    assert wrapped.stdout == "github-mcp:stdio --read-only --toolsets context,repos,issues,pull_requests,actions"


@pytest.mark.parametrize("repository", ["nodesource", "docker"])
def test_rejected_apt_key_does_not_replace_an_existing_keyring(tmp_path, repository):
    tools = tmp_path / "tools"
    tools.mkdir()
    keyring = tmp_path / "keyring"
    keyring.write_text("trusted-existing-key")
    stub(tools, "curl", 'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then shift; echo untrusted > "$1"; exit; fi; shift; done; echo untrusted')
    stub(tools, "gpg", 'if [ "$1" = --dearmor ]; then while [ "$1" != -o ]; do shift; done; shift; cat > "$1"; else printf "pub::::::::::\\nfpr:::::::::BAD:\\n"; fi')
    if repository == "nodesource":
        text = (REPO / "deploy/lib/install-node.sh").read_text()
        fragment = text[text.index("install -d -m 0755 /usr/share/keyrings"):text.index('echo "deb [signed-by=')]
        fragment = fragment.replace("/usr/share/keyrings/nodesource.gpg", str(keyring))
        fragment = fragment.replace("/usr/share/keyrings", str(tmp_path))
    else:
        text = (REPO / "deploy/setup.sh").read_text()
        fragment = text[text.index('    # Docker signing key:'):text.index('    echo "deb [arch=')]
        fragment = fragment.replace("/etc/apt/keyrings/docker.asc", str(keyring))
    result = run(["sh", "-eu", "-c", fragment], PATH=f"{tools}:{os.environ['PATH']}", ID="ubuntu",
                 REPO=str(REPO), LIB=str(REPO / "deploy/lib"))
    assert result.returncode != 0
    assert keyring.read_text() == "trusted-existing-key"


CLAUDE_KEY = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE" # gitleaks:allow -- public apt signing-key fingerprint


@pytest.mark.parametrize("actual", ["BAD", CLAUDE_KEY])
def test_claude_apt_repository_needs_the_pinned_key(tmp_path, actual):
    """deploy/lib/claude-apt-repo.sh, shared by setup.sh, the node installer and acc-packages: a key with another
    fingerprint changes nothing under /etc/apt; the pinned one installs the keyring and a sources list signed by it."""
    tools = tmp_path / "tools"
    tools.mkdir()
    stub(tools, "curl", 'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then shift; printf key > "$1"; fi; shift; done')
    stub(tools, "gpg", f"printf 'pub::::::::::\\nfpr:::::::::{actual}:\\n'")
    keyring, sources = tmp_path / "etc/apt/keyrings/claude-code.asc", tmp_path / "etc/apt/sources.list.d/claude-code.list"
    result = run(["sh", str(REPO / "deploy/lib/claude-apt-repo.sh"), str(keyring), str(sources)],
                 PATH=f"{tools}:{os.environ['PATH']}")
    if actual == "BAD":
        assert result.returncode == 1 and "fingerprint mismatch (got BAD); nothing changed" in result.stderr
        assert not keyring.exists() and not sources.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert keyring.read_text() == "key"
        assert sources.read_text() == (f"deb [signed-by={keyring}] https://downloads.claude.ai/claude-code/apt/stable "
                                       "stable main\n")


def test_claude_apt_repository_script_holds_the_one_pinned_fingerprint():
    for path in ("deploy/setup.sh", "deploy/packages/acc-packages", "deploy/node/install.sh"):
        assert CLAUDE_KEY not in (REPO / path).read_text(), path
    assert f"FINGERPRINT={CLAUDE_KEY}" in (REPO / "deploy/lib/claude-apt-repo.sh").read_text()
    assert 'sh "$REPO/deploy/lib/claude-apt-repo.sh"' in (REPO / "deploy/setup.sh").read_text()
    assert 'run sh "$REPO/deploy/lib/claude-apt-repo.sh"' in (NODE / "install.sh").read_text()  # --install-prereqs


def test_linux_refuses_the_main_acc_machine_before_installing_anything():
    """Run on the main Rynhult by mistake, the prerequisites (a newer Node.js) restarted its services before the refusal."""
    script = (NODE / "install.sh").read_text()
    assert script.index("/etc/systemd/system/accd.service") < script.index('echo "== prerequisites"')
    bootstrap = (REPO / "src/acc/server/node_bootstrap.sh").read_text()
    assert bootstrap.index("/etc/systemd/system/accd.service") < bootstrap.index("work=$(mktemp -d)")


def test_linux_app_folder_is_reachable_whatever_the_source_mode(stubs):
    """A source unpacked under a 0700 folder left /opt/acc/app 0700 on windows-test-pc: acc-node got Permission denied."""
    out = run(["bash", str(NODE / "install.sh"), "--dry-run", "--agent", "alex-agent"],
              PATH=f"{stubs}:{os.environ['PATH']}").stdout
    assert out.index("+ chmod 0755 /opt/acc/app") > out.index("+ chown -R -h root:root /opt/acc/app")


def test_setup_redacts_native_failure_output(installed_mac):
    stub(installed_mac["acc"] / "bin", "acc-node",
         'echo "rejected code 3fa21c-0b9d4e-77aa01-c3d2e1" >&2; exit 1')
    result = run(["bash", str(SETUP), "enroll", "--log", SAMPLE], **installed_mac["env"])
    assert result.returncode == 1
    assert "3fa21c" not in result.stderr + installed_mac["log"].read_text()
    assert "[redacted]" in result.stderr


@pytest.mark.parametrize("installer", [NODE / "install.sh", NODE / "macos/install.sh"])
def test_direct_installer_failure_points_to_redacted_log(installer, tmp_path):
    result = run(["bash", str(installer), "--bad-3fa21c-0b9d4e-77aa01-c3d2e1"],
                 HOME=str(tmp_path), TMPDIR=str(tmp_path))
    assert result.returncode != 0
    assert "Full log: " in result.stderr
    log = Path(result.stderr.split("Full log: ")[-1].strip())
    assert log.is_absolute() and log.is_file()
    assert "unknown option" in log.read_text()
    assert "3fa21c" not in result.stdout + result.stderr + log.read_text()
    if installer == NODE / "install.sh":
        shutil.rmtree(log.parent)


def test_setup_failure_excerpt_is_bounded(installed_mac):
    stub(installed_mac["acc"] / "bin", "acc-node",
         'i=0; while [ "$i" -lt 40 ]; do echo "diagnostic-$i"; i=$((i+1)); done; '
         'printf "%10000s\\n" "last diagnostic"; exit 1')
    result = run(["bash", str(SETUP), "enroll", "--log", SAMPLE], **installed_mac["env"])
    assert result.returncode == 1
    assert "diagnostic-39" in result.stderr
    assert "diagnostic-0\n" not in result.stderr
    assert len(result.stderr) < 5000
    assert str(installed_mac["log"]) in result.stderr


@pytest.mark.parametrize("secret", ["https://alex:sample-password@example.com/repo", "Bearer sample-access-token", "token=sample-api-token"])
def test_security_setup_redacts_credentials(installed_mac, secret):
    stub(installed_mac["acc"] / "bin", "acc-node", f"echo '{secret}' >&2; exit 1")
    result = run(["bash", str(SETUP), "enroll", "--log", SAMPLE], **installed_mac["env"])
    assert result.returncode == 1
    output = result.stderr + installed_mac["log"].read_text()
    assert secret not in output
    assert "[redacted]" in output


def test_security_direct_mac_log_does_not_modify_symlink_target(tmp_path):
    victim = tmp_path / "unrelated"
    victim.write_text("preserve me")
    victim.chmod(0o644)
    logs = tmp_path / "Library/Logs/ACC"
    logs.mkdir(parents=True)
    (logs / "install-node.log").symlink_to(victim)
    run(["bash", str(NODE / "macos/install.sh"), "--bad-option"], HOME=str(tmp_path))
    assert victim.read_text() == "preserve me"
    assert victim.stat().st_mode & 0o777 == 0o644


def test_security_setup_log_is_private(setup_tree):
    result = run(["bash", "-c", 'umask 022; exec bash "$1" install --log', "test", str(setup_tree["setup"])], **setup_tree["env"])
    assert result.returncode == 0
    assert setup_tree["log"].stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("secret", ["Authorization: Basic sample-credential", '{"api_key": "sample-key"}', "{'token': 'sample-token'}"])
def test_security_setup_redacts_alternate_credentials(installed_mac, secret):
    # A native process prints the diagnostic as data, never evaluates it.
    import shlex
    stub(installed_mac["acc"] / "bin", "acc-node", f"printf '%s\\n' {shlex.quote(secret)} >&2; exit 1")
    result = run(["bash", str(SETUP), "enroll", "--log", SAMPLE], **installed_mac["env"])
    assert result.returncode == 1
    assert "sample-" not in result.stderr + installed_mac["log"].read_text()


def test_security_setup_sanitizes_retained_log(installed_mac):
    installed_mac["log"].parent.mkdir(parents=True, exist_ok=True)
    installed_mac["log"].write_text("Bearer sample-old-token\n")
    stub(installed_mac["acc"] / "bin", "acc-node", "exit 1")
    run(["bash", str(SETUP), "enroll", "--log", SAMPLE], **installed_mac["env"])
    assert "sample-old-token" not in installed_mac["log"].read_text()


def test_security_direct_mac_sanitizes_retained_log(tmp_path):
    log = tmp_path / "Library/Logs/ACC/install-node.log"
    log.parent.mkdir(parents=True)
    log.write_text("Bearer sample-old-token\n")
    run(["bash", str(NODE / "macos/install.sh"), "--bad-option"], HOME=str(tmp_path))
    assert "sample-old-token" not in log.read_text()


# ------------------------------------------------------------------ updates keep the node's agent accounts


def broker_env(folder: Path, account: str, role: str = "agents", roots: str = "/srv/acc/workspaces") -> None:
    folder.mkdir(exist_ok=True)
    (folder / f"{account}.env").write_text(f"# Written by acc-agent-add.\nACC_BROKER_ROLE={role}\n"
                                           f"ACC_ALLOWED_UIDS=998\nACC_ALLOWED_ROOTS={roots}\n")


def node_dry_run(stubs: Path, env_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return run(["bash", str(NODE / "install.sh"), "--dry-run", *args], PATH=f"{stubs}:{os.environ['PATH']}",
               SUDO_USER="", ACC_INSTALL_TEST_BROKER_ENV=str(env_dir))


def agent_adds(out: str) -> list[str]:
    return [line.split("acc-agent-add ", 1)[1] for line in out.splitlines()
            if line.startswith("+ env ACC_AGENT_CLIENT=acc-node")]


def test_an_update_keeps_the_enrolled_accounts_and_their_folders(stubs, tmp_path):
    """A root update without SUDO_USER must preserve enrolled accounts, not add a default account."""
    env_dir = tmp_path / "broker"
    broker_env(env_dir, "alex-agent", roots="/srv/acc/workspaces:/data/projects")
    broker_env(env_dir, "sam-agent")
    broker_env(env_dir, "acc-svc", role="services")  # not an agents account
    result = node_dry_run(stubs, env_dir, "--update")
    assert agent_adds(result.stdout) == ["alex-agent --roots /srv/acc/workspaces:/data/projects",
                                         "sam-agent --roots /srv/acc/workspaces"], result.stdout + result.stderr
    assert "acc-agent-add acc-agent" not in result.stdout
    assert "agent account(s) alex-agent sam-agent" in result.stdout


def test_a_plain_rerun_also_keeps_the_accounts(stubs, tmp_path):
    env_dir = tmp_path / "broker"
    broker_env(env_dir, "alex-agent")
    assert agent_adds(node_dry_run(stubs, env_dir).stdout) == ["alex-agent --roots /srv/acc/workspaces"]
    # --roots given: the same accounts, with the new folders
    assert agent_adds(node_dry_run(stubs, env_dir, "--roots", "/a:/b").stdout) == ["alex-agent --roots /a:/b"]


def test_an_update_never_creates_an_account(stubs, tmp_path):
    result = node_dry_run(stubs, tmp_path / "empty", "--update")
    assert result.returncode == 1
    assert "an update never creates one" in result.stderr
    assert "acc-agent-add" not in result.stdout


def test_a_first_install_still_creates_the_default_account(stubs, tmp_path):
    assert agent_adds(node_dry_run(stubs, tmp_path / "empty").stdout) == ["acc-agent --roots /srv/acc/workspaces"]
    named = node_dry_run(stubs, tmp_path / "empty", "--agent", "alex-agent")
    assert agent_adds(named.stdout) == ["alex-agent --roots /srv/acc/workspaces"]


def test_the_broker_env_test_knob_only_works_in_a_dry_run():
    script = (NODE / "install.sh").read_text()
    assert '[ -n "$DRY" ] && BROKER_ENV=${ACC_INSTALL_TEST_BROKER_ENV:-$BROKER_ENV}' in script
    assert "BROKER_ENV=/etc/acc/broker\n" in script


def test_the_node_updater_runs_the_installer_in_update_mode(tmp_path):
    from acc.update_helper import NodeTransaction
    commands = []
    job = NodeTransaction(tmp_path / "updates", tmp_path / "app", tmp_path / "data", "0.13.0", {},
                          run=lambda argv, **_: commands.append(argv) or subprocess.CompletedProcess(argv, 0))
    job.install(tmp_path / "new")
    assert commands == [["bash", str(tmp_path / "new/deploy/node/install.sh"), "--update"]]


@pytest.mark.parametrize("args, expected", [
    ([], "WSL isolation is unchanged"),
    (["--no-wsl-isolate"], "WSL isolation is unchanged"),
    (["--wsl-isolate"], "wsl --shutdown"),
    (["--wsl-isolate", "--wsl-disable-automount"], "automount disabled"),
    (["--update", "--wsl-isolate"], "--update never changes WSL isolation"),
])
def test_wsl_isolation_is_opt_in_and_updates_never_change_it(stubs, tmp_path, args, expected):
    folder = tmp_path / "broker"
    broker_env(folder, "alex-agent")
    conf = tmp_path / "wsl.conf"
    conf.write_text("[boot]\nsystemd=true\n")
    result = run(["bash", str(NODE / "install.sh"), "--dry-run", *args],
                 PATH=f"{stubs}:{os.environ['PATH']}", WSL_DISTRO_NAME="Ubuntu-24.04",
                 ACC_INSTALL_TEST_BROKER_ENV=str(folder), ACC_INSTALL_TEST_WSL_CONF=str(conf))
    assert expected in result.stdout, result.stdout + result.stderr
    assert conf.read_text() == "[boot]\nsystemd=true\n"
    assert not list(tmp_path.glob("wsl.conf.rynhult-backup-*"))


def test_update_does_not_even_parse_pending_wsl_configuration(stubs, tmp_path):
    folder = tmp_path / "broker"
    broker_env(folder, "alex-agent")
    conf = tmp_path / "wsl.conf"
    conf.write_text("not a configuration\n")
    result = run(["bash", str(NODE / "install.sh"), "--dry-run", "--update",
                  "--wsl-isolate", "--wsl-disable-automount"],
                 PATH=f"{stubs}:{os.environ['PATH']}", WSL_DISTRO_NAME="Ubuntu-24.04",
                 ACC_INSTALL_TEST_BROKER_ENV=str(folder), ACC_INSTALL_TEST_WSL_CONF=str(conf))
    assert "--update never changes WSL isolation" in result.stdout
    assert "unparsable" not in result.stderr
    assert conf.read_text() == "not a configuration\n"
    assert not list(tmp_path.glob("wsl.conf.rynhult-backup-*"))


@pytest.mark.parametrize("answers, expected", [
    (b"\n", "WSL isolation is unchanged"),
    (b"y\n\n", "automount restricted to root"),
    (b"y\ny\n", "automount disabled"),
])
def test_wsl_interactive_choices_are_explicit_and_default_no(stubs, tmp_path, answers, expected):
    import pty

    folder = tmp_path / "broker"
    broker_env(folder, "alex-agent")
    conf = tmp_path / "wsl.conf"
    master, slave = pty.openpty()
    try:
        with subprocess.Popen(["bash", str(NODE / "install.sh"), "--dry-run"], stdin=slave,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={
                                  **os.environ, "PATH": f"{stubs}:{os.environ['PATH']}",
                                  "WSL_DISTRO_NAME": "Ubuntu-24.04", "ACC_INSTALL_TEST_BROKER_ENV": str(folder),
                                  "ACC_INSTALL_TEST_WSL_CONF": str(conf)}) as process:
            os.write(master, answers)
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == (2 if b"WOULD FAIL" in stdout else 0), stdout + stderr
        assert b"Opt into WSL isolation? [y/N]" in stderr
        assert expected.encode() in stdout
        assert (b"Also disable Windows drive automount completely?" in stderr) is answers.startswith(b"y")
        assert not conf.exists()
    finally:
        os.close(master)
        os.close(slave)

def test_macos_alias_only_dryrun_never_boots_out_jobs(mac):
    app = Path(mac['HOME']) / 'Library/Application Support/ACC'
    (app / 'app/.venv/bin').mkdir(parents=True)
    (app / 'app/.venv/bin/python').symlink_to(sys.executable)
    (app / 'bin').mkdir()
    (app / 'bin/acc-request').write_text('#!/bin/sh\nexit 0\n')
    result = run(['bash', str(NODE / 'macos/install.sh'), '--aliases-only', '--dry-run'], **mac)
    assert result.returncode == 0, result.stderr
    assert 'rynhult-request' in result.stdout
    assert 'launchctl' not in result.stdout
    assert not (app / 'rename-phase-b.json').exists()
    assert not (app / 'bin/rynhult-request').is_symlink()
    assert not (Path(mac['HOME']) / 'Library/Logs').exists()


def test_macos_installer_ignores_planted_modules(mac, tmp_path):
    checkout = tmp_path / 'hostile'
    checkout.mkdir()
    marker = tmp_path / 'imported'
    for module in ('platform', 'json', 'sitecustomize'):
        (checkout / f'{module}.py').write_text(f'open({str(marker)!r}, "w").write("injected")\nraise RuntimeError("planted")\n')
    result = subprocess.run(['bash', str(NODE / 'macos/install.sh'), '--dry-run', '--render-dir', str(tmp_path / 'render')],
                            cwd=checkout, env={**os.environ, **mac, 'PYTHONPATH': str(checkout)},
                            capture_output=True, text=True, timeout=60)
    assert not marker.exists(), result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
