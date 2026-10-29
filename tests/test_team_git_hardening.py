"""Git the broker runs outside a guarded tool call (team merge, worktrees, team checkout) never runs a program an
agent configured: hooks, fsmonitor, filter/diff/merge drivers, from the repository's config or the agent's global."""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from isolated_settings import isolated_broker
from test_changes import git, repo

from acc.broker import ops

EVIL = '*.txt filter=evil diff=evil merge=evil\n'


def config(tmp_path):
    return isolated_broker(tmp_path, socket_path=tmp_path/'s', run_dir=tmp_path, tmux_socket='x', allowed_roots=(tmp_path,),
                        allowed_uids=frozenset(), providers={}, hook_path='x', git_remote_base=str(tmp_path/'remotes'))


def output(cwd, *args):
    return subprocess.check_output(['git', '-C', str(cwd), *args], text=True).strip()


@pytest.fixture
def marks(tmp_path, monkeypatch):
    """A marker script (filters pass content through), hooks that use it, and an agent global config that sets
    fsmonitor, a filter and the agent's identity."""
    folder = tmp_path/'marks'
    folder.mkdir()
    script = tmp_path/'mark.sh'
    script.write_text(f'#!/bin/sh\ntouch "{folder}/$1"\ncase "$1" in clean|smudge|global-clean|global-smudge) exec cat;; esac\n'
                      'exit 1\n')
    script.chmod(0o755)
    hooks = tmp_path/'hooks'
    hooks.mkdir()
    for name in ('post-merge', 'post-checkout', 'pre-merge-commit', 'commit-msg', 'prepare-commit-msg',
                 'reference-transaction', 'pre-commit', 'post-commit', 'pre-push'):
        (hooks/name).write_text(f'#!/bin/sh\ntouch "{folder}/hook-{name}"\n')
        (hooks/name).chmod(0o755)
    home = tmp_path/'home'
    home.mkdir()
    (home/'.gitconfig').write_text(f'[user]\n\tname = Taylor\n\temail = taylor@example.com\n'
                                   f'[core]\n\tfsmonitor = {script} global-fsmonitor\n\thooksPath = {hooks}\n'
                                   f'[filter "global"]\n\tclean = {script} global-clean\n\tsmudge = {script} global-smudge\n'
                                   '\trequired = true\n')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home/'.config'))
    return SimpleNamespace(folder=folder, script=script, hooks=hooks)


def arm(root, marks):
    """The repository settings an agent could write into the shared .git/config."""
    s = marks.script
    for key, value in [('core.fsmonitor', f'{s} fsmonitor'), ('core.hooksPath', str(marks.hooks)),
                       ('filter.evil.clean', f'{s} clean'), ('filter.evil.smudge', f'{s} smudge'),
                       ('filter.evil.required', 'true'), ('diff.evil.textconv', f'{s} textconv'),
                       ('diff.evil.command', f'{s} diff-command'), ('diff.external', f'{s} external'),
                       ('merge.evil.driver', f'{s} merge %O %A %B'), ('commit.gpgSign', 'true'),
                       ('gpg.program', f'{s} gpg'), ('merge.verifySignatures', 'true')]:
        subprocess.run(['git', '-C', str(root), 'config', key, value], check=True)


def clear(marks):
    """Forget what the test's own (plain) git setup fired."""
    for fired in marks.folder.iterdir():
        fired.unlink()


