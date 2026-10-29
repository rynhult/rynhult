"""Build-time runtime layout, exercised without root or an image engine."""
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy/containers' / f'{name}.py')
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_prepared_home_has_frozen_effective_hooks_and_separate_state(tmp_path):
    import json
    import tomllib

    prepare = module('prepare_session')
    version = tmp_path / 'codex'
    version.write_text('#!/bin/sh\necho "codex-cli 0.164.0"\n')
    version.chmod(0o755)
    home = tmp_path / 'home'
    prepare.prepare_home(home, codex_exe=str(version))
    assert json.loads((home / '.codex/hooks.json').read_text())['hooks']['PreToolUse']
    config = tomllib.loads((home / '.codex/config.toml').read_text())
    assert config['hooks']['state'] and config['sqlite_home'].startswith('/tmp/')
    assert json.loads((home / '.copilot/hooks/acc.json').read_text())['hooks']['PreToolUse']
    assert not home.stat().st_mode & 0o222
    assert not (home / '.codex').stat().st_mode & 0o222
    assert (home / '.codex/sessions').is_symlink()
    assert os.readlink(home / '.codex/sessions').startswith('/tmp/acc-state/')


def test_slim_copy_preserves_internal_symlinks_and_excludes_caches(tmp_path):
    slim = module('slim_rootfs')
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'module.py').write_text('example')
    (source / '__pycache__').mkdir()
    (source / '__pycache__/module.pyc').write_bytes(b'bytecode')
    (source / 'module-link').symlink_to('module.py')
    dest = tmp_path / 'runtime'
    slim.copy_tree(source, dest)
    assert (dest / 'module.py').read_text() == 'example'
    assert (dest / 'module-link').is_symlink()
    assert not (dest / '__pycache__').exists()


def test_policy_hooks_remain_outside_mutable_provider_state(tmp_path):
    import json
    prepare = module('prepare_session')
    version = tmp_path / 'codex'
    version.write_text('#!/bin/sh\necho "codex-cli 0.164.0"\n')
    version.chmod(0o755)
    home, policies = tmp_path / 'home', tmp_path / 'etc'
    prepare.prepare_home(home, codex_exe=str(version), policies=policies)
    claude = json.loads((policies / 'claude-code/managed-settings.json').read_text())
    assert claude['hooks']['PreToolUse'] and claude['disableAllHooks'] is False
    # Copilot skips policy hooks uid 0 doesn't own (attempt 16): its hooks are ~/.copilot/hooks, checked by its wrapper.
    assert not (policies / 'github-copilot').exists()
    for path in policies.rglob('*.json'):
        assert not path.stat().st_mode & 0o222
        assert not path.parent.stat().st_mode & 0o222
    assert (home / '.codex/installation_id').is_symlink()
    for directory in ('skills', '.tmp', 'thread-writer-locks'):
        assert (home / '.codex' / directory).is_symlink()
    assert (home / '.config').is_symlink()


def test_native_sandbox_policy_ignores_environment_and_repository(tmp_path, monkeypatch):
    policy = module('sandbox_policy')
    monkeypatch.setattr(policy, 'CONTRACT', tmp_path / 'no-launcher/contract.json')  # natively, no launcher contract
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('ACC_SESSION_READONLY_INPUTS', '1')
    monkeypatch.setenv('ACC_CONTAINER', '1')
    (tmp_path / 'contract.json').write_text('{"version":1,"sandbox":"container","network":"none"}')
    assert policy.container_is_sandbox() is False
    args = ['exec', '--sandbox', 'workspace-write', 'hello']
    assert policy.arguments('codex', args) == ['-c', 'features.hooks=true', *args]  # only hooks forced on (CT-01)
    assert policy.arguments('chrome', ['--headless']) == ['--headless']


def test_only_verified_contract_selects_container_cli_policy(monkeypatch):
    policy = module('sandbox_policy')
    monkeypatch.setattr(policy, 'container_is_sandbox', lambda: True)
    args = policy.arguments('codex', ['exec', '--sandbox', 'workspace-write', 'hello'])
    assert args[args.index('--sandbox') + 1] == 'danger-full-access'
    assert 'sandbox_mode="danger-full-access"' in args
    assert '--no-sandbox' in policy.arguments('chrome', ['--headless'])


def test_agent_owned_writable_contract_cannot_enable_policy(tmp_path, monkeypatch):
    policy = module('sandbox_policy')
    contract = tmp_path / 'contract.json'
    contract.write_text('{"version":1,"sandbox":"container","network":"none"}')
    contract.chmod(0o444)
    monkeypatch.setattr(policy, 'CONTRACT', contract)
    assert policy.container_is_sandbox() is False


def test_container_policy_preserves_literal_prompt_after_option_separator(monkeypatch):
    policy = module('sandbox_policy')
    monkeypatch.setattr(policy, 'container_is_sandbox', lambda: True)
    args = policy.arguments('codex', ['exec', '--sandbox=workspace-write', '--', '-s', '--sandbox=read-only'])
    assert args[-3:] == ['--', '-s', '--sandbox=read-only']
    assert '--sandbox=danger-full-access' in args[:-3]


def test_ct01_image_codex_wrapper_forces_hooks_in_every_mode(monkeypatch):
    policy = module('sandbox_policy')
    for contract in (False, True):
        monkeypatch.setattr(policy, 'container_is_sandbox', lambda: contract)
        args = policy.arguments('codex', ['exec', '--sandbox', 'workspace-write', 'hello'])
        assert args[:2] == ['-c', 'features.hooks=true']
        assert args[-1] == 'hello'


