"""Exercise the CLI against offline hosts; regressions must not expose values or write files."""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/rename_stage2_dryrun.py'


def run(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), '--root', str(root), *args],
                          capture_output=True, text=True)


def put(root, name, content=''):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def snapshot(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
            os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


def test_legacy_host_reports_actions_without_writes_or_secret_values(tmp_path):
    put(tmp_path, 'srv/acc/data/acc.db', 'not opened as a database')
    put(tmp_path, 'etc/default/accd', 'ACC_DATA_DIR=/srv/acc/data\nACC_SECRET=super-secret\n')
    put(tmp_path, 'etc/acc-node.env', 'ACC_NODE_ID=private-id\nACC_NODE_PRIMARY=https://private.invalid\n')
    put(tmp_path, 'etc/systemd/system/accd.service', '[Service]\nEnvironment=ACC_DATA_DIR=/srv/acc/data\n')
    before = snapshot(tmp_path)
    result = run(tmp_path, '--json')
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    rows = report['findings']
    assert any(r['source'] == '/srv/acc' and r['action'] == 'compat-path' for r in rows)
    assert any(r['source'] == '/etc/systemd/system/accd.service' and
               r['target'] == '/etc/systemd/system/rynhultd.service' for r in rows)
    assert any(r.get('identifier') == 'ACC_NODE_ID' and r['target'] == 'RYNHULT_NODE_ID' for r in rows)
    assert any(r['action'] == 'keep' and r['source'] == '/srv/acc/data/acc.db' for r in rows)
    assert 'super-secret' not in result.stdout
    assert 'private-id' not in result.stdout
    assert 'private.invalid' not in result.stdout
    assert snapshot(tmp_path) == before


def test_conflicting_env_and_paths_block_but_equal_env_is_compatible(tmp_path):
    put(tmp_path, 'srv/acc/marker')
    put(tmp_path, 'srv/rynhult/marker')
    put(tmp_path, 'etc/default/accd', 'ACC_RUN_DIR=/one\nRYNHULT_RUN_DIR=/two\nACC_X=same\nRYNHULT_X=same\n')
    result = run(tmp_path, '--json')
    assert result.returncode == 2
    rows = json.loads(result.stdout)['findings']
    assert any(r['source'] == '/srv/acc' and r['action'] == 'blocked' for r in rows)
    assert any(r.get('identifier') == 'ACC_RUN_DIR' and r['action'] == 'blocked' for r in rows)
    assert any(r.get('identifier') == 'ACC_X' and r['action'] == 'keep' for r in rows)


def test_links_never_escape_fixture_and_special_files_are_not_read(tmp_path):
    # An absolute symlink must not cause the scanner to inspect the actual host.
    (tmp_path / 'etc').mkdir()
    (tmp_path / 'etc/default').symlink_to('/etc/default', target_is_directory=True)
    os.mkfifo(tmp_path / 'etc/acc-node.env')
    result = run(tmp_path, '--json')
    assert result.returncode == 2
    rows = json.loads(result.stdout)['findings']
    assert any(r['action'] == 'blocked' and r['source'] == '/etc/default' for r in rows)
    assert any(r['action'] == 'blocked' and r['source'] == '/etc/acc-node.env' for r in rows)


def test_macos_windows_and_custom_env_file(tmp_path):
    put(tmp_path, 'Users/alex/Library/LaunchAgents/local.acc.node.plist',
        '<plist><dict><key>Label</key><string>local.acc.node</string></dict></plist>')
    put(tmp_path, 'Users/alex/.acc/acc-node.env', 'ACC_NODE_ID=hidden\n')
    put(tmp_path, 'ProgramFiles/ACC/logs/install-node.log')
    put(tmp_path, 'custom/node.env', 'export ACC_NODE_CONFIG=/etc/acc-node.env\n')
    result = run(tmp_path, '--json', '--env-file', '/custom/node.env')
    assert result.returncode == 0
    rows = json.loads(result.stdout)['findings']
    assert any(r['target'].endswith('local.rynhult.node.plist') for r in rows)
    assert any(r.get('identifier') == 'ACC_NODE_CONFIG' for r in rows)
    assert any(r['source'] == '/ProgramFiles/ACC' for r in rows)


def test_empty_root_and_invalid_arguments(tmp_path):
    result = run(tmp_path)
    assert result.returncode == 0
    assert 'No detected legacy identifiers' in result.stdout
    assert run(tmp_path, '--env-file', '../../outside').returncode == 2
    assert run(tmp_path / 'missing').returncode == 2


def test_dropins_duplicates_malformed_and_oversized_files_block(tmp_path):
    put(tmp_path, 'etc/systemd/system/acc-node.service.d/override.conf',
        '[Service]\nEnvironment="ACC_NODE_CONFIG=/etc/acc-node.env"\n')
    put(tmp_path, 'etc/default/accd', 'ACC_X=one\nACC_X=two\nACC_Y="unterminated\n')
    put(tmp_path, 'etc/acc-node.env', 'x' * (1024 * 1024 + 1))
    result = run(tmp_path, '--json')
    assert result.returncode == 2
    rows = json.loads(result.stdout)['findings']
    assert any(r['action'] == 'review-reference' and r.get('identifier') == 'ACC_NODE_CONFIG' for r in rows)
    assert any(r['action'] == 'blocked' and 'Duplicate' in r['detail'] for r in rows)
    assert any(r['action'] == 'blocked' and 'Malformed' in r['detail'] for r in rows)
    assert any(r['action'] == 'blocked' and '1 MiB' in r['detail'] for r in rows)


def test_inventory_counts_occurrences_and_records_source_lines(tmp_path):
    put(tmp_path, 'src/acc/demo.py', 'ACC_DATA_DIR = "ACC_DATA_DIR"\npath = "/srv/acc/data"\n')
    put(tmp_path, 'deploy/accd.service', 'Environment=ACC_DATA_DIR=/srv/acc/data\n')
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'add', '.'], check=True)
    inventory = SCRIPT.with_name('rename_stage2_inventory.py')
    result = subprocess.run([sys.executable, str(inventory), '--root', str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '| `ACC_DATA_DIR` | `RYNHULT_DATA_DIR` | 3 |' in result.stdout
    assert '`src/acc/demo.py:1` ×2' in result.stdout
    assert '`deploy/accd.service:1`' in result.stdout
    assert '| `/srv/acc` | `/srv/rynhult` | 2 |' in result.stdout
    assert '| `accd.service` | `rynhultd.service` | 1 |' in result.stdout


def test_windows_tasks_and_protected_state_are_deferred(tmp_path):
    put(tmp_path, 'Windows/System32/Tasks/ACC node (WSL)', '<Task><Actions/></Task>')
    put(tmp_path, 'var/lib/caddy/acc-access.json', 'never parse logs')
    put(tmp_path, 'usr/local/lib/acc-dns/credentials.py', 'do not execute')
    result = run(tmp_path, '--json')
    assert result.returncode == 0
    rows = json.loads(result.stdout)['findings']
    assert any(r['action'] == 'windows-task-cutover' and r['target'].endswith('Rynhult node (WSL)') for r in rows)
    assert any(r['action'] == 'keep' and r['source'] == '/var/lib/caddy/acc-access.json' for r in rows)
    assert any(r['source'] == '/usr/local/lib/acc-dns' and r['action'] == 'compat-path' for r in rows)


def test_explicit_missing_input_is_not_reported_as_clean(tmp_path):
    result = run(tmp_path, '--json', '--env-file', '/custom/missing.env')
    assert result.returncode == 2
    rows = json.loads(result.stdout)['findings']
    assert any(r['action'] == 'blocked' and r['source'] == '/custom/missing.env' for r in rows)


def test_verified_systemd_alias_is_reported_without_following_it(tmp_path):
    unit = put(tmp_path, 'etc/systemd/system/accd.socket', '[Socket]\nListenStream=/run/accd.sock\n')
    alias = unit.with_name('rynhultd.socket')
    alias.symlink_to(unit.name)
    before = snapshot(tmp_path)
    result = run(tmp_path, '--json')
    assert result.returncode == 0, result.stdout
    rows = json.loads(result.stdout)['findings']
    assert any(r['action'] == 'unit-alias-present' and r['target'].endswith('rynhultd.socket') for r in rows)
    assert snapshot(tmp_path) == before


def test_systemd_instance_aliases_are_checked_against_the_legacy_template(tmp_path):
    source = put(tmp_path, 'etc/systemd/system/acc-broker@.service', '[Service]\n')
    alias = source.with_name('rynhult-broker@demo.service')
    alias.symlink_to('/etc/systemd/system/acc-broker@.service')
    result = run(tmp_path, '--json')
    assert result.returncode == 0, result.stdout
    assert any(r['action'] == 'unit-alias-present' and r['target'].endswith('rynhult-broker@demo.service')
               for r in json.loads(result.stdout)['findings'])
    alias.unlink()
    alias.write_text('custom')
    result = run(tmp_path, '--json')
    assert result.returncode == 2
    assert any(r['action'] == 'blocked' and r['source'].endswith('rynhult-broker@demo.service')
               for r in json.loads(result.stdout)['findings'])
