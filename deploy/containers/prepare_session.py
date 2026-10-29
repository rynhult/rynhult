#!/usr/bin/python3 -I
"""Build-time immutable home; only explicit state symlinks reach writable tmpfs."""
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

STATE_LINKS = {
    '.codex/sessions': 'codex/sessions', '.codex/archived_sessions': 'codex/archived_sessions',
    '.codex/log': 'codex/log', '.codex/tmp': 'codex/tmp', '.codex/shell_snapshots': 'codex/shell_snapshots',
    '.codex/skills': 'codex/skills', '.codex/.tmp': 'codex/dot-tmp',
    '.codex/thread-writer-locks': 'codex/thread-writer-locks',
    '.codex/memories': 'codex/memories', '.codex/generated': 'codex/generated',
    '.claude/projects': 'claude/projects', '.claude/debug': 'claude/debug', '.claude/todos': 'claude/todos',
    '.claude/plans': 'claude/plans', '.claude/tasks': 'claude/tasks', '.claude/cache': 'claude/cache',
    '.claude/session-env': 'claude/session-env', '.claude/shell-snapshots': 'claude/shell-snapshots',
    '.copilot/session-state': 'copilot/session-state', '.copilot/logs': 'copilot/logs',
    '.copilot/state': 'copilot/state', '.cache': 'cache', '.local': 'local', '.config': 'config',
}


def load_providers(package=Path('/opt/acc/src/acc')):
    """Load the image's fixed package without PYTHONPATH or the build directory."""
    if not package.is_absolute():
        raise ValueError('provider package must be absolute')
    if os.geteuid() == 0:
        for path in (package, *package.parents, *package.rglob('*')):
            info = path.lstat()
            sticky = path in package.parents and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
            if (info.st_uid != 0 or (info.st_mode & 0o022 and not sticky)
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
                raise ValueError('unsafe provider package')
    init = package / '__init__.py'
    spec = importlib.util.spec_from_file_location('acc', init, submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules['acc'] = module
    exec(compile(init.read_bytes(), str(init), 'exec'), module.__dict__)


def claude_settings():
    # Image-only machine policy: Option A retains outer seccomp/proc masks.
    # Claude's nested bubblewrap is unsupported under those restrictions.
    return {'sandbox': {'enabled': False}, 'disableAllHooks': False, 'hooks': {
        event: [{'hooks': [{'type': 'command', 'command': '/usr/local/bin/' + program + ' claude ' + event}]}]
        for event, program in [('PreToolUse', 'acc-guard'), ('PostToolUse', 'acc-hook')]},
        'statusLine': {'type': 'command', 'command': '/usr/local/bin/acc-statusline'}}


def prepare_home(home, *, codex_exe='/usr/local/bin/codex', policies=None):
    from acc.providers import get

    home.mkdir(parents=True, exist_ok=True)
    get('codex').bootstrap(home, '/usr/local/bin/acc-hook', exe=codex_exe)
    get('copilot').bootstrap(home, '/usr/local/bin/acc-hook', exe='/usr/local/bin/copilot')
    config = home / '.codex/config.toml'
    config.write_text('sqlite_home = "/tmp/acc-state/codex/db"\n'
                      'log_dir = "/tmp/acc-state/codex/log"\n'
                      'check_for_update_on_startup = false\n' + config.read_text())
    (home / '.claude').mkdir(exist_ok=True)
    (home / '.claude/settings.json').write_text(json.dumps(claude_settings(), indent=2))
    (home / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True}))
    (home / '.copilot/config.json').write_text(json.dumps({'trustedFolders': [], 'auto_update': False}))
    (home / '.codex/auth.json').symlink_to('/run/acc-credentials/auth.json')
    (home / '.codex/installation_id').symlink_to('/tmp/acc-state/codex/installation_id')
    if policies is not None:
        # Copilot gets no policy copy: it skips policy hooks that uid 0 doesn't own, and root's files aren't uid 0 in
        # the rootless container. Its hooks are ~/.copilot/hooks, which its wrapper checks (sandbox_policy.copilot_problem).
        policy_files = {'claude-code/managed-settings.json': claude_settings()}
        for relative, content in policy_files.items():
            target = policies / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(content, indent=2))
            target.chmod(0o444)
            target.parent.chmod(0o555)
    for relative, target in STATE_LINKS.items():
        link = home / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to('/tmp/acc-state/' + target)
    for folder, _, files in os.walk(home):
        for name in files:
            file = Path(folder) / name
            if not file.is_symlink():
                file.chmod(0o444)
        Path(folder).chmod(0o555)


if __name__ == '__main__':
    load_providers()
    prepare_home(Path('/home/acc'), policies=Path('/etc'))
    Path('/opt/acc/state-directories.json').write_text(json.dumps(
        ['/tmp/acc-state/' + target for target in STATE_LINKS.values()] + ['/tmp/acc-state/codex/db', '/tmp/acc-state/claude', '/tmp/acc-state/copilot']))
    Path('/opt/acc/state-files.json').write_text(json.dumps(['/tmp/acc-state/codex/installation_id']))
    Path('/opt/acc/claude-settings.json').write_text(json.dumps(claude_settings(), indent=2))