def unsafe_tree(root):
    root.mkdir()
    (root / 'tmp').mkdir()
    (root / 'tmp').chmod(0o1777)
    browser = root / 'ms-playwright/chrome-linux'
    browser.mkdir(parents=True)
    (browser / 'chrome').write_text('binary')
    (browser / 'chrome').chmod(0o777)
    (browser / 'helper').write_text('setuid')
    (browser / 'helper').chmod(0o4755)
    browser.chmod(0o777)
    (root / 'etc').mkdir()
    (root / 'etc').chmod(0o775)
    (root / 'etc/passwd').write_text('root:x:0:0::/:/bin/sh\n')
    (root / 'etc/passwd').chmod(0o664)
    (root / 'link').symlink_to('etc/passwd')
    acl = root / 'opt'
    acl.mkdir()
    acl.chmod(0o755)
    # A build folder's default ACL: user entries hidden behind the group bits. Not every filesystem has ACLs.
    import shutil
    import subprocess
    if shutil.which('setfacl'):
        subprocess.run(['setfacl', '-m', f'u:{os.getuid()}:rwx,m::r-x', str(acl)], capture_output=True)
    return 'system.posix_acl_access' in os.listxattr(acl)


def test_ct02_runtime_tree_has_no_group_other_writable_or_setid_entries(tmp_path):
    slim = module('slim_rootfs')
    root = tmp_path / 'runtime'
    has_acl = unsafe_tree(root)
    assert ('/opt' in slim.unsafe_entries(root)) is has_acl
    slim.normalize(root)
    assert slim.unsafe_entries(root) == []
    assert 'system.posix_acl_access' not in os.listxattr(root / 'opt')
    assert (root / 'tmp').stat().st_mode & 0o7777 == 0o1777
    assert (root / 'ms-playwright/chrome-linux/chrome').stat().st_mode & 0o7777 == 0o755
    assert (root / 'ms-playwright/chrome-linux/helper').stat().st_mode & 0o7777 == 0o755
    assert (root / 'etc/passwd').stat().st_mode & 0o7777 == 0o644
    assert (root / 'link').is_symlink()


def test_ct02_unsafe_entries_reports_writable_and_acl_entries(tmp_path):
    slim = module('slim_rootfs')
    root = tmp_path / 'runtime'
    unsafe_tree(root)
    found = slim.unsafe_entries(root)
    assert '/ms-playwright/chrome-linux/chrome' in found and '/etc' in found
    assert '/ms-playwright/chrome-linux/helper' in found and '/tmp' not in found


def verified_context(monkeypatch, tmp_path, policy, interfaces, *, owner=0, uid_map='0 0 4294967295'):
    """Everything the gate checks reports a launcher-made container, except the network under test."""
    from types import SimpleNamespace

    parent = tmp_path / 'acc-container'
    parent.mkdir()
    contract = parent / 'contract.json'
    contract.write_text('{"version":1,"issuer":"acc-containers","sandbox":"container","network":"none"}')
    contract.chmod(0o444)
    parent.chmod(0o555)
    status = tmp_path / 'status'
    status.write_text('NoNewPrivs:\t1\nSeccomp:\t2\nCapBnd:\t0000000000000000\n')
    real_stat, real_path = os.stat, policy.Path
    readonly = SimpleNamespace(f_flag=os.ST_RDONLY)

    def root_owned(path, *args, **kwargs):  # Path.lstat() is os.stat(follow_symlinks=False)
        info = real_stat(path, *args, **kwargs)
        if not isinstance(path, int) and str(path) == str(parent):
            return os.stat_result((info.st_mode, *info[1:4], owner, *info[5:]))
        return info

    monkeypatch.setattr(policy, 'CONTRACT', contract)
    monkeypatch.setattr(policy.os, 'stat', root_owned)
    monkeypatch.setattr(policy.os, 'statvfs', lambda _: readonly)
    monkeypatch.setattr(policy.os, 'fstatvfs', lambda _: readonly)
    (tmp_path / 'uid_map').write_text(f'{uid_map}\n')
    (tmp_path / 'overflowuid').write_text('65534\n')
    files = {'/proc/self/status': status, '/proc/self/uid_map': tmp_path / 'uid_map',
             '/proc/sys/kernel/overflowuid': tmp_path / 'overflowuid'}
    monkeypatch.setattr(policy, 'Path', lambda value: files.get(str(value)) or real_path(value))
    monkeypatch.setattr(policy, 'socket', SimpleNamespace(if_nameindex=lambda: list(enumerate(interfaces, 1))),
                        raising=False)


def test_ct04_verified_loopback_only_container_selects_option_a(tmp_path, monkeypatch):
    policy = module('sandbox_policy')
    verified_context(monkeypatch, tmp_path, policy, ['lo'])
    assert policy.container_is_sandbox() is True


def test_ct04_contract_copied_into_a_networked_container_does_not_select_option_a(tmp_path, monkeypatch):
    policy = module('sandbox_policy')
    verified_context(monkeypatch, tmp_path, policy, ['lo', 'eth0'])
    assert policy.container_is_sandbox() is False


# Attempt 15: the launcher's session container is rootless, and host root is never mapped in a rootless user namespace,
# so the root-owned rootfs and contract show there as the kernel's overflow uid (65534), never 0.
ROOTLESS_MAP = '0 1 1000\n1000 0 1\n1001 1001 64536'  # keep-id:uid=1000 (inside, outside, count)


def test_root_owned_parent_seen_as_the_overflow_uid_in_a_rootless_container_selects_option_a(tmp_path, monkeypatch):
    policy = module('sandbox_policy')
    verified_context(monkeypatch, tmp_path, policy, ['lo'], owner=65534, uid_map=ROOTLESS_MAP)
    assert policy.container_is_sandbox() is True


