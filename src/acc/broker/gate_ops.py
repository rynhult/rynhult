"""Orchestrator v2 slice 5 on the machine: gate jobs, number scans and restacking, as the project's agent account.

accd decides what runs (src/acc/server/orchestrator/gates.py); this broker only does fixed things:
- a gate job gets a fresh detached worktree at the branch head, under this account's run folder, and runs the owner's
  argv there without a shell; it is stopped only through the pid and process start time recorded when it started,
  never by matching process names;
- numbers come from fixed repository layouts (migrations, decision records);
- a restack replays a stopped helper's own commits with `git rebase --onto` and aborts on any conflict.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from .config import BrokerConfig
from .ops import (
    BRANCH,
    OpError,
    _git,
    _git_argv,
    _git_ok,
    _hardened_env,
    _hardened_git,
    _under_roots,
    git_head,
)

JOB = re.compile(r'^[0-9a-f]{32}$')
LAYOUTS = {'migration': re.compile(r'^src/acc/server/migrations/versions/([0-9]{4})_[A-Za-z0-9_]+\.py$'),
           'decision': re.compile(r'^docs/decisions/([0-9]{4})-[A-Za-z0-9_.-]+\.md$')}
SCAN_BRANCHES = 200
JUNIT_LIMIT = 16 << 20
# Runs the owner's argv and records its exit status, so a result survives this broker restarting while it runs. It also
# applies the host budget's per-job limits itself, also across a broker restart (security review 2026-10-25, OV-5;
# threat review S5-2): niceness 10 so gates yield the CPU to people's sessions; RLIMIT_DATA per process; the log cut
# at its cap (output after it is drained and dropped, so the job never blocks); and the whole process group stopped at
# the time limit (SIGTERM, then SIGKILL), with a `timeout` marker. A per-job task count would need a cgroup: Linux's
# RLIMIT_NPROC counts the whole account, which would hit the person's own sessions.
WRAPPER = r"""
import os, resource, signal, subprocess, sys, threading
exit_path, log_path = sys.argv[1], sys.argv[2]
seconds, log_bytes, memory = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])


def confine():
    os.nice(10)
    if memory:
        resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def finish(code):
    with open(exit_path + '.tmp', 'w') as out:
        out.write(str(code))
    os.replace(exit_path + '.tmp', exit_path)


child = subprocess.Popen(sys.argv[6:], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         preexec_fn=confine)


def pump():
    written, cut = 0, False
    with open(log_path, 'ab') as log:
        while chunk := child.stdout.read1(65536):
            if written < log_bytes:
                part = chunk[:log_bytes - written]
                log.write(part)
                written += len(part)
            if written >= log_bytes and not cut:
                cut = True
                log.write(b'\n[gate log cut at the host budget limit; later output was not kept]\n')
            log.flush()


reader = threading.Thread(target=pump, daemon=True)
reader.start()
try:
    code = child.wait(timeout=seconds)
except subprocess.TimeoutExpired:
    open(os.path.join(os.path.dirname(exit_path), 'timeout'), 'w').close()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.killpg(0, signal.SIGTERM)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        finish(124)
        os.killpg(0, signal.SIGKILL)
    code = 124
reader.join(5)
finish(code)
"""
DEFAULT_LIMITS = {'seconds': 7200, 'log_bytes': 50 << 20, 'memory_bytes': 8 << 30}
ENV_KEEP = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'USER', 'LOGNAME', 'TMPDIR')
_children: dict[str, subprocess.Popen] = {}  # reaped by gate_status; a restarted broker relies on the record instead


def _job_dir(config: BrokerConfig, job: str) -> Path:
    if not isinstance(job, str) or not JOB.fullmatch(job):
        raise OpError('invalid gate job')
    return config.run_dir / f'gate-{job}'


def _base_ref(cwd: Path, base: str) -> str:
    """main means the repository's main line (main, else master); otherwise a Rynhult topic branch."""
    if base == 'main':
        for name in ('main', 'master'):
            if not _git(cwd, 'rev-parse', '--verify', '--quiet', f'refs/heads/{name}').returncode:
                return name
        raise OpError('the repository has no main or master branch')
    if not isinstance(base, str) or not BRANCH.fullmatch(base):
        raise OpError('invalid gate base')
    return base


