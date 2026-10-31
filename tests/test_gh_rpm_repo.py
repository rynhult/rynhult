"""RPM repository trust setup executes only in a disposable filesystem with fake downloads."""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REPOS = {
    'caddy': ('caddy-rpm-repo.sh', '4A76F92FF6D40440F8FC4F36C52191B1D605147E',
              'rynhult-caddy.repo', 'RYNHULT-CADDY.asc',
              'https://download.copr.fedorainfracloud.org/results/@caddy/caddy/epel-$releasever_major-$basearch/', 'caddy'),
    'gh': ('gh-rpm-repo.sh', '7F38BBB59D064DBCB3D84D725612B36462313325',
           'gh-cli.repo', 'RYNHULT-GITHUB-CLI.asc', 'https://cli.github.com/packages/rpm', 'gh'),
    'compose': ('docker-compose-rpm-repo.sh', '060A61C51B558A7F742B77AAC52FEB6B621E9F35',
                'rynhult-docker-compose.repo', 'RYNHULT-DOCKER.asc',
                'https://download.docker.com/linux/rhel/$releasever_major/$basearch/stable', 'docker-compose-plugin'),
}


@pytest.fixture(params=REPOS)
def rpm_repo(tmp_path, signing_keys, request):
    filename, pin, repo_name, key_name, endpoint, package = REPOS[request.param]
    trusted, rogue = signing_keys(), signing_keys()
    root = tmp_path / 'root'
    root.mkdir()
    key = tmp_path / 'download.asc'
    key.write_bytes(trusted.public() + rogue.public())
    tools = tmp_path / 'bin'
    tools.mkdir()
    curl = tools / 'curl'
    curl.write_text('#!/bin/sh\nprintf used-fixture > "$TEST_DOWNLOAD_TRACE"\n[ "${TEST_NETWORK_FAIL:-}" != 1 ] || exit 22\nwhile [ "$#" -gt 0 ]; do\n if [ "$1" = -o ]; then cp "$TEST_KEY" "$2"; exit; fi\n shift\ndone\nexit 2\n')
    curl.chmod(0o755)
    gpg = tools / 'gpg-trace'
    gpg.write_text('''#!/bin/sh
previous=''
for argument do
    if [ "$previous" = --homedir ]; then
        printf '%s\n' "$argument" >> "$TEST_GPG_TRACE"
        [ "$(stat -c %a "$argument")" = 700 ] || exit 91
    fi
    previous=$argument
done
exec /usr/bin/gpg "$@"
''')
    gpg.chmod(0o755)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    gnupg = home / '.gnupg'
    gnupg.mkdir(mode=0o700)

    def run(**overrides):
        source = REPO / 'deploy/lib' / filename
        # Test only a staged copy: no environment-controlled destination or trust override in production.
        script = tmp_path / 'repo.sh'
        if source.exists():
            script.write_text(source.read_text().replace('/etc/', str(root / 'etc') + '/').replace(pin, trusted.fingerprint)
                              .replace("install -o 0 -g 0", f"install -o {os.getuid()} -g {os.getgid()}")
                              .replace('/usr/bin/gpg', f'"{gpg}"').replace("curl -fsSL", f'"{curl}" -fsSL').replace('>/dev/null 2>&1 ||', '>/dev/null ||'))
        result = subprocess.run(['sh', str(script)], capture_output=True, text=True, timeout=40,
                              env={'HOME': str(home), 'GNUPGHOME': str(gnupg), 'LC_ALL': 'C',
                                   'PATH': str(tools) + ':/usr/bin:/bin',
                                   'TMPDIR': str(scratch), 'TEST_KEY': str(key),
                                   'TEST_DOWNLOAD_TRACE': str(tmp_path / 'download-trace'),
                                   'TEST_GPG_TRACE': str(tmp_path / 'gpg-trace'), **overrides})
        assert (tmp_path / 'download-trace').exists(), result.stderr
        assert (tmp_path / 'download-trace').read_text() == 'used-fixture'
        assert not list(home.rglob('S.gpg-agent*')), result.stderr
        trace = tmp_path / 'gpg-trace'
        if trace.exists():
            assert all(not Path(directory).exists() for directory in trace.read_text().splitlines()), result.stderr
        return result
    return run, root, key, trusted, rogue, scratch, repo_name, key_name, endpoint, package