@pytest.mark.parametrize('owner,uid_map,caller', [
    (65534, '0 0 4294967295', 1000),  # the initial namespace: 65534 is the real nobody, not root
    (65534, ROOTLESS_MAP, 65534),     # the caller's own files are never root's
    (1000, ROOTLESS_MAP, 1001),       # any other owner
])
def test_overflow_uid_counts_as_root_only_in_a_user_namespace_and_never_as_the_caller(tmp_path, monkeypatch, owner,
                                                                                     uid_map, caller):
    policy = module('sandbox_policy')
    verified_context(monkeypatch, tmp_path, policy, ['lo'], owner=owner, uid_map=uid_map)
    monkeypatch.setattr(policy.os, 'getuid', lambda: caller)
    assert policy.container_is_sandbox() is False


def test_an_unreadable_uid_map_never_counts_the_overflow_uid_as_root(tmp_path, monkeypatch):
    """Review of phase1-in-container (question 2): the namespace check fails closed, so a process that can't read its
    uid map (or reads a malformed overflow uid) accepts only uid 0."""
    policy = module('sandbox_policy')
    verified_context(monkeypatch, tmp_path, policy, ['lo'], owner=65534, uid_map=ROOTLESS_MAP)
    (tmp_path / 'uid_map').unlink()
    assert policy.root_uids() == {0} and policy.container_is_sandbox() is False
    (tmp_path / 'uid_map').write_text(ROOTLESS_MAP + '\n')
    (tmp_path / 'overflowuid').write_text('nobody\n')
    assert policy.root_uids() == {0} and policy.container_is_sandbox() is False


