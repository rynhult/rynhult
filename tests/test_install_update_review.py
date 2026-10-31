"""Install and update review (docs/security/2026-10-27-install-update-review.md): one test group per fixed finding."""
import sqlite3
import stat
from contextlib import closing

from alembic import command

from acc.server.db import alembic_config, migrate


# IU-1: an upgrade by hand migrated the schema at accd's next start with no copy of the database taken first.
def test_iu_1_migrating_an_older_database_keeps_a_private_copy_first(tmp_path):
    database = tmp_path / 'acc.db'
    url = f'sqlite:///{database}'
    command.upgrade(alembic_config(url), '0066')
    with closing(sqlite3.connect(database)) as db:
        db.execute("INSERT INTO setting (key, value, updated_at) VALUES ('sr-marker', '1', '2026-10-27')")
        db.commit()
    migrate(url)
    copy = tmp_path / 'acc.db.pre-migration-0066'
    assert copy.is_file() and stat.S_IMODE(copy.stat().st_mode) == 0o600
    with closing(sqlite3.connect(copy)) as db:
        assert db.execute('select version_num from alembic_version').fetchall() == [('0066',)]
        assert db.execute("select value from setting where key = 'sr-marker'").fetchall() == [(1,)]
    with closing(sqlite3.connect(database)) as db:
        assert db.execute('select version_num from alembic_version').fetchone()[0] != '0066'
    assert not list(tmp_path.glob('.acc.db.pre-migration-*.tmp'))


def test_iu_1_a_current_or_new_database_gets_no_copy(tmp_path):
    database = tmp_path / 'acc.db'
    migrate(f'sqlite:///{database}')  # new
    migrate(f'sqlite:///{database}')  # already current
    assert not list(tmp_path.glob('acc.db.pre-migration-*'))


def test_iu_1_a_retried_migration_keeps_the_first_copy(tmp_path):
    from acc.server.db import pre_migration_copy
    database = tmp_path / 'acc.db'
    command.upgrade(alembic_config(f'sqlite:///{database}'), '0066')
    first = pre_migration_copy(database, '0066')
    before = first.read_bytes()
    with closing(sqlite3.connect(database)) as db:
        db.execute("INSERT INTO setting (key, value, updated_at) VALUES ('later', '1', '2026-10-27')")
        db.commit()
    assert pre_migration_copy(database, '0066') == first and first.read_bytes() == before


# IU-2: install.sh copied Caddy's root as root, following any link in Caddy's writable home, and /api/nodes/ca
# served the copy's raw text to anyone.
import datetime  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def root_pem():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, 'Test Root')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=3650))
            .sign(key, hashes.SHA256()))
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return cert.public_bytes(serialization.Encoding.PEM).decode(), private


def single_cert(text):
    return subprocess.run([sys.executable, '-I', str(REPO / 'deploy/lib/single-cert.py')], input=text,
                          capture_output=True, text=True)


def test_iu_2_only_one_certificate_passes_the_copy_filter():
    cert, private = root_pem()
    assert single_cert(cert).stdout == cert
    for text in ['root:$6$hash:19000:0:99999:7:::\n', cert + cert, private + cert, cert + private, '']:
        result = single_cert(text)
        assert result.returncode == 1 and result.stdout == ''


def certificate_section(tmp_path, planted):
    """The installer's private-certificate section, with root's users stubbed: runuser runs as this account."""
    tools = tmp_path / 'tools'
    tools.mkdir()
    log = tmp_path / 'runuser.log'
    (tools / 'runuser').write_text(f'#!/bin/sh\necho "$@" >> {log}\nshift 3\nexec "$@"\n')
    (tools / 'systemctl').write_text('#!/bin/sh\nexit 0\n')
    for tool in ('runuser', 'systemctl'):
        (tools / tool).chmod(0o755)
    caddy_root, node_ca, default = tmp_path / 'caddy/root.crt', tmp_path / 'data/node-ca.pem', tmp_path / 'accd'
    caddy_root.parent.mkdir()
    node_ca.parent.mkdir()
    planted(caddy_root)
    script = (REPO / 'deploy/install.sh').read_text()
    script = script[script.index('echo "== private certificate:'):script.index('echo "== done:')]
    script = (script.replace('/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt', str(caddy_root))
              .replace('/srv/acc/data/node-ca.pem', str(node_ca)).replace('/etc/default/accd', str(default)))
    result = subprocess.run(['bash', '-euo', 'pipefail', '-c', 'setting() { :; }; CADDY_SIGNS=1; ' + script],
                            capture_output=True, text=True, timeout=60,
                            env={**os.environ, 'REPO': str(REPO), 'PATH': f"{tools}:{os.environ['PATH']}"})
    return result, node_ca, log.read_text() if log.exists() else ''