def test_installs_only_pinned_key_and_scoped_signed_repo(rpm_repo, tmp_path):
    run, root, _, trusted, rogue, scratch, repo_name, key_name, endpoint, package = rpm_repo
    result = run()
    assert result.returncode == 0, result.stderr
    repo = root / 'etc/yum.repos.d' / repo_name
    body = repo.read_text()
    assert 'baseurl=' + endpoint in body
    assert 'gpgcheck=1' in body and 'includepkgs=' + package + '\n' in body
    assert ('repo_gpgcheck=0' if package == 'caddy' else 'repo_gpgcheck=1') in body
    assert 'gpgkey=file://' + str(root / 'etc/pki/rpm-gpg' / key_name) in body
    key = root / 'etc/pki/rpm-gpg' / key_name
    # Real GnuPG checks the installed bytes, without consulting the user's keyring.
    home = tmp_path / 'inspect'
    home.mkdir(mode=0o700)
    listing = subprocess.run(['/usr/bin/gpg', '--no-options', '--homedir', str(home), '--with-colons', '--show-keys', str(key)],
                             capture_output=True, text=True, check=True).stdout
    assert trusted.fingerprint in listing and rogue.fingerprint not in listing
    for path in (key, repo):
        assert path.stat().st_mode & 0o777 == 0o644
        assert path.stat().st_uid == os.getuid()
        assert path.stat().st_gid == os.getgid()
    before = {p: p.read_bytes() for p in (key, repo)}
    assert run().returncode == 0
    assert all(p.read_bytes() == value for p, value in before.items())
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize('failure', ['wrong-key', 'garbled', 'network'])
def test_bad_download_never_changes_existing_repository(rpm_repo, failure):
    run, root, key, _, rogue, scratch, repo_name, key_name, _, _ = rpm_repo
    if failure == 'wrong-key':
        key.write_bytes(rogue.public())
    elif failure == 'garbled':
        key.write_bytes(b'not a key')
    # Faults are passed explicitly, never inherited from the pytest worker.
    repo = root / 'etc/yum.repos.d' / repo_name
    repo.parent.mkdir(parents=True)
    repo.write_text('existing reviewed repo\n')
    result = run(TEST_NETWORK_FAIL='1' if failure == 'network' else '0')
    assert result.returncode == (22 if failure == 'network' else 1), result.stderr
    if failure != 'network':
        expected = 'fingerprint mismatch' if failure == 'wrong-key' else 'key download'
        assert expected in result.stderr, result.stderr
    assert repo.read_text() == 'existing reviewed repo\n'
    assert not (root / 'etc/pki').exists()
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize('target', ['key', 'repo'])
@pytest.mark.parametrize('failure', ['install', 'rename', 'flush', 'signal'])
def test_interrupted_publication_preserves_previous_file(rpm_repo, tmp_path, target, failure):
    run, root, _, _, _, scratch, repo_name, key_name, _, _ = rpm_repo
    key = root / 'etc/pki/rpm-gpg' / key_name
    repo = root / 'etc/yum.repos.d' / repo_name
    for path in (key, repo):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'previous complete file\n')
    destination = key if target == 'key' else repo
    tools = tmp_path / 'bin'
    # Only the external filesystem operation is replaced. The actual helper must
    # preserve the destination even if staging writes partial bytes then fails.
    tool = tools / ('install' if failure in ('install', 'signal') else 'sync' if failure == 'flush' else 'mv')
    real = '/usr/bin/' + tool.name
    tool.write_text('''#!/bin/sh
for last do :; done
case "$last" in
  "$FAIL_DEST"|"$FAIL_DEST".tmp.*)
    echo "injected-$FAILURE:$FAIL_DEST" >&2
    if [ "$FAILURE" = rename ] || [ "$FAILURE" = flush ]; then exit 28; fi
    printf partial > "$last"
    if [ "$FAILURE" = signal ]; then kill -TERM "$PPID"; fi
    exit 28 ;;
esac
exec "''' + real + '''" "$@"
''')
    tool.chmod(0o755)
    result = run(FAIL_DEST=str(destination), FAILURE=failure)
    assert f'injected-{failure}:{destination}' in result.stderr, result.stderr
    assert result.returncode == (1 if failure == 'signal' else 28), result.stderr
    assert destination.read_bytes() == b'previous complete file\n'
    if target == 'key':
        assert repo.read_bytes() == b'previous complete file\n'
    else:
        assert key.read_bytes() != b'previous complete file\n', result.stderr  # key publishes first
    assert not list(root.rglob('*.tmp.*'))
    assert list(scratch.iterdir()) == []


