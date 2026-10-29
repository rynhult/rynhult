"""Package-manager detection in the installers (docs/LINUX-DISTROS.md). Dry runs only, on a PATH built for each case:
Debian keeps its exact commands; dnf, zypper and pacman are recognised but not qualified; anything else is refused."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from isolated_installers import dry_run_subject, restore_source_names

REPO = Path(__file__).resolve().parents[1]
HIDDEN = {'apt-get', 'dpkg', 'dnf', 'zypper', 'pacman', 'tmux', 'git', 'node', 'nodejs', 'uv',
          'selinuxenabled', 'semanage', 'restorecon'}


@pytest.fixture
def system(tmp_path, monkeypatch):
    """A bin folder with this machine's tools minus the package managers and the prerequisites, plus `manager`."""
    monkeypatch.setenv('ACC_INSTALL_TEST_BROKER_ENV', str(tmp_path / 'no-broker-settings'))
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for folder in ('/usr/bin', '/bin'):
        for entry in os.scandir(folder):
            if entry.name not in HIDDEN and not (bin_dir / entry.name).exists():
                (bin_dir / entry.name).symlink_to(entry.path)

    def with_manager(manager=None):
        if manager:
            (bin_dir / manager).write_text('#!/bin/sh\nexit 0\n')
            (bin_dir / manager).chmod(0o755)
        return str(bin_dir)
    # Never inherit the host's SELinux state or write through a tool symlink.
    (bin_dir / 'selinuxenabled').write_text('#!/bin/sh\nexit 1\n')
    (bin_dir / 'selinuxenabled').chmod(0o755)
    return with_manager


def run_installer(argv, **kwargs):
    # Dry-run shells still probe files. Execute only copies whose installation probes point into scratch.
    with dry_run_subject(argv, kwargs['env']) as (staged, replacements):
        return restore_source_names(subprocess.run(staged, **kwargs), replacements)


def node_dry_run(path, *extra):
    return run_installer(['bash', str(REPO / 'deploy/node/install.sh'), '--dry-run', '--agent', 'alex-agent',
                           '--install-prereqs', *extra], capture_output=True, text=True, timeout=120,
                          env={**os.environ, 'PATH': path})


def test_debian_keeps_its_exact_commands(system):
    out = node_dry_run(system('apt-get')).stdout
    assert '+ apt-get update -q\n+ env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends tmux git\n' in out
    assert '+ sh ' + str(REPO / 'deploy/lib/install-node.sh') in out
    assert 'qualified' not in out and 'LINUX-DISTROS' not in out


@pytest.mark.parametrize('manager,install,node', [
    ('dnf', '+ dnf install -y -q --setopt=install_weak_deps=False tmux git', 'nodejs npm'),
    ('zypper', '+ zypper --non-interactive --quiet install --no-recommends tmux git', 'nodejs-default npm-default'),
    ('pacman', '+ pacman -S --needed --noconfirm tmux git', 'nodejs npm'),
])
def test_other_managers_are_recognised_but_need_an_explicit_test_install(system, manager, install, node):
    path = system(manager)
    refused = node_dry_run(path)
    assert f'uses {manager}, which Rynhult hasn\'t qualified yet' in refused.stdout
    assert '--unqualified-distro' in refused.stdout and refused.returncode == 2
    allowed = node_dry_run(path, '--unqualified-distro')
    out = allowed.stdout
    assert f"({manager}) isn't qualified yet; installing for a test" in out
    assert install + '\n' in out
    assert f'{install.split(" tmux")[0]} {node}\n' in out  # the distribution's own Node.js, not NodeSource
    assert 'apt-get' not in out and 'install-node.sh' not in out.split('== users and folders')[0]
    assert "Claude Code comes from Anthropic's pinned native release" in out


def test_without_a_known_package_manager_the_node_installer_refuses(system):
    result = node_dry_run(system())
    assert "has no package manager Rynhult knows (apt, dnf, zypper or pacman)" in result.stdout
    assert result.returncode == 2


def test_missing_prerequisites_are_named_in_that_distribution(system):
    result = run_installer(['bash', str(REPO / 'deploy/node/install.sh'), '--dry-run', '--agent', 'alex-agent',
                             '--unqualified-distro'], capture_output=True, text=True, timeout=120,
                            env={**os.environ, 'PATH': system('zypper')})
    assert 'missing tmux git: sudo zypper install tmux git' in result.stdout