def lead_and_helper(tmp_path):
    root = tmp_path/'repo'
    repo(root)
    (root/'.gitattributes').write_text(EVIL)
    (root/'data.txt').write_text('base\n')
    (root/'both.txt').write_text('one\ntwo\nthree\nfour\nfive\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'attributes')
    git(root, 'checkout', '-qb', 'acc/lead')
    child = tmp_path/'.worktrees'/'helper'
    git(root, 'worktree', 'add', '-q', '-b', 'acc/helper', str(child), 'HEAD')
    return root, child


def test_marker_vectors_fire_with_plain_git(tmp_path, marks):
    """The control: these settings really do run programs when git runs without the broker's hardening."""
    root, child = lead_and_helper(tmp_path)
    arm(root, marks)
    subprocess.run(['git', '-C', str(root), 'status'], capture_output=True)
    (child/'data.txt').unlink()
    subprocess.run(['git', '-C', str(child), 'checkout', '--', 'data.txt'], capture_output=True)
    fired = {p.name for p in marks.folder.iterdir()}
    assert {'fsmonitor', 'smudge', 'hook-post-checkout'} <= fired


def test_team_merge_and_worktrees_run_no_agent_configured_program(tmp_path, marks):
    root, child = lead_and_helper(tmp_path)
    (child/'data.txt').write_text('helper\n')
    (child/'new.txt').write_text('new\n')
    git(child, 'add', '.')
    git(child, 'commit', '-qm', 'helper work')
    (root/'app.py').write_text('lead\n')
    git(root, 'commit', '-qam', 'lead work')
    arm(root, marks)
    clear(marks)
    cfg = config(tmp_path)

    made = ops.make_worktree(cfg, str(root), str(tmp_path), 'second', 'acc/second')
    result = ops.team_merge(cfg, str(root), 'acc/helper', 'acc/lead')
    assert result['merged'], result
    assert ops.remove_worktree(cfg, str(root), made['path']) == {'removed': True}

    assert sorted(p.name for p in marks.folder.iterdir()) == []
    assert (root/'data.txt').read_text() == 'helper\n' and (root/'new.txt').read_text() == 'new\n'
    assert output(root, 'log', '-1', '--format=%an %P').startswith('Taylor ')  # the agent's identity, carried over
    assert len(output(root, 'log', '-1', '--format=%P').split()) == 2
    assert output(root, 'cat-file', '-p', 'HEAD').count('gpgsig') == 0


def test_custom_merge_driver_fails_closed(tmp_path, marks):
    """A driver can't be reset to git's own merge: its command is replaced by `false`, so its files conflict."""
    root, child = lead_and_helper(tmp_path)
    (child/'both.txt').write_text('one\ntwo\nthree\nfour\nFIVE\n')
    git(child, 'commit', '-qam', 'helper')
    (root/'both.txt').write_text('ONE\ntwo\nthree\nfour\nfive\n')
    git(root, 'commit', '-qam', 'lead')
    arm(root, marks)
    clear(marks)
    before = output(root, 'rev-parse', 'HEAD')
    result = ops.team_merge(config(tmp_path), str(root), 'acc/helper', 'acc/lead')
    assert not result['merged'] and result['conflicts'] == ['both.txt']
    assert sorted(p.name for p in marks.folder.iterdir()) == []  # before the test's own (plain) git reads below
    assert output(root, 'rev-parse', 'HEAD') == before and output(root, 'status', '--porcelain') == ''


def test_tracked_include_cannot_add_a_driver_mid_operation(tmp_path, marks):
    """The repository's config includes a tracked file, which the helper's branch changes: the merge's own
    follow-up git commands still neutralise the driver it adds."""
    root, child = lead_and_helper(tmp_path)
    subprocess.run(['git', '-C', str(root), 'config', 'include.path', '../.agent.cfg'], check=True)
    (child/'.agent.cfg').write_text(f'[filter "evil"]\n\tclean = {marks.script} clean\n')
    (child/'data.txt').write_text('helper\n')
    git(child, 'add', '.')
    git(child, 'commit', '-qm', 'helper adds config')
    clear(marks)
    assert ops.team_merge(config(tmp_path), str(root), 'acc/helper', 'acc/lead')['merged']
    (root/'data.txt').write_text('dirty\n')  # status must hash it (clean filter) rather than trust the index
    assert ops.remove_worktree(config(tmp_path), str(root), str(child)) == {'removed': True}
    assert sorted(p.name for p in marks.folder.iterdir()) == []


def test_team_checkout_ignores_the_agents_global_filter(tmp_path, marks):
    root = tmp_path/'repo'
    repo(root)
    git(root, 'checkout', '-qb', 'acc/lead')
    (root/'.gitattributes').write_text('*.txt filter=global\n')
    (root/'data.txt').write_text('lead\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'lead')
    clear(marks)
    cfg = config(tmp_path)
    bundle = ops.team_bundle(cfg, str(root), 'acc/lead')
    remote = ops.team_checkout(cfg, 'helper', 'acc/helper', bundle['data'], bundle['commit'])
    assert (Path(remote['path'])/'data.txt').read_text() == 'lead\n'
    assert sorted(p.name for p in marks.folder.iterdir()) == []


def test_hardened_env_lists_every_driver_and_leaves_container_checkouts_alone(tmp_path, marks):
    root, _ = lead_and_helper(tmp_path)
    arm(root, marks)
    subprocess.run(['git', '-C', str(root), 'config', 'filter.a.b=c.process', 'x'], check=True)
    env = ops._hardened_env(root)
    pairs = {env[f'GIT_CONFIG_KEY_{i}']: env[f'GIT_CONFIG_VALUE_{i}'] for i in range(int(env['GIT_CONFIG_COUNT']))}
    assert env['GIT_CONFIG_GLOBAL'] == '/dev/null' and env['GIT_CONFIG_NOSYSTEM'] == '1'
    assert pairs['core.hooksPath'] == '/dev/null' and pairs['core.fsmonitor'] == 'false'
    assert pairs['commit.gpgSign'] == 'false' and pairs['core.attributesFile'] == '/dev/null'
    assert pairs['diff.external'] == '' and pairs['filter.evil.smudge'] == '' and pairs['filter.evil.required'] == 'false'
    assert pairs['filter.a.b=c.process'] == '' and pairs['diff.evil.textconv'] == ''
    assert pairs['merge.evil.driver'] == 'false'
    assert pairs['safe.directory'] == str(root.resolve()) and pairs['user.name'] == 'Taylor'
    container = tmp_path/'.worktrees'/'boxed'
    container.mkdir()
    (tmp_path/'.worktrees'/'.boxed.acc-container').touch()
    assert ops._hardened_env(container) is None  # container_git runs it, config-checked and hardened


def workspace(tmp_path, marks):
    """A local workspace whose git (no commits yet) an agent has armed."""
    folder = tmp_path/'local'/'notes'
    folder.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(folder)], check=True)
    (folder/'.gitattributes').write_text(EVIL)
    (folder/'data.txt').write_text('notes\n')
    arm(folder, marks)
    clear(marks)
    return folder