def test_slim_rootfs_keeps_every_acc_module_the_providers_import():
    """The image keeps only acc/__init__.py, acc/providers and listed files; since #263 a provider
    imported acc.env, the kit's owner run found the build failing with ModuleNotFoundError."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    slim = (root / 'deploy/containers/slim_rootfs.py').read_text()
    for source in (root / 'src/acc/providers').glob('*.py'):
        for module in re.findall(r'^\s*(?:from|import) acc\.([\w.]+)', source.read_text(), re.M):
            if module.split('.')[0] == 'providers':
                continue
            path = '/opt/acc/src/acc/' + module.replace('.', '/') + '.py'
            assert f"'{path}'" in slim, f'{source.name} imports acc.{module}, which the slim image drops'


def test_read_only_rootfs_ships_every_mount_target_podman_and_crun_need(tmp_path):
    """The published rootfs is read-only before Podman starts: libpod can't create /etc/mtab and crun can't create a
    missing mount target (owner qualification run on acc-host, attempt 6: "creating /etc/mtab symlink: permission denied")."""
    slim = module('slim_rootfs')
    root = tmp_path / 'root'
    (root / 'etc').mkdir(parents=True)
    slim.mount_targets(root)
    slim.normalize(root)
    for name in ('proc', 'dev', 'sys', 'run', 'tmp', 'var', 'var/tmp', 'run/credentials', 'run/acc-container', 'srv/acc/run',
                 'srv/acc/workspaces'):
        assert (root / name).is_dir() and not (root / name).is_symlink(), name
    for name in ('etc/hosts', 'etc/hostname', 'etc/resolv.conf', 'run/.containerenv', 'run/acc-container/contract.json'):
        assert (root / name).is_file() and not (root / name).is_symlink() and (root / name).stat().st_size == 0, name
    assert os.readlink(root / 'etc/mtab') == '../proc/self/mounts'
    assert os.readlink(root / 'var/run') == '../run'
    assert (root / 'tmp').stat().st_mode & 0o7777 == 0o1777
    assert (root / 'var/tmp').stat().st_mode & 0o7777 == 0o1777
    assert slim.unsafe_entries(root) == []  # the strip, ACL and set-id rules still hold
    source = (ROOT / 'deploy/containers/slim_rootfs.py').read_text()
    body = source[source.index('def assemble(root):'):]
    assert body.index('mount_targets(root)') < body.index('normalize(root)')


# ---- build-time checks of the runtime tree (check_rootfs.py, the assembled stage) -------------------------------------

def fake_runtime(root):
    for folder in ('usr/bin', 'usr/local/bin', 'opt/acc/src/acc', 'opt/acc/copilot', 'opt/acc/cli/codex/bin',
                   'opt/acc/cli/claude'):
        (root / folder).mkdir(parents=True)
    (root / 'bin').symlink_to('usr/bin')
    for name in ('usr/bin/python3.12', 'usr/bin/git', 'usr/local/bin/node', 'opt/acc/cli/claude/claude',
                 'opt/acc/cli/codex/bin/codex', 'opt/acc/cli/codex/bin/codex.acc-real'):
        (root / name).write_text('')
    (root / 'usr/bin/python3').symlink_to('python3.12')                                   # relative
    (root / 'usr/local/bin/claude').symlink_to('/opt/acc/cli/claude/claude')               # absolute: stays inside
    (root / 'usr/local/bin/codex').symlink_to('/opt/acc/cli/../cli/codex/bin/codex')
    (root / 'opt/acc/state-directories.json').write_text('["/tmp/acc-state/codex/db", "/tmp/acc-state/claude"]')


def test_rootfs_checks_run_the_runtimes_own_files_and_leave_the_tree_alone(tmp_path, monkeypatch):
    check = module('check_rootfs')
    root = tmp_path / 'runtime'
    fake_runtime(root)
    ran = []
    monkeypatch.setattr(check, 'run', lambda argv, scratch, **extra: ran.append([str(a) for a in argv]) or 'v1')
    assert check.check(root) == ['v1'] * 5  # claude, codex (wrapper and binary), copilot, git
    executables = [argv[0] for argv in ran]
    # the runtime's own files, through relative and absolute symlinks, never the build stage's copies
    assert executables == [str(root / 'usr/bin/python3.12'), str(root / 'opt/acc/cli/claude/claude'),
                           str(root / 'opt/acc/cli/codex/bin/codex'), str(root / 'opt/acc/cli/codex/bin/codex.acc-real'),
                           str(root / 'usr/local/bin/node'), str(root / 'usr/bin/git')]
    assert ran[0][-4:] == [str(root / 'opt/acc/src/acc/__init__.py'), 'claude', 'codex', 'copilot']
    assert ran[4][1] == str(root / 'opt/acc/copilot/cli-main.js')


def test_rootfs_checks_refuse_a_check_that_writes_into_the_tree(tmp_path, monkeypatch):
    check = module('check_rootfs')
    root = tmp_path / 'runtime'
    fake_runtime(root)

    def writes(argv, scratch, **extra):
        (root / 'home-acc-.claude.json').write_text('{}')  # e.g. a CLI writing its config beside the binaries
        return 'v1'
    monkeypatch.setattr(check, 'run', writes)
    with pytest.raises(RuntimeError, match=r'the checks changed the runtime tree: .*home-acc-\.claude\.json'):
        check.check(root)


def test_rootfs_checks_refuse_state_directories_outside_tmp(tmp_path, monkeypatch):
    check = module('check_rootfs')
    root = tmp_path / 'runtime'
    fake_runtime(root)
    monkeypatch.setattr(check, 'run', lambda argv, scratch, **extra: 'v1')
    (root / 'opt/acc/state-directories.json').write_text('["/tmp/../etc/acc"]')
    with pytest.raises(RuntimeError, match='state directory outside /tmp'):
        check.check(root)


def test_paths_resolve_inside_the_runtime_tree(tmp_path):
    check = module('check_rootfs')
    root = tmp_path / 'runtime'
    fake_runtime(root)
    assert check.inside(root, '/bin/python3') == root / 'usr/bin/python3.12'
    assert check.inside(root, '/usr/local/bin/codex') == root / 'opt/acc/cli/codex/bin/codex'
    (root / 'usr/bin/escape').symlink_to('../../../../..')
    assert check.inside(root, '/usr/bin/escape') == root  # .. stops at the tree's root
    (root / 'loop').symlink_to('/loop')
    with pytest.raises(RuntimeError, match='symlink loop'):
        check.inside(root, '/loop')


# Review follow-up (L2): content, not mtime, decides whether the checks changed the tree.
def test_rootfs_checks_refuse_a_same_size_rewrite_with_the_mtime_put_back(tmp_path, monkeypatch):
    check = module('check_rootfs')
    root = tmp_path / 'runtime'
    fake_runtime(root)
    target = root / 'usr/bin/git'
    target.write_bytes(b'git v1')
    before = target.stat()

    def rewrites(argv, scratch, **extra):
        target.write_bytes(b'git v2')
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return 'v1'
    monkeypatch.setattr(check, 'run', rewrites)
    with pytest.raises(RuntimeError, match=r'the checks changed the runtime tree: usr/bin/git'):
        check.check(root)


# Review follow-up (L3): the build accepts only what the image's entrypoint accepts (its state folders are made by
# session_runtime.prepare, which acc-session runs).
def test_rootfs_checks_refuse_state_directories_outside_acc_state(tmp_path, monkeypatch):
    check = module('check_rootfs')
    root = tmp_path / 'runtime'
    fake_runtime(root)
    monkeypatch.setattr(check, 'run', lambda argv, scratch, **extra: 'v1')
    (root / 'opt/acc/state-directories.json').write_text('["/tmp/acc-state/claude", "/tmp/claude-cache"]')
    with pytest.raises(RuntimeError, match='state directory outside /tmp/acc-state/: /tmp/claude-cache'):
        check.check(root)
    session = (Path(__file__).resolve().parents[1] / 'deploy/containers/session_runtime.py').read_text()
    assert "name.startswith('/tmp/acc-state/')" in session  # the rule this mirrors


def podman_destinations(args):
    """Mount destinations of a `podman run --rootfs` (Podman 4.9, crun 1.14): crun's own, libpod's generated files, and
    the explicit --tmpfs/--mount ones. With --read-only, libpod adds /run, /tmp and /var/tmp tmpfs mounts unless
    --read-only-tmpfs=false (pkg/specgen/generate)."""
    found = {'/proc', '/dev', '/sys', '/etc/hosts', '/etc/hostname', '/etc/resolv.conf', '/run/.containerenv'}
    if '--read-only' in args and '--read-only-tmpfs=false' not in args:
        found |= {'/run', '/tmp', '/var/tmp'}
    for arg in args:
        if arg.startswith('--tmpfs='):
            found.add(arg.removeprefix('--tmpfs=').split(':', 1)[0])
        elif arg.startswith('--mount='):
            found.add(dict(part.split('=', 1) for part in arg.removeprefix('--mount=').split(',') if '=' in part)['dst'])
    return found


def test_qualification_probe_podman_run_finds_every_mount_target_in_the_read_only_rootfs(tmp_path):
    """The CT-05 probe runs `podman run --read-only --rootfs` on the published rootfs with the default read-only tmpfs
    mounts; owner run attempt 8 failed with "crun: mkdir `/var`: Permission denied" because /var/tmp had no target."""
    import ast
    tree = ast.parse((ROOT / 'scripts/container_qualification/image_store.py').read_text())
    runs = [[arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'podman'
            and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'run']
    assert len(runs) == 1 and '--rootfs' in runs[0] and '--read-only' in runs[0]
    slim = module('slim_rootfs')
    root = tmp_path / 'root'
    (root / 'etc').mkdir(parents=True)
    slim.mount_targets(root)
    slim.normalize(root)
    destinations = podman_destinations(runs[0])
    assert {'/run', '/tmp', '/var/tmp'} <= destinations
    for destination in destinations:
        target = root / destination.lstrip('/')
        assert target.exists() and not target.is_symlink(), destination


def test_the_var_targets_add_no_writable_path_and_no_link_out_of_the_rootfs(tmp_path):
    """Security review of session-image-var (2026-10-25): the only world-writable entries are the two sticky mount points
    (empty, under a read-only root, and covered by a tmpfs only where Podman adds one); every link the build makes is
    relative and resolves inside the rootfs, never to the host; mount points are real directories, never links; and
    the launcher still forbids /var and /var/run as mounts or their ancestors, so no grant can use the /var/run link."""
    slim = module('slim_rootfs')
    root = tmp_path / 'root'
    (root / 'etc').mkdir(parents=True)
    slim.mount_targets(root)
    slim.normalize(root)
    writable = sorted('/' + str(path.relative_to(root)) for path in slim.entries(root)
                      if path != root and not path.is_symlink() and path.lstat().st_mode & 0o0022)
    assert writable == ['/tmp', '/var/tmp']
    for name in slim.STICKY:
        assert (root / name).is_dir() and not (root / name).is_symlink() and not any((root / name).iterdir())
    for name, target in {**slim.MOUNT_LINKS, **slim.IMAGE_LINKS}.items():
        assert not os.path.isabs(target), name
        resolved = os.path.normpath(os.path.join(os.path.dirname('/' + name), target))
        assert resolved.startswith('/') and '..' not in resolved.split('/'), (name, resolved)
        assert (root / resolved.lstrip('/')).resolve().is_relative_to(root.resolve()), name
    for name in slim.MOUNT_DIRECTORIES:
        assert not any((root / part).is_symlink() for part in [Path(name), *Path(name).parents] if str(part) != '.'), name
    launcher = (ROOT / 'deploy/containers/acc-containers').read_text()
    forbidden = launcher.split('FORBIDDEN = (', 1)[1].split(')', 1)[0]
    assert '"/var"' in forbidden and '"/var/run"' in forbidden
    # SV-2: /srv/acc/bin keeps pointing at the image's /usr/local/bin, and assemble() makes no link of its own.
    assert os.path.normpath(os.path.join('/srv/acc', slim.IMAGE_LINKS['srv/acc/bin'])) == '/usr/local/bin'
    assert 'symlink_to(\'/' not in (ROOT / 'deploy/containers/slim_rootfs.py').read_text()


FAKE_PODMAN = r'''#!/usr/bin/python3 -I
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps({'args': args, 'storage_conf': os.environ.get('CONTAINERS_STORAGE_CONF'),
                          'conf': os.environ.get('CONTAINERS_CONF'), 'data': os.environ.get('XDG_DATA_HOME'),
                          'tmp': os.environ.get('TMPDIR'), 'runtime': os.environ.get('XDG_RUNTIME_DIR'),
                          'runtime_mode': (pathlib.Path(os.environ['XDG_RUNTIME_DIR']).stat().st_mode & 0o777) if os.environ.get('XDG_RUNTIME_DIR') else None}) + '\n')
root = args[args.index('--root') + 1]
pathlib.Path(root, 'overlay').mkdir(parents=True, exist_ok=True)  # what a real build writes there
command = args[args.index('overlay') + 1:]
if command[0] == 'build' and os.environ.get('FAKE_FAIL'):
    sys.exit(1)
if command[0] == 'build' and os.environ.get('FAKE_STUCK'):
    # A layer the account's own rm cannot remove (subordinate IDs, or still mounted), and an unshare that fails.
    stuck = pathlib.Path(root, 'overlay', 'stuck')
    stuck.mkdir()
    (stuck / 'file').write_text('x')
    stuck.chmod(0o555)
if command[0] == 'unshare' and os.environ.get('FAKE_STUCK'):
    sys.exit(125)
if command[:2] == ['image', 'inspect']:
    print('sha256:' + '7e' * 32 + ' amd64 linux')
elif command[0] == 'save':
    pathlib.Path(command[command.index('--output') + 1]).write_bytes(b'oci archive')
elif command[0] == 'cp':
    pathlib.Path(command[2], pathlib.Path(command[1].split(':', 1)[1]).name).write_text('inventory')
elif command[0] == 'unshare':
    os.execvp(command[1], command[1:])
'''


@pytest.mark.parametrize('fail', [False, True])
def test_build_helper_uses_a_private_throwaway_store_and_removes_it(tmp_path, fail):
    """Qualification attempt 11: building in the account's own store left a layer of the session image there."""
    import json
    import os
    import subprocess
    if os.getuid() == 0:
        pytest.skip('the helper refuses root by design')
    podman = tmp_path / 'podman'
    podman.write_text(FAKE_PODMAN)
    podman.chmod(0o755)
    log, out, home = tmp_path / 'calls', tmp_path / 'out', tmp_path / 'home'
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'ACC_BUILD_PODMAN': str(podman), 'FAKE_LOG': str(log),
           'ACC_BUILD_CENSUS': '/bin/true'}  # the quiet-account check has its own tests (test_build_image_quiet.py)
    if fail:
        env['FAKE_FAIL'] = '1'
    result = subprocess.run([ROOT / 'deploy/containers/build-image', out], env=env, capture_output=True, text=True,
                            timeout=60)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    roots = {c['args'][c['args'].index('--root') + 1] for c in calls}
    runroots = {c['args'][c['args'].index('--runroot') + 1] for c in calls}
    [store] = {str(Path(p).parent) for p in roots | runroots}
    assert store.startswith('/tmp/acc-ib.') and all(len(p) <= 50 for p in runroots)  # Podman's runroot limit
    assert all(c['storage_conf'] == str(ROOT / 'deploy/containers/build-storage.conf') and c['conf'] == '/dev/null'
               for c in calls)
    # BS-3: libpod state, the blob-info cache and scratch files are in the throwaway store too.
    assert all(c['args'][c['args'].index('--tmpdir') + 1] == store + '/tmp/libpod' and c['data'] == store + '/data'
               and c['tmp'] == store + '/tmp' for c in calls)
    assert all(c['runtime'] == store + '/xdg' and c['runtime_mode'] == 0o700 for c in calls)
    assert all('--cgroup-manager=cgroupfs' in c['args'] for c in calls)
    assert not Path(store).exists()  # removed on success and on failure
    assert not (home / '.local/share/containers').exists()  # the account's own store is never touched
    if fail:
        assert result.returncode != 0 and not (out / 'acc-session.oci.tar').exists()
        assert 'the build FAILED' in result.stderr and 'succeeded' not in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert 'The build succeeded' in result.stdout and 'build store is removed' in result.stdout
        assert (out / 'acc-session.oci.tar').read_bytes() == b'oci archive'
        assert (out / 'acc-session.oci.tar.sha256').read_text().endswith('  acc-session.oci.tar\n')
        assert (out / 'review/system-packages.txt').exists() and 'image: sha256:' + '7e' * 32 in result.stdout
        assert [c['args'][c['args'].index('overlay') + 1] for c in calls][-1] == 'unshare'
        again = subprocess.run([ROOT / 'deploy/containers/build-image', out], env=env, capture_output=True, text=True,
                               timeout=60)
        assert again.returncode != 0 and 'not empty' in again.stderr