def test_iu_2_a_link_to_another_file_is_not_copied(tmp_path):
    secret = tmp_path / 'root-only'
    secret.write_text('root:$6$hash:19000:0:99999:7:::\n')
    result, node_ca, log = certificate_section(tmp_path, lambda path: path.symlink_to(secret))
    assert result.returncode == 0, result.stderr
    assert not node_ca.exists() and 'single certificate' in result.stderr
    assert log.startswith('-u caddy -- cat -- ')  # read as caddy, never opened by root's shell


def test_iu_2_caddys_root_is_copied_re_encoded(tmp_path):
    cert, _ = root_pem()
    result, node_ca, log = certificate_section(tmp_path, lambda path: path.write_text('\n' + cert))
    assert result.returncode == 0, result.stderr
    assert node_ca.read_text() == cert and '-u acc -- sh -c' in log and '/dev/stdin' not in log


async def test_iu_2_the_public_ca_endpoint_serves_only_a_single_certificate(api, tmp_path):
    cert, private = root_pem()
    settings = api.app.state.ctx.settings
    (tmp_path / 'node-ca.pem').write_text(private + cert)
    object.__setattr__(settings, 'node_ca_file', tmp_path / 'node-ca.pem')
    assert (await api.get('/api/nodes/ca')).status_code == 404
    (tmp_path / 'node-ca.pem').write_text(cert)
    served = await api.get('/api/nodes/ca')
    assert served.status_code == 200 and served.text == cert


# IU-3: setup.sh wrote Caddy's apt keyring in place, then compared only the first fingerprint: an extra key passed.
def test_iu_3_an_extra_key_never_reaches_caddys_apt_keyring(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    for name, body in {
        'curl': 'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then shift; echo key > "$1"; fi; shift; done',
        'gpg': "printf 'pub::::::::::\\nfpr:::::::::65760C51EDEA2017CEA2CA15155B6D79CA56EA34:\\n"
               "pub::::::::::\\nfpr:::::::::0000000000000000000000000000000000000000:\\n'; "
               'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then shift; echo keyring > "$1"; fi; shift; done',
        'apt-get': 'exit 0'}.items():
        (tools / name).write_text('#!/bin/sh\n' + body + '\n')
        (tools / name).chmod(0o755)
    keyring, sources = tmp_path / 'caddy.gpg', tmp_path / 'caddy-stable.list'
    script = (REPO / 'deploy/setup.sh').read_text()
    script = script[script.index('CADDY_KEYRING='):script.index('apt-get install -y -qq caddy')]
    script = (script.replace('/usr/share/keyrings/caddy-stable-archive-keyring.gpg', str(keyring))
              .replace('/etc/apt/sources.list.d/caddy-stable.list', str(sources)))
    result = subprocess.run(['bash', '-eu', '-c', 'die() { echo "$*" >&2; exit 1; }; ' + script],
                            capture_output=True, text=True, timeout=60,
                            env={**os.environ, 'REPO': str(REPO), 'PATH': f"{tools}:{os.environ['PATH']}"})
    assert result.returncode == 1 and 'refused' in result.stderr, result.stderr
    assert not keyring.exists() and not sources.exists()


# IU-4: the Cloudflare token was on curl's command line, readable by every local account in /proc.
def test_iu_4_the_cloudflare_token_reaches_curl_on_stdin_only(tmp_path):
    tools, argv, stdin = tmp_path / 'tools', tmp_path / 'argv', tmp_path / 'stdin'
    tools.mkdir()
    (tools / 'curl').write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {argv}\ncat > {stdin}\necho "{{}}"\n')
    (tools / 'jq').write_text('#!/bin/sh\ncat >/dev/null\necho 1\n')
    for name in ('curl', 'jq'):
        (tools / name).chmod(0o755)
    script = (REPO / 'deploy/setup.sh').read_text()
    script = script[script.index('    found=$('):script.index('    [ "$found" = 1 ]')]
    result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script + '\n[ "$found" = 1 ]'], capture_output=True,
                            text=True, timeout=60,
                            env={**os.environ, 'CF_API_TOKEN': 'EXAMPLE-cf-token-123', 'ACC_ZONE': 'example.com',
                                 'PATH': f"{tools}:{os.environ['PATH']}"})
    assert result.returncode == 0, result.stderr
    assert 'EXAMPLE-cf-token-123' not in argv.read_text()
    assert stdin.read_text() == 'Authorization: Bearer EXAMPLE-cf-token-123\n'


# IU-5: setup.sh from a new version, run while another version was installed, silently ran the installed one.
def setup_copy_step(tmp_path, installed_version):
    repo, app = tmp_path / 'new', tmp_path / 'app'
    (repo / 'deploy').mkdir(parents=True)
    (repo / 'pyproject.toml').write_text('version = "0.15.0-rc.2"\n')
    (app / 'deploy').mkdir(parents=True)
    (app / 'pyproject.toml').write_text(f'version = "{installed_version}"\n')
    (app / 'deploy/setup.sh').write_text('#!/bin/sh\necho "ran the installed setup"\n')
    (app / 'deploy/setup.sh').chmod(0o755)
    script = (REPO / 'deploy/setup.sh').read_text()
    script = script[script.index("# Rynhult's units run it from"):script.index('VERSION=$(sed')]
    prelude = "die() { printf 'ERROR: %s\\n' \"$*\" >&2; exit 1; }; log() { :; }; info() { :; }; "
    return subprocess.run(['bash', '-eu', '-c', prelude + script, 'setup.sh'], capture_output=True, text=True,
                          timeout=60, env={**os.environ, 'REPO': str(repo), 'APP': str(app), 'DRY': '', 'PKG': 'apt'})