def test_workspace_initial_commit_runs_no_agent_configured_program(tmp_path, marks):
    folder = workspace(tmp_path, marks)
    ops.prepare_private_workspace(config(tmp_path), str(folder))
    assert sorted(p.name for p in marks.folder.iterdir()) == []
    assert output(folder, 'log', '-1', '--format=%an <%ae>') == 'Rynhult <acc@example.com>'  # not the agent's


def test_workspace_promotion_commit_runs_no_agent_configured_program(tmp_path, marks):
    folder = workspace(tmp_path, marks)
    remote = tmp_path/'remotes'/'taylor'/'notes.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)
    # The remote stands in for GitHub: its receive-pack is not ours, so keep the agent's global hooks out of it.
    subprocess.run(['git', '-C', str(remote), 'config', 'core.hooksPath', '/dev/null'], check=True)
    pushed = ops.push_workspace(config(tmp_path), str(folder), 'taylor/notes')
    assert sorted(p.name for p in marks.folder.iterdir()) == []
    assert output(folder, 'log', '-1', '--format=%an %s') == 'Taylor Local workspace promoted to a project'
    assert output(remote, 'rev-parse', '--short', 'main') == pushed['head']
    assert output(remote, 'show', 'main:data.txt') == 'notes'  # committed as is: no clean filter ran