def test_build_profiles_never_point_at_the_session_store():
    build = (ROOT / 'deploy/containers/build-storage.conf').read_text()
    assert 'additionalimagestores' not in build
    assert 'rootless_storage_path = "$HOME/.local/share/acc-image-build/storage"' in build
    assert 'acc-image-build' not in (ROOT / 'deploy/containers/storage.conf').read_text()
    docs = [ROOT / 'deploy/containers/README.md', ROOT / 'deploy/containers/ROOT-IMAGE-STORE.md',
            ROOT / 'docs/CONTAINERS.md', ROOT / 'scripts/container_qualification/README.md',
            *(ROOT / 'docs/wiki').rglob('*.md')]
    for doc in docs:
        text = doc.read_text()
        assert 'podman build' not in text, doc  # only deploy/containers/build-image builds
        assert 'CONTAINERS_STORAGE_CONF="$PWD/deploy/containers/build-storage.conf"' not in text, doc


def test_build_helper_reports_a_store_it_could_not_remove_without_hiding_the_success(tmp_path):
    """BS-2: a failed throwaway cleanup after a successful build names what is left and keeps exit 0 and the outputs."""
    import json
    import os
    import shlex
    import subprocess
    if os.getuid() == 0:
        pytest.skip('the helper refuses root by design')
    podman = tmp_path / 'podman'
    podman.write_text(FAKE_PODMAN)
    podman.chmod(0o755)
    out = tmp_path / 'out'
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path / 'home'), 'ACC_BUILD_PODMAN': str(podman),
           'FAKE_LOG': str(tmp_path / 'calls'), 'FAKE_STUCK': '1', 'ACC_BUILD_CENSUS': '/bin/true'}
    result = subprocess.run([ROOT / 'deploy/containers/build-image', out], env=env, capture_output=True, text=True,
                            timeout=60)
    calls = [json.loads(line) for line in (tmp_path / 'calls').read_text().splitlines()]
    store = str(Path(calls[0]['args'][calls[0]['args'].index('--root') + 1]).parent)
    try:
        assert f'build store {store} could not be fully removed' in result.stderr
        assert result.returncode == 0, result.stderr
        assert 'The build succeeded' in result.stdout and 'build store is removed' not in result.stdout
        assert (out / 'acc-session.oci.tar').exists() and Path(store).exists() and store.startswith('/tmp/acc-ib.')
        [command] = [line for line in result.stderr.splitlines() if 'unshare rm -rf' in line]
        assert shlex.split(command)[-5:] == ['unshare', 'rm', '-rf', '--', store] and '$HOME' not in command
    finally:
        if (Path(store) / 'storage/overlay/stuck').exists():
            (Path(store) / 'storage/overlay/stuck').chmod(0o755)
        subprocess.run(['rm', '-rf', '--', store], check=True)


