"""Installer permission regression: execute only bounded preludes/sections in temporary trees."""
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PRELUDES = {
    'deploy/setup.sh': '# Only dry runs',
    'deploy/install.sh': '# Phase B',
    'deploy/acc-install': 'acc_compat_lib=',
    'deploy/node/install.sh': '# Phase B',
    'deploy/node/macos/install.sh': '# Add/remove only',
    'deploy/macos/acc-setup.sh': 'acc_compat_lib=',
    'deploy/node/windows/wsl-node.sh': 'acc_compat_lib=',
    'deploy/node/windows/source.sh': 'dest=',
}


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize('inherited', [0o077, 0o027])
@pytest.mark.parametrize('entry,stop', PRELUDES.items())
def test_installer_code_readable_and_vault_private(tmp_path, inherited, entry, stop):
    # Removing the entry point's umask must make the code/readability assertion fail.
    prefix = (REPO / entry).read_text().split(stop, 1)[0]
    app = tmp_path / 'srv/acc/app'
    script = prefix + '\nmkdir -p "$1/deploy/lib"\ncp "$2" "$1/deploy/lib/vault-key"\n' + '''
install -d -m 0700 "$3/secrets"
/usr/bin/python3 -I "$1/deploy/lib/vault-key" "$3/secrets/vault.key" "$3/acc.db"
'''
    data = tmp_path / 'data'
    result = subprocess.run(['bash', '-eu', '-c', script, '-', str(app), str(REPO / 'deploy/lib/vault-key'), str(data)],
                            umask=inherited, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    # Check other-account read/traverse bits, even when coordinator executes pytest as root.
    for directory in (app.parent.parent, app.parent, app, app / 'deploy', app / 'deploy/lib'):
        assert mode(directory) == 0o755
    assert mode(app / 'deploy/lib/vault-key') == 0o755
    assert mode(data / 'secrets') == 0o700
    assert mode(data / 'secrets/vault.key') == 0o600


@pytest.mark.parametrize('inherited', [0o077, 0o027])
def test_update_entry_normalizes_permissions_before_loading_either_mode(tmp_path, inherited):
    # Both normal and --offline dispatch share this exact prelude; never run the host helper.
    prefix = (REPO / 'deploy/updates/acc-update').read_text().split('import runpy as _compat_runpy', 1)[0]
    program = prefix + '\nfrom pathlib import Path\np = Path(__import__("sys").argv[1]); p.mkdir(); (p / "code").write_text("code")\n'
    result = subprocess.run(['/usr/bin/python3', '-I', '-c', program, str(tmp_path / 'app')],
                            umask=inherited, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert mode(tmp_path / 'app') == 0o755
    assert mode(tmp_path / 'app/code') == 0o644


def test_settings_private_before_first_byte_is_written(tmp_path):
    source = (REPO / 'deploy/setup.sh').read_text()
    section = source.split('# ------------------------------------------------------------- write config --', 1)[1]
    section = section.split('# ------------------------------------------------------------------ install --', 1)[0]
    config = tmp_path / 'accd'
    # Check mode *inside* the writer, before it emits any setting, not only after chmod.
    program = '''set -eu
umask 022
log() { :; }; info() { :; }
accd_settings() { [ "$(stat -c %a "$1")" = 600 ]; printf 'ACC_DATABASE_URL=fixture\n'; }
'''.replace('"$1"', f'"{config}"')
    result = subprocess.run(['bash', '-c', program + section.replace('/etc/default/accd', str(config))],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert mode(config) == 0o600
    assert config.read_text() == 'ACC_DATABASE_URL=fixture\n'


@pytest.mark.parametrize('strict,expected_tree,expected_mask', [('', 'old', '0022'), ('1', 'new', '0077')])
def test_vm_qualification_selects_fixed_candidate_for_strict_install(tmp_path, strict, expected_tree, expected_mask):
    source = (REPO / 'deploy/test/upgrade-rc1.sh').read_text()
    # Execute only source preparation/selection and the install invocation with
    # fake setup scripts. No host checks, accounts, packages or services run.
    preparation = source[source.index('SOURCES=$(mktemp'):source.index('OWNER=upgrade-owner')]
    preparation = preparation.replace('/var/tmp/acc-upgrade-source.', str(tmp_path / 'source.'))
    invocation = source[source.index('(umask "$INSTALL_UMASK"'):source.index('check_health()')]
    scratch = tmp_path / 'evidence'
    scratch.mkdir(mode=0o700)
    program = '''set -eu
extract_verified() {
  mkdir -p "$2/deploy"
  if [ "$1" = old ]; then version=0.15.0-rc.1; else version=0.15.0-rc.3; fi
  printf 'version = "%s"\n' "$version" > "$2/pyproject.toml"
  printf '#!/bin/bash\nprintf "tree=%%s mask=%%s\\n" "$(basename "$(dirname "$(dirname "$0")")")" "$(umask)"\n' > "$2/deploy/setup.sh"
  printf '%s\n' "$2"
}
fail() { echo "$*" >&2; exit 1; }
OLD=old NEW=new OWNER=fixture
'''
    program += f'STRICT={strict!r}\nSCRATCH="{scratch}"\n' + preparation + invocation
    result = subprocess.run(['bash', '-c', program], umask=0o077, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert (scratch / 'install.log').read_text().strip() == f'tree={expected_tree} mask={expected_mask}'
    assert mode(scratch) == 0o700