@pytest.mark.parametrize('manager', [None, 'dnf', 'zypper', 'pacman'])
def test_the_main_setup_refuses_anything_but_apt(system, manager, tmp_path):
    result = run_installer(['bash', str(REPO / 'deploy/setup.sh'), '--dry-run', '--yes'], capture_output=True,
                            text=True, timeout=120, stdin=subprocess.DEVNULL,
                            env={**os.environ, 'PATH': system(manager), 'ACC_SETUP_TEST_ACCD': str(tmp_path / 'accd')})
    expected = (f'uses {manager}: the main Rynhult supports only Debian and Ubuntu (apt) for now' if manager
                else 'this script supports Debian and Ubuntu (apt)')
    assert expected in result.stdout + result.stderr


@pytest.mark.parametrize('manager,names', [
    ('apt', 'python3 gnupg nodejs npm'), ('dnf', 'python3 gnupg2 nodejs npm'),
    ('zypper', 'python3 gpg2 nodejs-default npm-default'), ('pacman', 'python gnupg nodejs npm')])
def test_package_names_follow_each_distribution(manager, names):
    script = f'. "{REPO}/deploy/lib/pkg.sh"; for n in python3 gnupg nodejs npm; do pkg_name {manager} "$n"; done'
    out = subprocess.run(['sh', '-c', script], capture_output=True, text=True, check=True).stdout
    assert out.split() == names.split()


def test_desktop_rpm_script_passes_shellcheck_and_refuses_without_a_deb(tmp_path):
    script = REPO / 'desktop/package-rpm.sh'
    if shutil.which('shellcheck'):
        assert subprocess.run(['shellcheck', str(script)], capture_output=True, text=True).returncode == 0
    result = subprocess.run(['sh', str(script), str(tmp_path / 'missing.deb')], capture_output=True, text=True)
    assert result.returncode == 1 and 'Build the .deb first' in result.stderr


def test_desktop_rpm_spec_repackages_without_rewriting_the_payload():
    spec = (REPO / 'desktop/rpm/rynhult-desktop.spec').read_text()
    # Built in Fedora 42 and Tumbleweed containers (docs/LINUX-DISTROS.md); here, the guards that matter.
    for line in ('%global __os_install_post %{nil}', '%global __brp_mangle_shebangs %{nil}',
                 '%global __requires_exclude_from ^/usr/lib/[^/]+/desktop/runtime/.*$',
                 'Name:           %{rynhult_name}', '%files -f %{_sourcedir}/files.list'):
        assert line in spec, line
    # GUI libraries come from the binary's own ELF dependencies; setfacl, which desktop setup runs, is declared.
    assert [line for line in spec.splitlines() if line.startswith('Requires:')] == ['Requires:       /usr/bin/setfacl']


@pytest.mark.parametrize('manager', ['apt-get', 'dnf'])
def test_the_architecture_check_understands_the_kernels_names_without_dpkg(system, manager, tmp_path):
    result = run_installer(['bash', str(REPO / 'deploy/setup.sh'), '--dry-run', '--yes'], capture_output=True,
                            text=True, timeout=120, stdin=subprocess.DEVNULL,
                            env={**os.environ, 'PATH': system(manager), 'ACC_SETUP_TEST_ACCD': str(tmp_path / 'accd')})
    assert 'unsupported architecture' not in result.stdout + result.stderr
    assert os.uname().machine not in ('x86_64', 'aarch64') or 'amd64' in result.stdout or 'arm64' in result.stdout


def test_the_main_installer_needs_setfacl_before_it_changes_anything():
    script = (REPO / 'deploy/install.sh').read_text()
    check = script.index('command -v setfacl >/dev/null ||')
    assert check < script.index('echo "== users and directories"') < script.index('acc-agent-share "$AGENT_USER"')