@pytest.mark.parametrize('provider,event', [('claude', 'PostToolUse'), ('codex', ''), ('copilot', 'UserPromptSubmit')])
def test_hook_sink_records_with_only_the_image_commands(tmp_path, provider, event):
    # Attempt 16: acc-hook ran `dirname`, which the image doesn't have, so dash failed to source acc-env.sh and exited 2
    # with an error on stderr before writing the event. Codex treats that as a blocked prompt (no model call), and
    # Claude lost every PostToolUse event.
    import json
    import shutil
    import subprocess

    slim = module('slim_rootfs')
    path = tmp_path / 'path'
    path.mkdir()
    for command in slim.COMMANDS:
        found = shutil.which(command)
        if found:
            (path / command).symlink_to(found)
    assert not (path / 'dirname').exists()
    bin_dir = tmp_path / 'usr/local/bin'
    bin_dir.mkdir(parents=True)
    shutil.copy2(ROOT / 'bin/acc-hook', bin_dir / 'acc-hook')
    shutil.copy2(ROOT / 'deploy/lib/acc-env.sh', bin_dir / 'acc-env.sh')
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    payload = json.dumps({'hook_event_name': event or 'PostToolUse', 'tool_input': {'command': 'true'}})
    result = subprocess.run([str(bin_dir / 'acc-hook'), provider, *([event] if event else [])], input=payload,
                            capture_output=True, text=True, timeout=10,
                            env={'PATH': str(path), 'ACC_RUN_DIR': str(run_dir), 'ACC_SESSION_ID': 'qualification'})
    assert result.returncode == 0 and not result.stderr, result.stderr
    [line] = (run_dir / 'events.jsonl').read_text().splitlines()
    record = json.loads(line)
    assert record['provider'] == provider and record['event'] == event and record['hook'] == json.loads(payload)
    assert record['ts'].endswith('Z')