def test_iu_5_another_installed_version_is_refused_not_run(tmp_path):
    result = setup_copy_step(tmp_path, '0.15.0-rc.1')
    assert result.returncode == 1 and 'ran the installed setup' not in result.stdout
    assert '0.15.0-rc.1 is installed' in result.stderr and 'this setup is 0.15.0-rc.2' in result.stderr


def test_iu_5_the_same_version_continues_from_the_installed_copy(tmp_path):
    result = setup_copy_step(tmp_path, '0.15.0-rc.2')
    assert result.returncode == 0 and 'ran the installed setup' in result.stdout


# Owner-approved RC channel supersedes the stable-only policy; default Stable still refuses RCs.
def test_root_update_candidates_require_channel_and_signature(tmp_path):
    import pytest

    from acc.update_helper import validate_node_request
    from acc.updates import available, download_release, newer, validate_request
    with pytest.raises(ValueError):
        validate_request({'action': 'update', 'version': '0.15.0-rc.2'})
    assert validate_request({'action': 'update', 'version': '0.15.0-rc.2'}, include_prereleases=True) == '0.15.0-rc.2'
    with pytest.raises(ValueError, match='signature'):
        validate_request({'action': 'update', 'version': '0.15.0-rc.2', 'allow_unsigned': True}, include_prereleases=True)
    assert validate_node_request({'action': 'update', 'version': '0.15.0-rc.2', 'build': 'a' * 64, 'sha256': 'b' * 64}) == '0.15.0-rc.2'
    with pytest.raises(ValueError, match='stable'):
        download_release('owner/repo', {'latest': '0.15.0-rc.2', 'assets': {}}, tmp_path / 'stage',
                         get=lambda *a, **k: pytest.fail('nothing is fetched for a candidate'))
    assert newer('0.15.0', '0.15.0-rc.1') and not newer('0.15.0-rc.2', '0.15.0-rc.1') and not newer('0.14.0', '0.15.0-rc.1')
    assert not available({'latest': '0.15.0-rc.2'}, '0.15.0-rc.1', 'c' * 64)
    assert available({'latest': '0.15.0-rc.2'}, '0.15.0-rc.1', 'c' * 64, include_prereleases=True)


import pytest  # noqa: E402


@pytest.mark.parametrize('invalid', ['symlink', 'not_sqlite', 'public', 'wrong_revision'])
def test_existing_pre_migration_copy_must_be_a_private_valid_snapshot(tmp_path, invalid):
    database = tmp_path / 'acc.db'
    url = f'sqlite:///{database}'
    command.upgrade(alembic_config(url), '0066')
    retained = tmp_path / 'acc.db.pre-migration-0066'
    if invalid == 'symlink':
        retained.symlink_to(tmp_path / 'missing')
    elif invalid == 'not_sqlite':
        retained.write_bytes(b'incomplete recovery copy')
        retained.chmod(0o600)
    else:
        with closing(sqlite3.connect(retained)) as copy:
            copy.execute('CREATE TABLE alembic_version (version_num TEXT)')
            copy.execute('INSERT INTO alembic_version VALUES (?)', ('0065' if invalid == 'wrong_revision' else '0066',))
            copy.commit()
        retained.chmod(0o644 if invalid == 'public' else 0o600)
    with pytest.raises(RuntimeError, match='pre-migration copy'):
        migrate(url)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchall() == [('0066',)]
    assert retained.is_symlink() if invalid == 'symlink' else retained.exists()


def test_documented_manual_rollback_moves_failed_tree_before_restoring_previous(tmp_path):
    import re
    import shlex
    text = (REPO / 'docs/wiki/User-Guide/Updating.md').read_text().split('If the new version fails:', 1)[1]
    text = text.split('Remove old copies', 1)[0]
    moves = re.findall(r'`sudo (mv [^`]+)`', text)
    assert len(moves) >= 2, 'rollback needs explicit commands for both the failed and previous app trees'
    root = tmp_path / 'srv/acc'
    (root / 'app').mkdir(parents=True)
    (root / 'app.previous').mkdir()
    (root / 'app/version').write_text('failed')
    (root / 'app.previous/version').write_text('previous')
    for move in moves:
        args = shlex.split(move.replace('/srv/acc', str(root)))
        subprocess.run(args, check=True)
    assert (root / 'app/version').read_text() == 'previous'
    assert (root / 'app.failed/version').read_text() == 'failed'
    assert not (root / 'app/app.previous').exists()
