"""Actual gh config files, not pre-decoded alias mocks. Commands are data only."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

loader = importlib.machinery.SourceFileLoader('guard_alias_files', str(Path(__file__).resolve().parents[1] / 'bin/acc-guard'))
guard = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(guard)
POLICY = {'workspace': '/srv/ws', 'rules': {'merge_pr': True}, 'custom': []}


@pytest.mark.parametrize('text', [
    'aliases:\n  ship: "pr \\u006derge"\n',
    'aliases:\n  ship: "pr \\x6derge"\n',
    'aliases:\n  ship: >-\n    pr merge\n',
    'aliases: {ship: "pr merge"}\n',
    'aliases:\n  "\\u0073hip": pr merge\n',
])
def test_yaml_alias_spellings_cannot_hide_a_merge(tmp_path, monkeypatch, text):
    (tmp_path / 'config.yml').write_text(text)
    monkeypatch.setitem(guard.environment, 'GH_CONFIG_DIR', str(tmp_path))
    verdict = guard.check(POLICY, 'Bash', {'command': 'gh ship'}, '/srv/ws')
    assert verdict and verdict[0] == 'merge_pr'


@pytest.mark.parametrize('value', ['pr checkout', '"pr checkout"', "'pr checkout'", 'pr checkout # a comment'])
def test_plain_non_merge_aliases_stay_usable(tmp_path, monkeypatch, value):
    (tmp_path / 'config.yml').write_text(f'aliases:\n  co: {value}\n')
    monkeypatch.setitem(guard.environment, 'GH_CONFIG_DIR', str(tmp_path))
    assert guard.check(POLICY, 'Bash', {'command': 'gh co 7'}, '/srv/ws') is None


def test_uncheckable_alias_file_refuses_aliases_but_not_builtin_commands(tmp_path, monkeypatch):
    (tmp_path / 'config.yml').write_text('aliases: {ship: pr merge}\n')
    monkeypatch.setitem(guard.environment, 'GH_CONFIG_DIR', str(tmp_path))
    assert guard.check(POLICY, 'Bash', {'command': 'gh ship'}, '/srv/ws')[0] == 'merge_pr'
    assert guard.check(POLICY, 'Bash', {'command': 'gh pr view 7'}, '/srv/ws') is None


def test_oversized_alias_file_cannot_hide_a_late_merge(tmp_path, monkeypatch):
    (tmp_path / 'config.yml').write_text('# padding\n' * guard.MAX_GH_INPUT + 'aliases:\n  ship: pr merge\n')
    monkeypatch.setitem(guard.environment, 'GH_CONFIG_DIR', str(tmp_path))
    assert guard.check(POLICY, 'Bash', {'command': 'gh ship'}, '/srv/ws')[0] == 'merge_pr'


@pytest.mark.parametrize('text', ['aliases: {}\n', 'aliases: {}  # none\n', 'version: 1\naliases: {}\nbrowser:\n'])
def test_the_empty_alias_map_gh_writes_leaves_extensions_usable(tmp_path, monkeypatch, text):
    # gh 2.45 rewrites config.yml with `aliases: {}` after `gh alias delete` removes the last alias.
    (tmp_path / 'config.yml').write_text(text)
    monkeypatch.setitem(guard.environment, 'GH_CONFIG_DIR', str(tmp_path))
    assert guard.gh_aliases() == {}
    assert guard.check(POLICY, 'Bash', {'command': 'gh dash'}, '/srv/ws') is None


@pytest.mark.parametrize('text', ['aliases: {ship: pr merge}\n', 'aliases: { }\n  ship: pr merge\n'])
def test_a_non_empty_flow_alias_map_is_still_refused(tmp_path, monkeypatch, text):
    (tmp_path / 'config.yml').write_text(text)
    monkeypatch.setitem(guard.environment, 'GH_CONFIG_DIR', str(tmp_path))
    assert guard.check(POLICY, 'Bash', {'command': 'gh ship'}, '/srv/ws')[0] == 'merge_pr'


@pytest.mark.parametrize('command', [
    "printf 'aliases:\\n  ship: pr merge\\n' >> \"$GH_CONFIG_DIR/config.yml\"; gh ship",
    "sed -i 's/co: pr checkout/ship: pr merge/' \"$GH_CONFIG_DIR/config.yml\" && gh ship",
    "cp /tmp/aliases.yml \"$GH_CONFIG_DIR/config.yml\" && gh ship 7",
    "python3 -c 'open(\"cfg\", \"a\")' | gh ship 7",
    "gh ship \"$(cp /tmp/aliases.yml \"$GH_CONFIG_DIR/config.yml\")\"",
    "(cp /tmp/aliases.yml \"$GH_CONFIG_DIR/config.yml\"; gh ship 7)",
    "bash -c 'cp /tmp/aliases.yml \"$GH_CONFIG_DIR/config.yml\"; gh ship 7'",
])
def test_an_alias_file_rewritten_by_the_same_command_cannot_hide_a_merge(tmp_path, monkeypatch, command):
    # The guard reads the alias file before the command runs: anything alongside could rewrite it first.
    (tmp_path / 'config.yml').write_text('aliases:\n    co: pr checkout\n')
    monkeypatch.setitem(guard.environment, 'GH_CONFIG_DIR', str(tmp_path))
    verdict = guard.check(POLICY, 'Bash', {'command': command}, '/srv/ws')
    assert verdict and verdict[0] == 'merge_pr'


@pytest.mark.parametrize('command', ['gh co 7', 'gh dash', "bash -c 'gh co 7'", 'gh pr checks 7 && gh pr view 7',
                                     'cd /srv/ws && gh pr view 7'])
def test_aliases_on_their_own_and_built_in_commands_in_lists_still_run(tmp_path, monkeypatch, command):
    (tmp_path / 'config.yml').write_text('aliases:\n    co: pr checkout\n')
    monkeypatch.setitem(guard.environment, 'GH_CONFIG_DIR', str(tmp_path))
    assert guard.check(POLICY, 'Bash', {'command': command}, '/srv/ws') is None


@pytest.mark.parametrize('name', ['co', 'copilot', 'attestation', 'preview'])
def test_names_an_alias_can_still_take_are_read_from_the_alias_file(tmp_path, monkeypatch, name):
    # `co` is gh's default alias (redefinable with --clobber); gh 2.45 has no copilot/attestation/preview command.
    (tmp_path / 'config.yml').write_text(f'aliases:\n    {name}: pr merge\n')
    monkeypatch.setitem(guard.environment, 'GH_CONFIG_DIR', str(tmp_path))
    assert guard.check(POLICY, 'Bash', {'command': f'gh {name} 7'}, '/srv/ws')[0] == 'merge_pr'


@pytest.mark.parametrize('command', ['gh co 7', 'gh copilot suggest "list files"', 'gh attestation verify x.tgz -o o'])
def test_default_co_and_newer_built_ins_still_run_on_their_own(tmp_path, monkeypatch, command):
    (tmp_path / 'config.yml').write_text('aliases:\n    co: pr checkout\n')
    monkeypatch.setitem(guard.environment, 'GH_CONFIG_DIR', str(tmp_path))
    assert guard.check(POLICY, 'Bash', {'command': command}, '/srv/ws') is None