@pytest.fixture
def copilot_session(tmp_path, monkeypatch):
    """The image's Copilot layout under tmp_path: the provider's own hooks file read-only, the session's COPILOT_HOME,
    and a repository to start in."""
    from acc.providers import get

    policy = module('sandbox_policy')
    image_home = tmp_path / 'image/home/acc'
    get('copilot').bootstrap(image_home, '/usr/local/bin/acc-hook')
    hooks = image_home / '.copilot/hooks'
    (hooks / 'acc.json').chmod(0o444)
    hooks.chmod(0o555)
    state = tmp_path / 'tmp/acc-state/copilot'
    state.mkdir(parents=True, mode=0o700)
    workspace = tmp_path / 'srv/acc/workspaces/project'
    workspace.mkdir(parents=True)
    shared = tmp_path / 'image/opt/acc/src/acc/providers/copilot_settings.py'
    shared.parent.mkdir(parents=True)
    shared.write_text((ROOT / 'src/acc/providers/copilot_settings.py').read_text())
    shared.chmod(0o444)
    monkeypatch.setattr(policy, 'COPILOT_SETTINGS', shared)
    monkeypatch.setattr(policy, 'COPILOT_HOOKS', hooks)
    monkeypatch.setattr(policy, 'COPILOT_HOME', state)
    # The real rule is root's and read-only up to /, which tmp_path can't be; fixed() is tested on its own below.
    monkeypatch.setattr(policy, 'fixed', lambda path: not path.lstat().st_mode & 0o222)
    environ = {'COPILOT_HOME': str(state)}
    yield policy, workspace, environ
    hooks.chmod(0o755)


def test_copilot_starts_with_the_image_hooks_linked_into_its_session_home(copilot_session):
    policy, workspace, environ = copilot_session
    assert policy.copilot_problem(['-p', 'x', '--trust-folder'], cwd=workspace, environ=environ) is None
    link = policy.COPILOT_HOME / 'hooks'
    assert link.is_symlink() and os.readlink(link) == str(policy.COPILOT_HOOKS)
    # A second start finds the same link.
    assert policy.copilot_problem([], cwd=workspace, environ=environ) is None
    assert policy.copilot_problem(['--version'], cwd=workspace, environ={}) is None
    assert policy.arguments('copilot', ['-p', 'x']) == [policy.COPILOT_MAIN, '-p', 'x']


@pytest.mark.parametrize('change,problem', [
    (lambda p, w, e: e.update(COPILOT_HOME='/tmp/elsewhere'), 'COPILOT_HOME must be'),
    (lambda p, w, e: e.pop('COPILOT_HOME'), 'COPILOT_HOME must be'),
    (lambda p, w, e: (p.COPILOT_HOME / 'hooks').mkdir(), 'is not the link'),
    (lambda p, w, e: (p.COPILOT_HOME / 'hooks').symlink_to(w), 'is not the link'),
    (lambda p, w, e: (p.COPILOT_HOOKS / 'acc.json').chmod(0o644), 'is not intact'),
    (lambda p, w, e: (p.COPILOT_HOOKS.chmod(0o755), (p.COPILOT_HOOKS / 'extra.json').write_text('{}'),
                      p.COPILOT_HOOKS.chmod(0o555)), 'is not intact'),
    (lambda p, w, e: (p.COPILOT_HOOKS.chmod(0o755), (p.COPILOT_HOOKS / 'acc.json').chmod(0o644),
                      (p.COPILOT_HOOKS / 'acc.json').write_text('{"version": 1, "hooks": {}}'),
                      (p.COPILOT_HOOKS / 'acc.json').chmod(0o444), p.COPILOT_HOOKS.chmod(0o555)),
     'does not run the guard'),
])
def test_copilot_refuses_without_its_verified_hooks(copilot_session, change, problem):
    policy, workspace, environ = copilot_session
    change(policy, workspace, environ)
    found = policy.copilot_problem(['-p', 'x'], cwd=workspace, environ=environ)
    assert found and problem in found


def test_copilot_refuses_a_config_dir_that_would_move_its_hooks(copilot_session):
    policy, workspace, environ = copilot_session
    for args in (['--config-dir', '/tmp/x'], ['--config-dir=/tmp/x']):
        assert '--config-dir' in policy.copilot_problem(args, cwd=workspace, environ=environ)
    assert policy.copilot_problem(['-p', 'x', '--', '--config-dir'], cwd=workspace, environ=environ) is None