def _numbers(cwd: Path, ref: str) -> dict[str, list[int]]:
    found: dict[str, set[int]] = {kind: set() for kind in LAYOUTS}
    for name in _git_ok(cwd, 'ls-tree', '-r', '--name-only', ref, '--', 'src/acc/server/migrations/versions', 'docs/decisions').splitlines():
        for kind, pattern in LAYOUTS.items():
            if match := pattern.fullmatch(name):
                found[kind].add(int(match[1]))
    return {kind: sorted(values) for kind, values in found.items()}


def gate_load(config: BrokerConfig) -> dict:
    return {'load1': os.getloadavg()[0], 'cores': os.cpu_count()}


def gate_numbers(config: BrokerConfig, path: str) -> dict:
    """Numbers on the main line, and those already taken on local topic branches that haven't landed."""
    cwd = _under_roots(config, path)
    base = _base_ref(cwd, 'main')
    pending: dict[str, set[int]] = {kind: set() for kind in LAYOUTS}
    branches = _git_ok(cwd, 'for-each-ref', '--format=%(refname:short)', f'--count={SCAN_BRANCHES}', 'refs/heads/acc/').split()
    for branch in branches:
        if BRANCH.fullmatch(branch):
            for kind, values in _numbers(cwd, branch).items():
                pending[kind].update(values)
    return {'base': _numbers(cwd, base), 'pending': {kind: sorted(values) for kind, values in pending.items()}}


def gate_prepare(config: BrokerConfig, path: str, job: str, branch: str, base: str) -> dict:
    cwd = _under_roots(config, path)
    folder = _job_dir(config, job)
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch):
        raise OpError('invalid gate branch')
    base_ref = _base_ref(cwd, base)
    found = _git(cwd, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}^{{commit}}')
    if found.returncode:
        raise OpError(f'{branch} does not exist in this repository')
    head = found.stdout.strip()
    tip = _git_ok(cwd, 'rev-parse', '--verify', f'refs/heads/{base_ref}^{{commit}}').strip()
    based = not _git(cwd, 'merge-base', '--is-ancestor', tip, head).returncode
    added: dict[str, set[int]] = {kind: set() for kind in LAYOUTS}
    for name in _git_ok(cwd, 'diff', '--name-only', '--diff-filter=A', f'{tip}...{head}').splitlines():
        for kind, pattern in LAYOUTS.items():
            if match := pattern.fullmatch(name):
                added[kind].add(int(match[1]))
    from acc import __version__
    result = {'runner_version': __version__ + '/gate-1', 'head': head, 'base_tip': tip, 'based': based, 'added': {k: sorted(v) for k, v in added.items()},
              'base_numbers': _numbers(cwd, tip)}
    if based:
        _remove(cwd, folder)
        folder.mkdir(mode=0o700, parents=True)
        made = _hardened_git(cwd, 'worktree', 'add', '--detach', str(folder / 'checkout'), head, timeout=300)
        if made.returncode:
            raise OpError(f'could not check out {branch}: {made.stderr.strip()[:200]}')
        result['repo'] = str(cwd)
        (folder / 'repo').write_text(str(cwd))
    return result


def _start_time(pid: int) -> str | None:
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return None
    fields = stat.rsplit(')', 1)[1].split()
    return None if fields[0] == 'Z' else fields[19]  # an exited (zombie) process is not running; field 22: start time