def test_linux_packages_have_no_spaces_in_names_but_keep_the_display_name():
    linux = json.loads((REPO / 'desktop/src-tauri/tauri.package.linux.conf.json').read_text())
    assert ' ' not in linux['productName']
    template = (REPO / 'desktop/src-tauri' / linux['bundle']['linux']['deb']['desktopTemplate']).read_text()
    assert 'Name=Rynhult Linux spike\n' in template and '{{exec}}' in template
    for kind in ('deb', 'rpm'):
        assert linux['bundle']['linux'][kind]['depends'] == ['acl']
    package = (REPO / 'desktop/package.sh').read_text()
    assert '--config tauri.package.conf.json --config tauri.package.linux.conf.json' in package
    # macOS keeps its own product name: the Linux names never reach the Mac build.
    assert 'productName' not in (REPO / 'desktop/src-tauri/tauri.package.conf.json').read_text()


def test_the_build_compiles_every_shipped_module_including_the_runtime():
    package = (REPO / 'desktop/package.sh').read_text()
    assert ('-m compileall -q -f -j 0 --invalidation-mode unchecked-hash "$stage/src" "$stage/desktop"') in package


def test_appimage_builds_only_with_the_pinned_tools():
    package = (REPO / 'desktop/package.sh').read_text()
    assert package.count('sha256sum --quiet --strict -c "$here/appimage/tools.sha256"') == 2  # before and after
    assert json.loads((REPO / 'desktop/src-tauri/tauri.appimage.conf.json').read_text())['bundle']['useLocalToolsDir']
    pins = dict(line.split()[::-1] for line in (REPO / 'desktop/appimage/tools.sha256').read_text().splitlines())
    # The names Tauri's bundler looks for in its tools folder (tauri-bundler 2.10, linuxdeploy 07333c6).
    assert set(pins) == {'AppRun-x86_64', 'linuxdeploy-07333c6-x86_64.AppImage', 'linuxdeploy-plugin-appimage.AppImage'}
    fetch = (REPO / 'desktop/appimage/fetch-tools.sh').read_text()
    assert pins['AppRun-x86_64'] in fetch and pins['linuxdeploy-plugin-appimage.AppImage'] in fetch
    assert 'continuous' not in fetch and 'dd if=/dev/zero bs=1 count=3 seek=8' in fetch
    if shutil.which('shellcheck'):
        assert subprocess.run(['shellcheck', str(REPO / 'desktop/appimage/fetch-tools.sh'),
                               str(REPO / 'desktop/package.sh')], capture_output=True).returncode == 0


def setup_dry_run(path, tmp_path, *extra):
    return subprocess.run(['bash', str(REPO / 'deploy/setup.sh'), '--dry-run', '--yes', *extra], capture_output=True,
                          text=True, timeout=120, stdin=subprocess.DEVNULL,
                          env={**os.environ, 'PATH': path, 'ACC_SETUP_TEST_ACCD': str(tmp_path / 'accd'),
                               'ACC_OWNER': 'nobody', 'ACC_HOSTNAME': 'acc.example.lan',
                               'ACC_INTERNAL_IP': '192.0.2.10'})


@pytest.mark.parametrize('manager', ['dnf', 'zypper', 'pacman'])
def test_the_main_setup_allows_opt_in_on_non_apt_distributions(system, manager, tmp_path):
    out = setup_dry_run(system(manager), tmp_path, '--unqualified-distro').stdout
    assert f"({manager}) isn't qualified yet; installing for a test" in out and 'WOULD FAIL' not in out
    assert 'Podman for ' in out and 'rootless Docker' not in out
    assert "Claude Code from Anthropic's pinned native release (SHA-256 checked)" in out
    refused = setup_dry_run(system(manager), tmp_path).stdout
    assert 'a test install needs --unqualified-distro' in refused


@pytest.mark.parametrize('runner', ['root', None])
def test_the_dry_run_does_not_depend_on_the_runners_account(system, tmp_path, monkeypatch, runner):
    # The coordinator's gates run as root: an owner taken from $USER stopped the dry run before its install plan.
    if runner:
        monkeypatch.setenv('USER', runner)
    else:
        monkeypatch.delenv('USER', raising=False)
    out = setup_dry_run(system('dnf'), tmp_path, '--unqualified-distro')
    assert 'must be a regular user' not in out.stdout + out.stderr and 'Podman for nobody' in out.stdout




@pytest.mark.parametrize('manager,names', [
    ('apt', 'sqlite3 uidmap gh'), ('dnf', 'sqlite shadow-utils gh'), ('zypper', 'sqlite3 shadow gh'),
    ('pacman', 'sqlite shadow github-cli')])
