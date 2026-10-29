"""Trust must never spill beyond the exact groups ACC generates."""
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from acc.providers.codex import PROVIDER

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/codex-hook-trust.json').read_text())


@pytest.fixture
def qualified(monkeypatch):
    def version(argv, **kwargs):
        assert argv == ['/qualified/codex', '--version']
        return subprocess.CompletedProcess(argv, 0, 'codex-cli 0.163.0\n', '')
    monkeypatch.setattr('acc.providers.codex.run', version)


def test_real_hashes():
    from acc.providers.codex import acc_hook_hash
    for row in FIXTURE:
        assert acc_hook_hash(row['event'], row['command']) == row['trusted_hash']


def test_bootstrap_trusts_only_exact_groups_and_preserves_config(tmp_path, qualified):
    folder = tmp_path / '.codex'
    folder.mkdir()
    command = '/srv/acc/bin/acc-guard codex PreToolUse'
    exact = {'hooks': [{'type': 'command', 'command': command}]}
    groups = [dict(exact, matcher='.*'), {'hooks': [*exact['hooks'], {'type': 'command', 'command': 'evil'}]},
              {'hooks': [{'type': 'command', 'command': command + '; evil'}]}, exact]
    hooks = folder / 'hooks.json'
    hooks.write_text(json.dumps({'hooks': {'PreToolUse': groups}}))
    config = folder / 'config.toml'
    original = '# keep my comments\nmodel = "example"\n[hooks.state."other"]\nenabled = false # keep\ntrusted_hash = "manual"\n'
    config.write_text(original)
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    assert config.read_text().startswith(original)
    state = tomllib.loads(config.read_text())['hooks']['state']
    assert state.pop('other') == {'enabled': False, 'trusted_hash': 'manual'}
    assert len(state) == 8
    key = f'{hooks}:pre_tool_use:3:0'
    assert state[key]['trusted_hash'] == FIXTURE[6]['trusted_hash']
    assert not any(f'{hooks}:pre_tool_use:{i}:0' in state for i in range(3))
    assert json.loads(hooks.read_text())['hooks']['PreToolUse'] == groups
    before = config.read_bytes(), hooks.read_bytes()
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    assert (config.read_bytes(), hooks.read_bytes()) == before


def test_existing_acc_hash_is_updated_without_enabling_disabled_hook(tmp_path, qualified):
    folder = tmp_path / '.codex'
    folder.mkdir()
    config = folder / 'config.toml'
    key = f'{folder}/hooks.json:pre_tool_use:0:0'
    config.write_text(f'# comment\n[hooks.state.{json.dumps(key)}]\nenabled = false\ntrusted_hash = "old" # keep\n')
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    assert tomllib.loads(config.read_text())['hooks']['state'][key] == {
        'enabled': False, 'trusted_hash': FIXTURE[6]['trusted_hash']}
    assert '# comment' in config.read_text() and '# keep' in config.read_text()


@pytest.mark.parametrize('version', ['0.162.0', '0.165.0', '0.164.1', '0.164.0-dev', 'garbage'])
def test_unknown_version_never_writes_trust(tmp_path, monkeypatch, version):
    monkeypatch.setattr('acc.providers.codex.run', lambda *a, **kw: subprocess.CompletedProcess([], 0, f'codex-cli {version}\n', ''))
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    assert not (tmp_path / '.codex/config.toml').exists()


@pytest.mark.parametrize('text', ['[hooks\n', 'hooks = "unknown"\n', '[hooks]\nstate = []\n'])
def test_unknown_config_format_is_not_written(tmp_path, qualified, text):
    folder = tmp_path / '.codex'
    folder.mkdir()
    config = folder / 'config.toml'
    config.write_text(text)
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    assert config.read_text() == text


@pytest.mark.parametrize('stdout,code', [('codex-cli 0.164.0\n', 0), ('codex-cli 0.163.0\n', 1)])
def test_version_output_and_exit_status(tmp_path, monkeypatch, stdout, code):
    monkeypatch.setattr('acc.providers.codex.run', lambda *a, **kw: subprocess.CompletedProcess([], code, stdout, ''))
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    config = tmp_path / '.codex/config.toml'
    assert config.exists() == (code == 0)
    if config.exists():
        assert len(tomllib.loads(config.read_text())['hooks']['state']) == 8


@pytest.mark.parametrize('error', [FileNotFoundError(), subprocess.TimeoutExpired('codex', 5)])
def test_unavailable_version_leaves_existing_config(tmp_path, monkeypatch, error):
    def unavailable(*args, **kwargs):
        raise error
    monkeypatch.setattr('acc.providers.codex.run', unavailable)
    folder = tmp_path / '.codex'
    folder.mkdir()
    config = folder / 'config.toml'
    config.write_text('# untouched\n')
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    assert config.read_text() == '# untouched\n'


@pytest.mark.parametrize('source', ['not json', '{"hooks": []}', '{"hooks":{"PreToolUse":[null]}}',
                                  '{"unknown_schema":1,"hooks":{}}'])
def test_unfamiliar_hooks_format_never_creates_trust(tmp_path, qualified, source):
    folder = tmp_path / '.codex'
    folder.mkdir()
    hooks = folder / 'hooks.json'
    hooks.write_text(source)
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    assert not (folder / 'config.toml').exists()
    assert hooks.read_text() == source


def test_unsupported_state_layout_does_not_partially_write(tmp_path, qualified):
    folder = tmp_path / '.codex'
    folder.mkdir()
    key = f'{folder}/hooks.json:pre_tool_use:0:0'
    text = f'[hooks.state]\n{json.dumps(key)} = {{ trusted_hash = "old" }}\n'
    config = folder / 'config.toml'
    config.write_text(text)
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    assert config.read_text() == text


def test_unfamiliar_acc_state_value_leaves_all_trust_untouched(tmp_path, qualified):
    folder = tmp_path / '.codex'
    folder.mkdir()
    key = f'{folder}/hooks.json:pre_tool_use:0:0'
    text = f'[hooks.state.{json.dumps(key)}]\ntrusted_content_hash = 12\ntrusted_hash = "old"\n'
    config = folder / 'config.toml'
    config.write_text(text)
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    assert config.read_text() == text


# Security review 2026-10-23 (docs/reviews/2026-10-23-security-review-codex-hook-trust.md)

def test_trust_write_keeps_config_mode(tmp_path, qualified):
    folder = tmp_path / '.codex'
    folder.mkdir()
    config = folder / 'config.toml'
    config.write_text('model = "example"\n')
    config.chmod(0o640)
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    assert len(tomllib.loads(config.read_text())['hooks']['state']) == 8
    assert config.stat().st_mode & 0o777 == 0o640


def test_trust_write_keeps_symlinked_config(tmp_path, qualified):
    folder = tmp_path / '.codex'
    folder.mkdir()
    target = tmp_path / 'dotfiles-config.toml'
    target.write_text('model = "example"\n')
    target.chmod(0o600)
    config = folder / 'config.toml'
    config.symlink_to(target)
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    assert config.is_symlink()
    assert len(tomllib.loads(target.read_text())['hooks']['state']) == 8
    assert target.stat().st_mode & 0o777 == 0o600


def test_dangling_config_symlink_is_not_followed(tmp_path, qualified):
    folder = tmp_path / '.codex'
    folder.mkdir()
    config = folder / 'config.toml'
    config.symlink_to(tmp_path / 'elsewhere.toml')
    PROVIDER.bootstrap(tmp_path, '/srv/acc/bin/acc-hook', exe='/qualified/codex')
    assert config.is_symlink() and not (tmp_path / 'elsewhere.toml').exists()