def test_helper_ignores_inherited_shell_and_tool_environment(rpm_repo, tmp_path, monkeypatch):
    run, *_ = rpm_repo
    poison = tmp_path / 'poison'
    poison.mkdir()
    for name in ('cp', 'install', 'mktemp'):
        tool = poison / name
        tool.write_text('#!/bin/sh\necho inherited-tool >&2\nexit 97\n')
        tool.chmod(0o755)
    monkeypatch.setenv('PATH', str(poison) + ':' + os.environ['PATH'])
    monkeypatch.setenv('HOME', str(poison))
    monkeypatch.setenv('GNUPGHOME', str(poison / 'missing-keyring'))
    monkeypatch.setenv('TEST_NETWORK_FAIL', '1')
    result = run()
    assert result.returncode == 0, result.stderr


def test_long_scratch_uses_short_private_gpg_home(rpm_repo, tmp_path):
    run, _, _, _, _, scratch, *_ = rpm_repo
    long_scratch = scratch / ('deep-staging-' * 15)
    long_scratch.mkdir()
    result = run(TMPDIR=str(long_scratch))
    assert result.returncode == 0, result.stderr
    homes = (tmp_path / 'gpg-trace').read_text().splitlines()
    assert homes
    for home in homes:
        assert len(os.fsencode(home + '/S.gpg-agent')) < 100, home
        assert not Path(home).exists(), home
    assert list(long_scratch.iterdir()) == []


@pytest.mark.parametrize('helper', ['verify', 'install-asc', 'install-gpg', 'claude'])
def test_apt_helpers_use_short_private_gpg_home(tmp_path, signing_keys, helper):
    trusted = signing_keys()
    key = tmp_path / 'public.asc'
    key.write_bytes(trusted.public())
    tools = tmp_path / 'bin'
    tools.mkdir()
    curl = tools / 'curl'
    curl.write_text('#!/bin/sh\nfor last do :; done\ncp "$TEST_KEY" "$last"\n')
    curl.chmod(0o755)
    gpg = tools / 'gpg'
    gpg.write_text('''#!/bin/sh
previous=''
for argument do
    if [ "$previous" = --homedir ]; then
        printf '%s\n' "$argument" >> "$TEST_GPG_TRACE"
        [ "$(stat -c %a "$argument")" = 700 ] || exit 91
    fi
    previous=$argument
done
exec /usr/bin/gpg "$@"
''')
    gpg.chmod(0o755)
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    for name in ('verify-apt-key.sh', 'install-apt-key.sh', 'claude-apt-repo.sh'):
        body = (REPO / 'deploy/lib' / name).read_text()
        body = body.replace('31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE', trusted.fingerprint)
        (scripts / name).write_text(body)
    scratch = tmp_path / ('deep-staging-' * 15)
    scratch.mkdir()
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    destination = tmp_path / ('installed.gpg' if helper == 'install-gpg' else 'installed.asc')
    if helper == 'verify':
        command = ['sh', str(scripts / 'verify-apt-key.sh'), str(key), trusted.fingerprint]
    elif helper == 'claude':
        command = ['sh', str(scripts / 'claude-apt-repo.sh'), str(destination), str(tmp_path / 'sources.list')]
    else:
        command = ['sh', str(scripts / 'install-apt-key.sh'), 'https://keys.example.com/key.asc',
                   str(destination), trusted.fingerprint]
    trace = tmp_path / 'gpg-trace'
    result = subprocess.run(command, capture_output=True, text=True, timeout=40,
                            env={'HOME': str(home), 'GNUPGHOME': str(home / '.gnupg'),
                                 'TMPDIR': str(scratch), 'PATH': str(tools) + ':/usr/bin:/bin',
                                 'TEST_KEY': str(key), 'TEST_GPG_TRACE': str(trace), 'LC_ALL': 'C'})
    assert result.returncode == 0, result.stderr
    assert trace.exists(), 'GnuPG must use an explicit private home'
    for directory in trace.read_text().splitlines():
        assert len(os.fsencode(directory + '/S.gpg-agent')) < 100, directory
        assert not Path(directory).exists(), directory
    assert list(scratch.iterdir()) == []
    assert not list(home.rglob('S.gpg-agent*'))
    if helper != 'verify':
        assert destination.stat().st_size > 0
        if helper != 'install-gpg':
            assert destination.read_bytes() == key.read_bytes()