@pytest.mark.parametrize('where,name,text,refused', [
    # Attempt 16 / 1.0.102: in a trusted folder either repository file switches every user-level hook off.
    ('repo', 'settings.json', '{"disableAllHooks": true}', True),
    ('repo', 'settings.local.json', '{"disableAllHooks": true}', True),
    ('parent', 'settings.json', '{"disableAllHooks": true}', True),
    ('repo', 'settings.json', '{"disable\\u0041llHooks": true}', True),
    ('repo', 'settings.json', '{"disableAllHooks": false, "disableAllHooks": true}', True),
    ('repo', 'settings.json', '{"DisableAllHooks": true}', True),
    ('repo', 'settings.json', '{"disabledHooks": ["0123abcd"]}', True),
    ('repo', 'settings.json', '{"disableAllHooks": true,}', True),
    ('repo', 'settings.json', '[]', True),
    ('repo', 'settings.json', '{"disableAllHooks": false, "disabledHooks": [], "model": "gpt-4o",'
                              ' "hooks": {"postToolUse": []}}', False),
    ('user', 'settings.json', '{"disableAllHooks": true, "hooks": {}}', True),
    ('user', 'config.json', '// User settings belong in settings.json.\n// This file is managed automatically.\n'
                            '{"trustedFolders": [], "auto_update": false}', False),
    ('user', 'config.json', '{"disableAllHooks": true}', True),
])
def test_copilot_refuses_settings_that_could_switch_its_hooks_off(copilot_session, where, name, text, refused):
    policy, workspace, environ = copilot_session
    folder = {'repo': workspace / '.github/copilot', 'parent': workspace.parent / '.github/copilot',
              'user': policy.COPILOT_HOME}[where]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text)
    found = policy.copilot_problem(['-p', 'x', '--trust-folder'], cwd=workspace, environ=environ)
    if refused:
        assert found and str(folder / name) in found
    else:
        assert found is None


def test_copilot_refuses_a_linked_repository_settings_file(copilot_session, tmp_path):
    policy, workspace, environ = copilot_session
    (tmp_path / 'harmless.json').write_text('{}')
    (workspace / '.github/copilot').mkdir(parents=True)
    (workspace / '.github/copilot/settings.json').symlink_to(tmp_path / 'harmless.json')
    assert 'is a link' in policy.copilot_problem([], cwd=workspace, environ=environ)


def test_copilot_refuses_when_the_shared_settings_check_is_not_the_images(copilot_session):
    policy, workspace, environ = copilot_session
    policy.COPILOT_SETTINGS.chmod(0o644)  # writable: not the image's fixed file
    assert "settings check can't be loaded" in policy.copilot_problem([], cwd=workspace, environ=environ)
    policy.COPILOT_SETTINGS.chmod(0o444)
    assert policy.copilot_problem([], cwd=workspace, environ=environ) is None


def test_copilot_wrapper_exits_before_starting_when_hooks_are_unverified(copilot_session, monkeypatch, capsys):
    policy, workspace, environ = copilot_session
    (workspace / '.github/copilot').mkdir(parents=True)
    (workspace / '.github/copilot/settings.json').write_text('{"disableAllHooks": true}')
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(policy.os, 'environ', dict(environ))
    started = []
    monkeypatch.setattr(policy.os, 'execv', lambda *args: started.append(args))
    with pytest.raises(SystemExit) as stopped:
        policy.execute('copilot', '/usr/local/bin/node', ['-p', 'x'])
    assert stopped.value.code == policy.COPILOT_REFUSED == 78 and not started
    assert "Copilot's hooks can't be verified" in capsys.readouterr().err
    (workspace / '.github/copilot/settings.json').unlink()
    policy.execute('copilot', '/usr/local/bin/node', ['-p', 'x'])
    assert started == [('/usr/local/bin/node', ['/usr/local/bin/node', policy.COPILOT_MAIN, '-p', 'x'])]
    assert policy.os.environ['COPILOT_AUTO_UPDATE'] == 'false'


def test_fixed_means_roots_and_read_only_all_the_way_up(tmp_path, monkeypatch):
    policy = module('sandbox_policy')
    monkeypatch.setattr(policy, 'read_only', lambda path: True)
    assert policy.fixed(Path('/usr/bin/env')) is True
    mine = tmp_path / 'hooks.json'
    mine.write_text('{}')
    assert policy.fixed(mine) is False                        # the session's own file
    monkeypatch.setattr(policy, 'read_only', lambda path: False)
    assert policy.fixed(Path('/usr/bin/env')) is False        # root's but on a writable filesystem


def test_claude_image_explicitly_disables_nested_sandbox_without_weakening_hooks():
    settings = module('prepare_session').claude_settings()
    assert settings['sandbox'] == {'enabled': False}
    assert settings['disableAllHooks'] is False
    assert settings['hooks']['PreToolUse']


def test_image_includes_team_spool_helper():
    recipe = (ROOT / 'deploy/containers/Containerfile').read_text()
    slim = (ROOT / 'deploy/containers/slim_rootfs.py').read_text()
    assert 'bin/acc-mcp-team' in recipe
    assert "'acc-mcp-team'" in slim


def test_payload_refuses_unverified_boundary_before_using_disabled_inner_sandboxes(monkeypatch):
    import pytest

    policy = module('sandbox_policy')
    monkeypatch.setattr(policy, 'container_is_sandbox', lambda: False)
    with pytest.raises(RuntimeError, match='unverified container sandbox'):
        policy.require_container()
    monkeypatch.setattr(policy, 'container_is_sandbox', lambda: True)
    policy.require_container()
    entrypoint = (ROOT / 'deploy/containers/acc-session').read_text()
    assert "['require_container']()" in entrypoint