def _keep_first_result(folder):
    """Keep the first attempt's bounded report/exit before a targeted retry replaces them.

    Branch code writes JUnit: neither input links/special files nor existing archive
    paths are followed. Logs already append across attempts and survive cleanup.
    """
    for name, limit in (('junit.xml', JUNIT_LIMIT), ('exit', 32)):
        try:
            fd = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise OpError('original gate report is not a regular file; retry refused')
                body = source.read(limit + 1)
            if len(body) > limit:
                body = body[:limit]  # same bounded evidence policy as the job log
            fd = os.open(folder / ('first-' + name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as target:
                target.write(body)
        except FileNotFoundError:
            continue  # no earlier attempt yet
        except FileExistsError:
            archive = folder / ('first-' + name)
            if not stat.S_ISREG(archive.lstat().st_mode):
                raise OpError('original gate evidence archive is unsafe; retry refused') from None
        except OSError:
            raise OpError('could not preserve original gate evidence; retry refused') from None


def gate_start(config: BrokerConfig, job: str, argv: list[str], limits: dict | None = None) -> dict:
    folder = _job_dir(config, job)
    checkout = folder / 'checkout'
    if not checkout.is_dir():
        raise OpError('gate job is not prepared')
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and '\x00' not in a for a in argv):
        raise OpError('invalid gate command')
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    if (set(limits) != set(DEFAULT_LIMITS) or any(type(v) is not int for v in limits.values())
            or not 1 <= limits['seconds'] <= 86400 or not 1 << 20 <= limits['log_bytes'] <= 1 << 30
            or not 1 << 30 <= limits['memory_bytes'] <= 256 << 30):
        raise OpError('invalid gate limits')
    if _start_time(os.getpid()) is None:
        raise OpError('This machine cannot record a gate process identity. Run the gate on a Linux machine.')
    _keep_first_result(folder)
    for name in ('junit.xml', 'exit', 'timeout'):
        (folder / name).unlink(missing_ok=True)
    command = [a.replace('{junit}', str(folder / 'junit.xml')) for a in argv]
    env = {k: os.environ[k] for k in ENV_KEEP if k in os.environ} | {'CI': '1', 'ACC_GATE_JOB': job}
    process = subprocess.Popen([sys.executable, '-I', '-c', WRAPPER, str(folder / 'exit'), str(folder / 'gate.log'),
                                str(limits['seconds']), str(limits['log_bytes']), str(limits['memory_bytes']), *command],
                               cwd=checkout, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=env, start_new_session=True)
    _children[job] = process
    return {'pid': process.pid, 'start': _start_time(process.pid), 'log': str(folder / 'gate.log')}


def _alive(pid, start) -> bool:
    return type(pid) is int and pid > 1 and start is not None and _start_time(pid) == start


def gate_status(config: BrokerConfig, job: str, pid: int, start: str | None) -> dict:
    folder = _job_dir(config, job)
    child = _children.get(job)
    if child is not None and child.poll() is not None:
        _children.pop(job, None)
    if _alive(pid, start) and not (child is not None and child.returncode is not None):
        return {'running': True}
    try:
        code = int((folder / 'exit').read_text())
    except (OSError, ValueError):
        code = None
    return {'running': False, 'exit': code, 'timed_out': (folder / 'timeout').exists(), **_junit(folder)}


def _junit(folder: Path) -> dict:
    report = folder / 'junit.xml'
    try:
        if report.stat().st_size > JUNIT_LIMIT:
            return {'counts': {}, 'failed': [], 'rerunnable': False}
        root = ElementTree.parse(report).getroot()
    except (OSError, ElementTree.ParseError):
        return {'counts': {}, 'failed': [], 'rerunnable': False}
    counts = {'tests': 0, 'failed': 0, 'skipped': 0}
    failed, rerunnable = [], True
    for case in root.iter('testcase'):
        counts['tests'] += 1
        if case.find('skipped') is not None:
            counts['skipped'] += 1
        elif case.find('failure') is not None or case.find('error') is not None:
            counts['failed'] += 1
            test_id = _test_id(folder / 'checkout', case.get('classname') or '', case.get('name') or '')
            rerunnable &= test_id is not None
            if test_id and test_id not in failed:
                failed.append(test_id)
    counts['passed'] = counts['tests'] - counts['failed'] - counts['skipped']
    return {'counts': counts, 'failed': failed[:200], 'rerunnable': rerunnable and bool(failed)}


def _test_id(checkout: Path, classname: str, name: str) -> str | None:
    """A rerunnable id: pytest's file::Class::test, or a spec file the browser runner reruns whole."""
    parts = classname.split('.')
    for cut in range(len(parts), 0, -1):
        candidate = '/'.join(parts[:cut]) + '.py'
        if (checkout / candidate).is_file() and '..' not in parts:
            return '::'.join([candidate, *parts[cut:], name]) if name else None
    if classname and '..' not in classname.split('/') and (checkout / classname).is_file():
        return classname
    return None


def gate_stop(config: BrokerConfig, job: str, pid: int, start: str | None) -> dict:
    """Only the recorded process group: SIGTERM first, SIGKILL when asked again and it is still there."""
    folder = _job_dir(config, job)
    if not _alive(pid, start):
        return {'stopped': True}
    marker = folder / 'stop-requested'
    sig = signal.SIGKILL if marker.exists() else signal.SIGTERM
    marker.touch()
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    return {'stopped': False, 'signal': sig.name}


def _remove(cwd: Path, folder: Path) -> None:
    if (folder / 'checkout').exists():
        _hardened_git(cwd, 'worktree', 'remove', '--force', str(folder / 'checkout'), timeout=120)
    _hardened_git(cwd, 'worktree', 'prune')
    if folder.exists():
        shutil.rmtree(folder / 'checkout', ignore_errors=True)


def gate_cleanup(config: BrokerConfig, job: str) -> dict:
    """Remove the job's checkout; its log stays for reading."""
    folder = _job_dir(config, job)
    try:
        repo = _under_roots(config, (folder / 'repo').read_text().strip())
    except (OSError, OpError):
        shutil.rmtree(folder / 'checkout', ignore_errors=True)
        return {'removed': True}
    _remove(repo, folder)
    return {'removed': True}


def gate_heads(config: BrokerConfig, path: str, branches: list[str]) -> dict:
    """The current head of each listed topic branch (None when it is gone), so a gate result can be shown against
    the code it actually tested (security review 2026-10-25, threat review S5-3)."""
    cwd = _under_roots(config, path)
    if not isinstance(branches, list) or len(branches) > 100 or not all(isinstance(b, str) and (b == 'main' or BRANCH.fullmatch(b)) for b in branches):
        raise OpError('gate heads take up to 100 Rynhult topic branches')
    heads = {}
    for branch in branches:
        ref = _base_ref(cwd, branch) if branch == 'main' else branch
        found = _git(cwd, 'rev-parse', '--verify', '--quiet', f'refs/heads/{ref}^{{commit}}')
        heads[branch] = (found.stdout.strip() or None) if found.returncode == 0 else None
    return {'heads': heads}


def gate_restack(config: BrokerConfig, path: str, branch: str, parent: str, onto: str) -> dict:
    """Replay the helper's own commits (since it forked from its parent branch) onto the lead's branch or main.
    A conflict aborts and leaves the helper exactly as it was. So does a replay whose patches differ from the helper's
    own (`git patch-id --stable`, e.g. a commit git judged already applied and dropped): only the helper's own work is
    ever put on top of main or the lead's branch (security review 2026-10-25, OV-6; threat review S5-4)."""
    cwd = _under_roots(config, path)
    for value in (branch, parent):
        if not isinstance(value, str) or not BRANCH.fullmatch(value):
            raise OpError('restack takes Rynhult topic branches')
    quiet = ['-c', 'rebase.autoSquash=false', '-c', 'rebase.updateRefs=false']

    def git_env():
        return {**(_hardened_env(cwd) or os.environ), 'GIT_EDITOR': 'true', 'GIT_SEQUENCE_EDITOR': 'true'}

    def run(*args, **kwargs):
        return _git(cwd, *quiet, *args, env=git_env(), **kwargs)

    def read(*args):
        return _git_ok(cwd, *quiet, *args, env=git_env())

    def patches(rev_range: str) -> list[str]:
        """The patch-ids of the non-merge commits in a range, as a sorted list (a multiset: repeats count)."""
        log = run('log', '--no-merges', '--patch', '--format=commit %H', rev_range, timeout=120)
        if log.returncode:
            raise OpError('could not read the helper\'s commits')
        ids = subprocess.run([*_git_argv(cwd), *quiet, 'patch-id', '--stable'], input=log.stdout, capture_output=True,
                             text=True, timeout=120, env=git_env())
        if ids.returncode:
            raise OpError('could not compute the helper\'s patch ids')
        return sorted(line.split()[0] for line in ids.stdout.splitlines() if line.strip())
    target = _base_ref(cwd, onto)
    if read('branch', '--show-current').strip() != branch:
        raise OpError('the helper worktree is not on its branch')
    if read('status', '--porcelain').strip():
        raise OpError('the helper has uncommitted changes; restack refused')
    for state in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
        if (cwd / read('rev-parse', '--git-path', state).strip()).exists():
            raise OpError('the helper already has a git operation in progress')
    before = git_head(cwd)
    upstream = read('merge-base', branch, parent).strip()
    replayed = int(read('rev-list', '--count', f'{upstream}..{branch}').strip())
    own = patches(f'{upstream}..{branch}')
    try:
        result = run('rebase', '--no-autostash', '--onto', target, upstream, branch, timeout=300)
    except subprocess.TimeoutExpired:
        run('rebase', '--abort')
        raise OpError('restack timed out and was aborted') from None
    if not result.returncode:
        if patches(f'{target}..{branch}') != own:
            run('reset', '--hard', '--quiet', before)
            if git_head(cwd) != before or read('status', '--porcelain').strip():
                raise OpError('restack undo did not restore the helper; manual recovery required')
            return {'restacked': False, 'head': before, 'onto': target, 'replayed': 0, 'conflicts': [],
                    'error': "the replay didn't carry exactly the helper's own patches; nothing changed"}
        return {'restacked': True, 'head': git_head(cwd), 'previous': before, 'onto': target, 'replayed': replayed,
                'conflicts': []}
    conflicts = run('diff', '--name-only', '--diff-filter=U').stdout.splitlines()[:200]
    run('rebase', '--abort')
    if git_head(cwd) != before or read('status', '--porcelain').strip():
        raise OpError('restack abort did not restore the helper; manual recovery required')
    return {'restacked': False, 'head': before, 'onto': target, 'replayed': 0, 'conflicts': conflicts,
            'error': result.stderr.strip()[:1000]}


def methods(config: BrokerConfig) -> dict:
    """Registered in broker/server.py's method table with one line."""
    return {
        'gate_load': lambda p: gate_load(config),
        'gate_numbers': lambda p: gate_numbers(config, p['path']),
        'gate_prepare': lambda p: gate_prepare(config, p['path'], p['job'], p['branch'], p['base']),
        'gate_start': lambda p: gate_start(config, p['job'], p['argv'], p.get('limits')),
        'gate_status': lambda p: gate_status(config, p['job'], p.get('pid'), p.get('start')),
        'gate_stop': lambda p: gate_stop(config, p['job'], p.get('pid'), p.get('start')),
        'gate_cleanup': lambda p: gate_cleanup(config, p['job']),
        'gate_restack': lambda p: gate_restack(config, p['path'], p['branch'], p['parent'], p['onto']),
        'gate_heads': lambda p: gate_heads(config, p['path'], p['branches']),
    }