def test_main_installer_package_names(manager, names):
    # Checked against Fedora 43/44 (mdapi) and Tumbleweed's published repository, 2026-10-25.
    script = f'. "{REPO}/deploy/lib/pkg.sh"; for n in sqlite3 uidmap gh; do pkg_name {manager} "$n"; done'
    assert subprocess.run(['sh', '-c', script], capture_output=True, text=True, check=True).stdout.split() == names.split()


def selinux_tools(directory, *, fail_add=False):
    log = directory / 'selinux.log'
    for name, body in (('selinuxenabled', 'exit 0'), ('restorecon', f'echo "restorecon $*" >> {log}'),
                       ('semanage', f'echo "semanage $*" >> {log}; '
                                    + ('[ "$2" = -a ] && exit 1; ' if fail_add else '') + 'exit 0')):
        (directory / name).unlink(missing_ok=True)
        (directory / name).write_text(f'#!/bin/sh\n{body}\n')
        (directory / name).chmod(0o755)
    return log


@pytest.mark.parametrize('role', ['main', 'node'])
def test_selinux_labels_add_persistent_rules_and_restore_them(tmp_path, role):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = selinux_tools(bin_dir)
    plan = subprocess.run(['sh', str(REPO / 'deploy/lib/selinux-labels.sh'), role, '--dry-run'], capture_output=True,
                          text=True, env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}).stdout
    assert "+ semanage fcontext -a -t var_run_t '/run/acc-broker(/.*)?'" in plan
    if role == 'main':
        for rule in ("-t bin_t '/srv/acc/app/\\.venv/bin(/.*)?'", "-t var_lib_t '/srv/acc/data(/.*)?'",
                     "-t httpd_var_run_t '/run/accd\\.sock'"):
            assert f'+ semanage fcontext -a {rule}' in plan
    else:
        assert "+ semanage fcontext -a -t bin_t '/opt/acc/app/\\.venv/bin(/.*)?'" in plan
    assert not log.exists()  # a dry run runs nothing
    # A rerun: the rule exists, so -a fails and -m makes it the same; nothing touches the enforcing mode.
    log = selinux_tools(bin_dir, fail_add=True)
    # Even metadata probes belong in the fixture, not the installation roots.
    subject = tmp_path / 'selinux-labels.sh'
    source = (REPO / 'deploy/lib/selinux-labels.sh').read_text()
    for root in ('/srv/acc', '/opt/acc', '/var/lib/acc-node', '/run/acc'):
        source = source.replace(root, str(tmp_path / root.lstrip('/')))
    subject.write_text(source)
    result = subprocess.run(['sh', str(subject), role], capture_output=True, text=True,
                            env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'})
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert f'semanage fcontext -m -t var_run_t {tmp_path}/run/acc-broker(/.*)?' in calls


def test_nothing_ever_switches_selinux_off():
    """No installer or helper runs setenforce or makes a domain permissive (comments may say not to)."""
    for path in (REPO / 'deploy').rglob('*'):
        if not path.is_file() or path.suffix in ('.png', '.gz', '.md'):
            continue
        for line in path.read_text(errors='ignore').splitlines():
            code = line.split('#', 1)[0]
            assert 'setenforce' not in code and 'permissive -a' not in code, (path, line)


def test_the_node_installer_labels_paths_where_selinux_is_on(system, tmp_path):
    path = system('dnf')
    log = selinux_tools(Path(path))
    (Path(path) / 'semanage').unlink()
    out = node_dry_run(path, '--unqualified-distro').stdout
    assert 'policycoreutils-python-utils' in out.split('== users and folders')[0]
    assert f'+ sh {REPO}/deploy/lib/selinux-labels.sh node' in out
    assert not log.exists()


def test_caddy_runs_the_same_way_under_fedora_and_opensuse_units(tmp_path):
    """Their units differ from Debian's: Fedora validates with /usr/bin/caddy first, openSUSE starts with --resume
    (both read from the packages, 2026-10-25). Off Debian, install.sh pins the start command in one more drop-in."""
    text = (REPO / 'deploy/install.sh').read_text()
    block = text[text.index('if [ "${PKG:-apt}" != apt ]; then\n    # Fedora'):]
    block = block[:block.index('\n  fi\n') + 5]
    script = tmp_path / 'render.sh'
    script.write_text('set -eu\nPKG=dnf CADDY=/usr/local/bin/caddy CADDY_UNIT_CHANGED=""\n'
                      'install() { cp "$3" "$OUT"; }\n' + block.replace('/etc/systemd/system/caddy.service.d/distro.conf', '"$OUT"'))
    out = tmp_path / 'distro.conf'
    subprocess.run(['bash', str(script)], check=True, env={**os.environ, 'OUT': str(out)})
    assert out.read_text() == ('[Service]\nExecStartPre=\nExecStart=\n'
                               'ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile\n')
    assert 'if [ "${PKG:-apt}" != apt ]' in block  # Debian's units and drop-ins are unchanged


def test_arch_pkgbuild_preserves_payload_and_does_not_strip_runtime(tmp_path):
    import hashlib
    import tarfile
    recipe = REPO / 'desktop/arch/PKGBUILD'
    assert recipe.exists(), 'Arch recipe missing'
    payload = tmp_path / 'payload'
    (payload / 'usr/lib/rynhult-linux-spike/desktop/runtime').mkdir(parents=True)
    binary = payload / 'usr/lib/rynhult-linux-spike/desktop/runtime/python'
    binary.write_bytes(b'not-an-ELF-but-preserve-every-byte')
    binary.chmod(0o755)
    with tarfile.open(tmp_path / 'rynhult-desktop.tar.gz', 'w:gz') as archive:
        archive.add(payload / 'usr', arcname='usr')
    digest = hashlib.sha256((tmp_path / 'rynhult-desktop.tar.gz').read_bytes()).hexdigest()
    rendered = recipe.read_text().replace('@VERSION@', '0.0.0').replace('@SHA256@', digest)
    script = tmp_path / 'PKGBUILD'
    script.write_text(rendered)
    destination = tmp_path / 'package'
    destination.mkdir()
    subprocess.run(['tar', '-xf', str(tmp_path / 'rynhult-desktop.tar.gz'), '-C', str(tmp_path)], check=True)
    result = subprocess.run(['bash', '-c', 'source "$1"; package; [[ " ${options[*]} " == *" !strip "* ]]',
                             'test', str(script)], env={**os.environ, 'srcdir': str(tmp_path),
                                                       'pkgdir': str(destination)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    installed = destination / 'usr/lib/rynhult-linux-spike/desktop/runtime/python'
    assert installed.read_bytes() == binary.read_bytes()
    assert installed.stat().st_mode & 0o111


@pytest.mark.parametrize("output_name", ["build", "Alex's build"])
def test_arch_preparation_hashes_payload_and_refuses_existing_output(tmp_path, output_name):
    import hashlib
    import tarfile
    control = tmp_path / 'control'
    control.mkdir()
    (control / 'control').write_text('Package: rynhult-linux-spike\nVersion: 0.0.0\nArchitecture: amd64\n')
    payload = tmp_path / 'root'
    (payload / 'usr/bin').mkdir(parents=True)
    (payload / 'usr/bin/rynhult-linux-spike').write_text('payload')
    for name, folder in [('control', control), ('data', payload)]:
        with tarfile.open(tmp_path / f'{name}.tar.gz', 'w:gz') as archive:
            for entry in folder.iterdir():
                archive.add(entry, arcname=entry.name)
    deb = tmp_path / 'desktop.deb'
    subprocess.run(['ar', 'rc', str(deb), str(tmp_path / 'control.tar.gz'), str(tmp_path / 'data.tar.gz')], check=True)
    output = tmp_path / output_name
    command = ['bash', str(REPO / 'desktop/package-arch.sh'), str(deb), str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    digest = hashlib.sha256((output / 'rynhult-desktop.tar.gz').read_bytes()).hexdigest()
    assert digest in (output / 'PKGBUILD').read_text() and digest in result.stdout
    # Execute the printed follow-up verbatim with a fake, unprivileged build tool.
    tools = tmp_path / 'tools'
    tools.mkdir()
    makepkg = tools / 'makepkg'
    makepkg.write_text('#!/bin/bash\nprintf "%s\\n" "$PWD" "$@"\n')
    makepkg.chmod(0o755)
    hint = result.stdout.splitlines()[-1].split(': ', 1)[1]
    copied = subprocess.run(['bash', '-c', hint], capture_output=True, text=True,
                            env={**os.environ, 'PATH': f'{tools}:{os.environ["PATH"]}'})
    assert copied.returncode == 0, copied.stderr
    assert copied.stdout.splitlines() == [str(output), '--cleanbuild']
    again = subprocess.run(command, capture_output=True, text=True)
    assert again.returncode == 2
    assert hashlib.sha256((output / 'rynhult-desktop.tar.gz').read_bytes()).hexdigest() == digest


@pytest.mark.skipif(os.geteuid() == 0, reason='root can read a 0600 file')
def test_the_setup_dry_run_says_it_cannot_read_an_installed_machines_settings(system, tmp_path):
    """On an installed machine /etc/default/accd is root's (0600): say so, instead of 'ACC_OWNER must be a regular
    user (not root)' after empty reads (rc package check)."""
    accd = tmp_path / 'accd'
    accd.write_text('ACC_SERVICES_ACCOUNT=alex\n')
    accd.chmod(0)
    result = setup_dry_run(system(), tmp_path)  # this host's own apt
    assert result.returncode != 0
    assert "can't read" in result.stdout + result.stderr and 'sudo' in result.stdout + result.stderr
    assert 'ACC_OWNER must be a regular user' not in result.stdout + result.stderr


@pytest.mark.parametrize('manager', ['dnf', 'zypper'])
@pytest.mark.parametrize('missing,package', [('semanage', 'policycoreutils-python-utils'),
                                             ('restorecon', 'policycoreutils')])
def test_node_selinux_tools_are_independent_prerequisites(system, monkeypatch, manager, missing, package):
    # All ordinary prerequisites already exist. SELinux tools must not depend on
    # installing tmux/git/uv first (nor on both SELinux tools being absent).
    path = system(manager)
    tools = Path(path)
    for name in ('tmux', 'git', 'uv', 'node', 'selinuxenabled', 'semanage', 'restorecon'):
        target = tools / name
        target.unlink(missing_ok=True)
        if name != missing:
            target.write_text('#!/bin/sh\nexit 0\n')
            target.chmod(0o755)
    monkeypatch.setenv('ACC_INSTALL_TEST_OWNER', str(os.getuid()))
    result = node_dry_run(path, '--unqualified-distro')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ok: uv at ' in result.stdout
    assert package in result.stdout
    assert result.stdout.index(package) < result.stdout.index('== users and folders')
    # Without permission to install dependencies, refusal names the dependency.
    refused = run_installer(['bash', str(REPO / 'deploy/node/install.sh'), '--dry-run',
                             '--agent', 'alex-agent', '--unqualified-distro'],
                            capture_output=True, text=True, timeout=120,
                            env={**os.environ, 'PATH': path})
    assert refused.returncode == 2
    assert f'missing {package}:' in refused.stdout


def test_node_restores_selinux_labels_after_installing_executables_before_starting_brokers(system):
    path = system('dnf')
    log = selinux_tools(Path(path))
    result = node_dry_run(path, '--unqualified-distro')
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    labels = out.index('deploy/lib/selinux-labels.sh node')
    assert out.index('uv sync --frozen --no-dev') < labels
    assert out.index('bin/acc-hook') < labels
    assert labels < out.index('ACC_AGENT_CLIENT=acc-node')
    assert not log.exists()  # installers only plan; no host policy is applied


@pytest.mark.parametrize('manager', ['apt-get', 'dnf', 'zypper', 'pacman'])
def test_node_distro_plans_keep_node_payload_and_accounts_on_update(system, tmp_path, monkeypatch, manager):
    path = system(manager)
    broker = tmp_path / 'accounts'
    broker.mkdir()
    (broker / 'alex-agent.env').write_text('ACC_BROKER_ROLE=agents\nACC_ALLOWED_ROOTS=/srv/acc/workspaces/alex\n')
    (broker / 'morgan-agent.env').write_text('ACC_BROKER_ROLE=agents\nACC_ALLOWED_ROOTS=/srv/acc/workspaces/morgan\n')
    monkeypatch.setenv('ACC_INSTALL_TEST_BROKER_ENV', str(broker))
    command = ['bash', str(REPO / 'deploy/node/install.sh'), '--dry-run', '--install-prereqs', '--update']
    if manager != 'apt-get':
        command.append('--unqualified-distro')
    env = {**os.environ, 'PATH': path}
    first = run_installer(command, capture_output=True, text=True, timeout=120, env=env)
    second = run_installer(command, capture_output=True, text=True, timeout=120, env=env)
    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    assert first.stdout == second.stdout
    for account in ('alex-agent', 'morgan-agent'):
        assert f'/usr/local/sbin/acc-agent-add {account} --roots /srv/acc/workspaces/' in first.stdout
    for node_path in ('/opt/acc/app', '/usr/local/sbin/acc-packages', 'acc-packages.service.d/node.conf',
                      '/etc/systemd/system/acc-node.service'):
        assert node_path in first.stdout
    for desktop_payload in ('webkit', 'gtk3', 'AppImage', 'rpmbuild', 'makepkg', 'desktop/setup.py'):
        assert desktop_payload not in first.stdout
    assert 'acc-agent-add acc-agent ' not in first.stdout
    assert 'caddy' not in first.stdout.lower()


def test_node_labels_writable_service_directories_before_activation(system):
    path = system('dnf')
    selinux_tools(Path(path))
    result = node_dry_run(path, '--unqualified-distro')
    assert result.returncode == 0, result.stderr
    out = result.stdout
    labels = out.index('deploy/lib/selinux-labels.sh node')
    for command in (
        'install -d -o root -g acc-node -m 0751 /srv/acc/run',
        'install -d -o root -g acc-node -m 0750 /srv/acc/updates /srv/acc/updates/results',
        'install -d -o acc-node -g acc-node -m 0750 /srv/acc/updates/requests',
        'install -d -o root -g acc-node -m 0750 /srv/acc/packages /srv/acc/packages/results',
        'install -d -o acc-node -g acc-node -m 0750 /srv/acc/packages/requests',
    ):
        assert command in out
        assert out.index(command) < labels
    for activation in ('ACC_AGENT_CLIENT=acc-node', 'enable --now acc-update.path',
                       'enable --now acc-packages.path'):
        assert labels < out.index(activation)
    plan = subprocess.run(['sh', str(REPO / 'deploy/lib/selinux-labels.sh'), 'node', '--dry-run'],
                          capture_output=True, text=True,
                          env={**os.environ, 'PATH': path}, check=True).stdout
    for directory in ('updates', 'packages'):
        assert f"-t var_lib_t '/srv/acc/{directory}(/.*)?'" in plan
        assert f'restorecon -R /srv/acc/{directory}' in plan


def test_node_refuses_an_indeterminate_selinux_probe(system):
    path = system('dnf')
    selinux_tools(Path(path))
    (Path(path) / 'selinuxenabled').write_text('#!/bin/sh\nexit 2\n')
    result = node_dry_run(path, '--unqualified-distro')
    assert result.returncode == 2
    assert 'cannot determine whether SELinux is enabled' in result.stdout + result.stderr
    # Dry runs collect prerequisite failures and still print the inert plan.
    assert result.stdout.index('WOULD FAIL: cannot determine') < result.stdout.index('== users and folders')


@pytest.mark.parametrize('failing_tool', ['semanage', 'restorecon'])
def test_selinux_label_failure_stops_the_helper(tmp_path, failing_tool):
    tools = tmp_path / 'bin'
    tools.mkdir()
    selinux_tools(tools)
    (tools / failing_tool).write_text('#!/bin/sh\nexit 42\n')
    subject = tmp_path / 'labels.sh'
    source = (REPO / 'deploy/lib/selinux-labels.sh').read_text()
    for root in ('/srv/acc', '/opt/acc', '/var/lib/acc-node', '/run/acc'):
        source = source.replace(root, str(tmp_path / root.lstrip('/')))
    subject.write_text(source)
    (tmp_path / 'opt/acc/bin').mkdir(parents=True)
    result = subprocess.run(['sh', str(subject), 'node'], capture_output=True, text=True,
                            env={**os.environ, 'PATH': f'{tools}:{os.environ["PATH"]}'})
    assert result.returncode == 42, result.stdout + result.stderr