def test_team_view_and_merge_say_lfs_files_arrive_as_pointers(tmp_path):
    root = tmp_path/'repo'
    repo(root)
    git(root, 'checkout', '-qb', 'acc/lead')
    cfg = config(tmp_path)
    assert ops.team_inspect(cfg, str(root), 'acc/lead')['lfs'] is False
    child = Path(ops.make_worktree(cfg, str(root), str(tmp_path), 'helper', 'acc/helper')['path'])
    (child/'assets').mkdir()
    (child/'assets'/'.gitattributes').write_text('*.psd filter=lfs diff=lfs merge=lfs -text\n')
    git(child, 'add', '.')
    git(child, 'commit', '-qm', 'track designs in LFS')
    assert ops.team_inspect(cfg, str(child), 'acc/helper')['lfs'] is True
    merged = ops.team_merge(cfg, str(root), 'acc/helper', 'acc/lead')
    assert merged['merged'] and merged['note'] == 'LFS files arrive as pointers; run git lfs checkout'
    (root/'.gitattributes').write_text('*.bin filter=lfsx\n')  # a different filter is not LFS
    git(root, 'rm', '-q', '-r', 'assets')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'drop LFS')
    assert ops.team_inspect(cfg, str(root), 'acc/lead')['lfs'] is False


def importing(tmp_path, marks):
    """A helper's branch bundled for another machine, and the lead's repository it is imported into."""
    root, child = lead_and_helper(tmp_path)
    (child/'new.txt').write_text('helper\n')
    git(child, 'add', '.')
    git(child, 'commit', '-qm', 'helper work')
    cfg = config(tmp_path)
    return root, cfg, ops.team_bundle(cfg, str(child), 'acc/helper')


def test_team_import_cannot_be_rewritten_into_an_ext_command(tmp_path, marks):
    """url.*.insteadOf plus protocol.ext.allow in the shared .git/config would turn the bundle path into a command
    the broker runs (security review 10, SR10-3): only the file transport is allowed, so the import fails closed."""
    root, cfg, bundle = importing(tmp_path, marks)
    subprocess.run(['git', '-C', str(root), 'config', 'protocol.ext.allow', 'always'], check=True)
    subprocess.run(['git', '-C', str(root), 'config', f'url.ext::{marks.script} ext .insteadOf', str(tmp_path)],
                   check=True)
    plain = subprocess.run(['git', '-C', str(root), 'ls-remote', str(tmp_path/'anything')], capture_output=True)
    assert plain.returncode and (marks.folder/'ext').exists()  # the control: plain git runs it
    clear(marks)
    with pytest.raises(ops.OpError):
        ops.team_import(cfg, str(root), bundle['data'], bundle['commit'])
    assert sorted(p.name for p in marks.folder.iterdir()) == []


def test_team_import_runs_no_alternate_refs_command(tmp_path, marks):
    root, cfg, bundle = importing(tmp_path, marks)
    other = tmp_path/'other.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(other)], check=True)
    (root/'.git'/'objects'/'info').mkdir(parents=True, exist_ok=True)
    (root/'.git'/'objects'/'info'/'alternates').write_text(f'{other}/objects\n')
    subprocess.run(['git', '-C', str(root), 'config', 'core.alternateRefsCommand', f'{marks.script} alternate'], check=True)
    clear(marks)
    imported = ops.team_import(cfg, str(root), bundle['data'], bundle['commit'])
    assert sorted(p.name for p in marks.folder.iterdir()) == []
    assert output(root, 'rev-parse', imported['branch']) == bundle['commit']


def test_hardened_env_allows_no_transport(tmp_path):
    root = tmp_path/'repo'
    repo(root)
    env = ops._hardened_env(root)
    assert env['GIT_ALLOW_PROTOCOL'] == '' and env['GIT_NO_LAZY_FETCH'] == '1'
