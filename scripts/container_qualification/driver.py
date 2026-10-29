"""Owner-invoked control-plane driver; uses the installed broker, never impersonates it.

Run as acc (not root, not the payload account). This writes only uniquely named
qualification grants; no database settings or global feature flags change.
"""
import base64
import hashlib
import json
import os
import pwd
import re
import socket
import stat
import subprocess
import time
import uuid
from pathlib import Path

from phase1 import MANDATORY, result, verdict

LIMITS = {'cpus': 2, 'memory_mib': 4096, 'pids': 512, 'disk_gib': 4, 'sessions': 2}
ATTACH_ROOT = Path('/run/acc-ct-attach')
LAUNCHER = Path('/usr/local/sbin/acc-containers')  # deploy/install.sh
LAUNCHER_TASKS = 256  # deploy/containers/acc-containers@.service: Podman runs as the account in that unit
SLICE_TASKS = 16 * LAUNCHER_TASKS + 32  # acc-launcher.slice: every connection the socket admits, plus reconcile


def check_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {'account', 'repo', 'root', 'policy_id', 'image'}:
        raise ValueError('plan needs exactly account, repo, root, policy_id and image')
    if not isinstance(plan['policy_id'], str) or not re.fullmatch(r'qualification-[a-z0-9.-]{1,25}', plan['policy_id']):
        raise ValueError('use a separate qualification- policy, never the normal session policy')
    if not isinstance(plan['account'], str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,23}-agent', plan['account']):
        raise ValueError('bad account')
    if not isinstance(plan['image'], str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', plan['image']):
        raise ValueError('image must be a local immutable image ID')
    for key in ('repo', 'root'):
        if not isinstance(plan[key], str) or not Path(plan[key]).is_absolute() or '..' in Path(plan[key]).parts:
            raise ValueError('repository and workspace root must be absolute paths')
    return plan


def payload_report(value):
    if (not isinstance(value, dict) or value.get('version') != 1 or value.get('go_phase_1') is not False
            or not isinstance(value.get('checks'), dict) or set(value['checks']) != set(MANDATORY)
            or not isinstance(value.get('nonce'), str) or not re.fullmatch('[0-9a-f]{32}', value['nonce'])
            or type(value.get('uid')) is not int or value['uid'] == 0 or value.get('no_new_privs') != '1'):
        raise ValueError('invalid, root, or unrestricted qualification payload')
    for check in value['checks'].values():
        if not isinstance(check, dict) or set(check) != {'status', 'reason'}:
            raise ValueError('malformed check')
        result(check['status'], check['reason'])
    return value


def kernel_limits(folder, limits):
    try:
        cpu, period = (folder / 'cpu.max').read_text().split()
        return (cpu != 'max' and int(cpu) == limits['cpus'] * int(period)
                and (folder / 'memory.max').read_text().strip() == str(limits['memory_mib'] * 1024 * 1024)
                and (folder / 'memory.swap.max').read_text().strip() == '0'
                and (folder / 'pids.max').read_text().strip() == str(limits['pids']))
    except (OSError, ValueError):
        return False


def rpc(account, method, params):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(65)
        stream.connect(f'/run/acc-broker/{account}.sock')
        stream.sendall(json.dumps({'id': 1, 'method': method, 'params': params}).encode() + b'\n')
        with stream.makefile('rb') as reader:
            line = reader.readline(1 << 20)
        answer = json.loads(line)
        if answer.get('ok') is not True:
            raise RuntimeError(refusal(method, answer.get('error')))
        return answer['result']


def refusal(method, error):
    # Never echo broker/provider diagnostics, which may contain launch input. The launcher's refusal code is one fixed
    # word (acc-containers Refused), so that alone is kept.
    code = re.search(r'container launch refused: ([a-z_]{1,40}):', str(error or ''))
    return 'broker operation refused: ' + method + (f' (launcher: {code[1]})' if code else '')


def policy_matches(plan):
    path = Path('/etc/acc/containers/policy.json')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 65536:
            return False
        policy = json.loads(os.read(fd, 65537))
    finally:
        os.close(fd)
    entry = policy.get('images', {}).get(plan['policy_id'], {})
    return (policy.get('version') == 1 and entry.get('image') == plan['image']
            and entry.get('network') == 'none'
            and entry.get('providers') == {'codex': '/usr/bin/python3'})


def host_image_evidence(plan, *, folder=Path('/etc/acc/containers/qualification')):
    """Root captures the outside-container probe; the agent cannot attest for itself."""
    try:
        path = folder / (plan['account'] + '.json')
        for parent in (folder, *folder.parents):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                return False
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 4096:
                return False
            proof = json.loads(os.read(fd, 4097))
        finally:
            os.close(fd)
        return (proof['account'] == plan['account'] and proof['image'] == plan['image']
                and 0 <= time.time() - proof['time'] <= 3600 and proof['status'] == 'pass'
                and proof['direct_write'] == proof['unshare_write'] == 'denied'
                and proof['runtime_layers'] == 'protected' and proof['execution_source'] == 'held-rootfs'
                and proof['namespace_boundary'] == 'denied'
                and proof['files'] > 0)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def grant_write(folder, session_id, grant, *, first):
    # Only the control-plane account runs this driver. Refuse a substituted grants folder.
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o027:
            raise ValueError('grants folder must be protected and owned by the control account')
        name = session_id + '.json'
        if first:
            output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=fd)
            with os.fdopen(output, 'w') as stream:
                json.dump(grant, stream)
        else:
            temporary = '.' + session_id + '.qualification'
            output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=fd)
            with os.fdopen(output, 'w') as stream:
                json.dump(grant, stream)
            os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        os.close(fd)


def cgroup_evidence(unit):
    proc = subprocess.run(['/usr/bin/systemctl', 'show', unit, '--property=ControlGroup', '--value'],
                          capture_output=True, text=True, timeout=15, check=True)
    cgroup = proc.stdout.strip()
    if not cgroup.startswith('/') or '..' in Path(cgroup).parts or cgroup == '/':
        return False, ''
    folder = Path('/sys/fs/cgroup') / cgroup.removeprefix('/')
    aggregate = folder.parent
    doubled = {key: value * LIMITS['sessions'] for key, value in LIMITS.items() if key in ('cpus', 'memory_mib', 'pids')}
    return kernel_limits(folder, LIMITS) and kernel_limits(aggregate, doubled), cgroup


def systemd_show(unit):
    proc = subprocess.run(['/usr/bin/systemctl', 'show', unit, '--property=LoadState', '--property=TasksMax'],
                          capture_output=True, text=True, timeout=15, check=True)
    return dict(line.split('=', 1) for line in proc.stdout.splitlines() if '=' in line)


def launcher_host(*, attach_root=ATTACH_ROOT, owner=0, show=systemd_show):
    """What attempt 14 found only at the real launch, checked before any grant; [] when the host is ready.

    The guard reaches its terminal folder below the attach root as the account (an earlier launcher made the root 0700
    under its unit's UMask=0077), and Podman's threads need the launcher unit's task limit (32 rejected a fork)."""
    problems = []
    try:
        info = attach_root.lstat()
    except FileNotFoundError:
        info = None  # the launcher makes it, root's 0755
    except OSError as error:
        problems.append(f'{attach_root} is unreadable ({type(error).__name__})')
        info = None
    if info is not None:
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISDIR(info.st_mode):
            problems.append(f'{attach_root} is not a folder (a link or a file); the launcher refuses it: remove it as '
                            'root, then rerun')
        elif info.st_uid != owner:
            problems.append(f'{attach_root} is owned by uid {info.st_uid}, not root; the launcher refuses it: remove '
                            'it as root, then rerun')
        elif mode != 0o755:
            problems.append(f'{attach_root} is {mode:04o}, not 0755, so the account cannot reach its terminal folder '
                            f'below it: sudo chmod 0755 {attach_root}, and install the reviewed launcher')
    for unit, bound in (('acc-containers@qualification.service', LAUNCHER_TASKS), ('acc-launcher.slice', SLICE_TASKS)):
        props = show(unit)
        value = props.get('TasksMax', '')
        if props.get('LoadState') != 'loaded':
            problems.append(f'{unit} is not installed (LoadState {props.get("LoadState", "unknown")})')
        elif value != 'infinity' and (not value.isdigit() or int(value) < bound):
            problems.append(f'{unit} has TasksMax={value[:20]}, below {bound}: Podman as the account runs out '
                            'of tasks in the launcher; install the reviewed units, then systemctl daemon-reload')
    return problems


def launcher_container_uid(path=LAUNCHER, *, owner=0):
    """The uid the payload runs as, read from the installed launcher's one CONTAINER_UID line, which builds its keep-id
    and --user flags; the driver keeps no copy of it. A report from this run says this uid whatever the account's host
    uid (attempt 15 waited out a valid report while comparing it with the host uid). The account itself is proven by
    the broker that reads the report from its checkout, the unit's cgroup and the census; the report's uid proves only
    that the launcher's mapping held."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022 or info.st_size > 1 << 20:
            raise ValueError('unprotected launcher')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            text = stream.read((1 << 20) + 1).decode()
    finally:
        os.close(fd)
    found = re.findall(r'^CONTAINER_UID = ([1-9][0-9]{0,9})$', text, re.MULTILINE)
    if len(found) != 1:
        raise ValueError('no single container uid')
    return int(found[0])


def broker_pid(account):
    proc = subprocess.run(['/usr/bin/systemctl', 'show', f'acc-broker@{account}.service', '--property=MainPID',
                           '--value'], capture_output=True, text=True, timeout=15, check=True)
    value = proc.stdout.strip()
    return int(value) if value.isdigit() else 0


PAUSE_SCOPE = re.compile(r'podman-pause-[0-9a-f]{1,64}\.scope')
SHOWN = frozenset(b'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/@._:-')


def shown(text, limit=160):
    """A cgroup path for the report. Unit names in the account's own user manager are the account's to choose, so
    anything but plain path characters is escaped, a backslash too (so a name can't spell an escape), and the length
    is bounded."""
    out = ''.join(chr(b) if b in SHOWN else f'\\x{b:02x}' for b in text.encode(errors='surrogateescape'))
    return out if len(out) <= limit else out[:limit] + '...'


def label(cgroup, uid, broker_cgroup):
    """A short fixed label for where a foreign process runs; never text taken from the process."""
    manager = f'/user.slice/user-{uid}.slice/user@{uid}.service/'
    name = cgroup.rsplit('/', 1)[-1]
    if cgroup.startswith(manager):
        if PAUSE_SCOPE.fullmatch(name):
            return 'Podman pause scope'
        if name == 'dbus.service':
            return 'session bus'
        return 'user manager unit'
    if cgroup == broker_cgroup:
        return 'broker cgroup'
    if re.fullmatch(rf'/user\.slice/user-{uid}\.slice/session-[0-9]+\.scope', cgroup):
        return 'login session'
    if cgroup.startswith('/system.slice/'):
        return 'system service'
    return 'other'


def foreign_processes(uid, unit_cgroup, broker, *, pane=None, proc=Path('/proc')):
    """Processes of the payload's account outside its unit, the broker and its user manager, as (pid, label, cgroup)
    sorted by PID; None if unobservable.

    The payload reports through the account's own checkout and its challenge is in world-readable argv, so any
    other process of that account could write a forged report. This census fails closed on hidden processes
    (hidepid) and catches long-lived writers such as native sessions; it can miss one that lives between samples.

    The broker's own service cgroup (attempt 13: the tmux server the broker started for the session) is trusted only
    structurally, and only once a strict census before the launch (`pane` None) found nothing there but the broker.
    The driver runs as acc and cannot read another account's /proc/PID/exe (a ptrace check), and comm and cmdline
    are the account's to set, so an executable allowance could not be checked here. Instead, with `pane` (the pane
    PID the broker's launch answer gave), these are attributed to the broker:
    - its direct children: commands it runs for the control plane (tmux clients, git);
    - the single process there whose parent is outside it: the tmux server, which daemonizes. Two or more such
      processes are all foreign, so a pane that detaches a helper is caught;
    - the launched pane and its descendants. In a container session it runs only the fixed attach command, never
      the provider: the agent and its hooks run in the unit.
    Everything else there is foreign, notably other panes and tmux jobs (other children of the tmux server), and a
    respawned pane (a new PID). Why this cannot forge payload evidence: account processes cannot move into a system
    service's cgroup (its cgroup.procs is root's), so every process there descends from the broker; the strict census
    before launch rules out a tmux server or pane carrying planted hooks, buffers or options from an earlier session;
    and nothing attributed runs code the agent chose, given what the kit's settle step checks before this run: no
    tmux configuration in the account's home (it could choose the pane's shell) and a broker started with -I. What remains is the documented sampling gap: an account
    process outside the unit that lives between samples could command the tmux server, but it could as well write
    the forged report itself. This relies on kernel.yama.ptrace_scope >= 1, as the broker allowance already does."""
    def fields(pid, name, prefix):
        return next(line[len(prefix):] for line in (proc / str(pid) / name).read_text().splitlines()
                    if line.startswith(prefix))
    try:
        if str(uid) not in fields(broker, 'status', 'Uid:').split():
            return None
        broker_cgroup = fields(broker, 'cgroup', '0::').strip()
    except (OSError, StopIteration, ValueError):
        return None
    manager = f'/user.slice/user-{uid}.slice/user@{uid}.service/init.scope'
    processes = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == broker:
            continue
        try:
            if str(uid) not in fields(entry.name, 'status', 'Uid:').split():
                continue
            status = (proc / entry.name / 'status').read_text()
            parent = next((int(line[5:]) for line in status.splitlines() if line.startswith('PPid:')), None)
            cgroup = fields(entry.name, 'cgroup', '0::').strip()
        except (OSError, StopIteration, ValueError):
            continue  # exited while being read
        processes[int(entry.name)] = (parent, cgroup)
    in_unit = (lambda cgroup: unit_cgroup is not None
               and (cgroup == unit_cgroup or cgroup.startswith(unit_cgroup.rstrip('/') + '/')))
    trusted = set()
    if pane is not None:
        inside = {pid for pid, (_, cgroup) in processes.items() if cgroup == broker_cgroup}
        # A process whose parent is unknown is never attributed (and never the tmux server).
        detached = [pid for pid in inside if processes[pid][0] not in (broker, None) and processes[pid][0] not in inside]
        trusted = {pid for pid in inside if processes[pid][0] == broker}
        if len(detached) == 1:
            trusted.add(detached[0])
        for pid in inside:
            seen, current = set(), pid
            while current in inside and current not in seen:  # walk up, within the cgroup, to the pane
                if current == pane:
                    trusted.add(pid)
                    break
                seen.add(current)
                current = processes[current][0]
    foreign = []
    for pid, (_, cgroup) in sorted(processes.items()):
        if cgroup == manager or in_unit(cgroup) or pid in trusted:
            continue
        foreign.append((pid, label(cgroup, uid, broker_cgroup), shown(cgroup)))
    return foreign


def describe(foreign, limit=8):
    """PID, fixed label and escaped cgroup of each foreign process, so a failed run needs no census by hand."""
    text = '; '.join(f'PID {pid} {where} {cgroup}' for pid, where, cgroup in foreign[:limit])
    return text + (f'; and {len(foreign) - limit} more' if len(foreign) > limit else '')


class Stopped(ValueError):
    """A reason the run can't go on, worded for the owner."""


def qualify(plan, *, grants=Path('/srv/acc/containers/grants'), call=rpc):
    plan = check_plan(plan)
    checks = {name: result('skip', 'trusted-launcher qualification did not reach this check') for name in MANDATORY}
    report = {'version': 1, 'status': 'no-go', 'go_phase_1': False, 'checks': checks, 'image': plan['image'],
              'limits': LIMITS, 'scope': 'Linux amd64; network none; synthetic model transport; no credentials'}
    if os.getuid() == 0 or os.getuid() != pwd.getpwnam('acc').pw_uid:
        report['reason'] = 'run this owner command as the acc control account, never root or an agent'
        checks['trusted_launcher'] = result('fail', report['reason'])
        return report
    try:
        matched = policy_matches(plan)
    except (OSError, ValueError) as error:
        report['reason'] = 'protected qualification policy unavailable: ' + type(error).__name__
        checks['trusted_launcher'] = result('fail', report['reason'])
        return report
    if not matched:
        report['reason'] = 'protected qualification policy does not match reviewed image and fixed provider map'
        checks['trusted_launcher'] = result('fail', report['reason'])
        return report
    if not host_image_evidence(plan):
        checks['image_store'] = result('fail', 'fresh root-captured host write/unshare and runtime layer proof required')
        return report
    checks['image_store'] = result('pass', 'root-captured agent host probe denied direct and unshare writes; keep-id layers protected')
    try:
        problems = launcher_host()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        problems = ['the launcher units could not be read: ' + type(error).__name__]
    try:
        container_uid = launcher_container_uid()
    except (OSError, ValueError) as error:
        problems.append(f'{LAUNCHER} does not declare one protected container uid ({type(error).__name__}): '
                        'install the reviewed launcher')
    if problems:
        report['launcher_host'] = problems
        report['reason'] = 'the host differs from what the launcher needs: ' + '; '.join(problems)
        checks['trusted_launcher'] = result('fail', report['reason'][:1000])
        return report
    session_id = uuid.uuid4().hex
    report['session_id'] = session_id
    account = plan['account']
    tmux_name = 'acc-q-' + session_id
    grant = None
    grant_owned = False
    launched = False
    clean = True
    nonces = []
    step = 'preparing the checkout'
    try:
        expected_uid = pwd.getpwnam(account).pw_uid
        broker = broker_pid(account)
        ping = call(account, 'ping', {})
        if ping.get('role') != 'agents' or ping.get('user') != account:
            raise ValueError('wrong broker identity')
        # Strict census before the first launch: nothing in the broker's cgroup but the broker, so the tmux server
        # and pane trusted during the run below are the ones this run started (foreign_processes explains why).
        # A short-lived command the broker runs for accd (a tmux client) may be sampled: a few tries, a persistent
        # process fails them all.
        for attempt in range(5):
            before = foreign_processes(expected_uid, None, broker)
            if before == []:
                break
            time.sleep(1)
        if before != []:
            report['foreign_processes'] = [{'pid': pid, 'where': where, 'cgroup': cgroup} for pid, where, cgroup in before or []]
            checks['trusted_launcher'] = result('fail', 'the account was not quiet before the launch ('
                + ('unobservable' if before is None else f'{len(before)}: {describe(before)}') + '); they could forge '
                'payload evidence: stop its sessions, the broker\'s tmux server and Podman\'s pause process, then rerun')
            raise RuntimeError('account not quiescent')
        checkout = call(account, 'make_private_checkout', {'repo': plan['repo'], 'root': plan['root'],
                        'name': 'qualification-' + session_id, 'branch': 'acc/qualification-' + session_id})['path']
        run_dir = str(Path(ping['run_dir']) / session_id)
        report['checkout'] = checkout  # retained for owner review, never force-removed
        policy = json.dumps({'rules': {}, 'custom': [{'pattern': '^touch /tmp/acc-qualification-forbidden$',
                            'reason': 'qualification forbidden command'}], 'workspace': checkout})
        files = {'guardrails.json': policy}
        guard_files = 'guardrails.json=' + hashlib.sha256(policy.encode()).hexdigest()
        for generation in (1, 2):
            container = {'generation': generation, 'policy_id': plan['policy_id']}
            challenge = uuid.uuid4().hex
            grant = {'version': 1, 'session_id': session_id, **container, 'account': account,
                     'provider': 'codex', 'args': ['/opt/acc/qualification/phase1.py', challenge], 'checkout': checkout,
                     'run_dir': run_dir, 'guard_files': guard_files, 'limits': LIMITS}
            step = f'generation {generation}: writing the grant'
            grant_write(grants, session_id, grant, first=generation == 1)
            grant_owned = True
            started = time.monotonic()
            # Set before RPC: a failed/timeout launch may have partially started a unit.
            launched = True
            step = f'generation {generation}: launch'
            launch = call(account, 'launch', {'acc_id': session_id, 'tmux_name': tmux_name, 'provider': 'codex',
                          'cwd': checkout, 'files': files, 'args': grant['args'], 'container': container})
            unit = f'acc-ct-{session_id}-g{generation}.service'
            if launch.get('container', {}).get('unit') != unit or launch['container'].get('state') != 'running':
                raise ValueError('trusted launcher did not confirm expected running unit')
            checks['trusted_launcher'] = result('pass', 'registered broker authorized the expected fixed launcher unit')
            limits_ok, cgroup = cgroup_evidence(unit)
            if generation == 1 or checks['per_session_limits']['status'] == 'pass':
                checks['per_session_limits'] = result('pass' if limits_ok else 'fail',
                    'kernel cgroup v2 CPU/memory/swap/PID controls checked for unit and aggregate slice; ' + cgroup)
            deadline = time.monotonic() + 330
            payload = None
            step = f'generation {generation}: waiting for the payload report'

            pane = launch.get('pid') if type(launch.get('pid')) is int else 0

            def quiescent():
                foreign = foreign_processes(expected_uid, cgroup, broker, pane=pane)
                if foreign != []:
                    report['foreign_processes'] = [{'pid': pid, 'where': where, 'cgroup': path}
                                                   for pid, where, path in foreign or []]
                    checks['trusted_launcher'] = result('fail', 'other processes of the account ran outside its '
                        'container during qualification ('
                        + ('unobservable' if foreign is None else f'{len(foreign)}: {describe(foreign)}') + '); '
                        'they could forge payload evidence: stop its sessions and rerun')
                    raise RuntimeError('account not quiescent')
            while time.monotonic() < deadline:
                quiescent()
                try:
                    artifact = call(account, 'read_artifact', {'path': checkout, 'name': 'qualification.json'})
                    if artifact['size'] > 65536:
                        raise ValueError('oversized payload report')
                    candidate = payload_report(json.loads(base64.b64decode(artifact['data'], validate=True)))
                    if candidate['nonce'] == challenge and candidate['nonce'] not in nonces:
                        if candidate['uid'] != container_uid:  # this run's report: it can't become right by waiting
                            checks['trusted_launcher'] = result('fail', 'the payload ran as an unexpected uid')
                            raise Stopped(f"the payload report for this run says uid {candidate['uid']}, not uid "
                                          f'{container_uid}, the container uid the launcher maps for {account}')
                        payload = candidate
                        break
                except RuntimeError:
                    pass  # artifact may not have been written yet
                time.sleep(1)
            if payload is None:
                raise RuntimeError('payload report timed out')
            quiescent()
            nonces.append(payload['nonce'])
            for name in MANDATORY:
                if name not in ('trusted_launcher', 'per_session_limits', 'restart_stop_cleanup', 'image_store'):
                    # Preserve failures from the previous generation.
                    if generation == 1 or checks[name]['status'] == 'pass':
                        checks[name] = payload['checks'][name]
            report.setdefault('generations', []).append({'generation': generation, 'nonce': payload['nonce'],
                                                        'elapsed_ms': round((time.monotonic() - started) * 1000)})
            step = f'generation {generation}: stop and cleanup'
            stopped = call(account, 'kill', {'tmux_name': tmux_name, 'acc_id': session_id, 'container': container})
            if stopped.get('container', {}).get('stopped') is not True:
                clean = False
                report['cleanup_detail'] = str(stopped.get('container', {}).get('error') or 'not stopped')[:300]
                raise RuntimeError('launcher cleanup failed; capacity remains reserved')
            launched = False
        checks['restart_stop_cleanup'] = result('pass', 'two fresh container generations; launcher verified whole-unit '
                                               'stop and automatic container removal after each')
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        # RuntimeError and Stopped carry this file's own fixed wording; other errors only their type.
        detail = str(error) if isinstance(error, (Stopped, RuntimeError)) else type(error).__name__
        report['reason'] = str(error) if isinstance(error, Stopped) else 'qualification stopped: ' + detail
        report['stopped_at'] = step
        # Failing payload rows are copied and never stop the run, so this is a lifecycle failure of its own.
        checks['restart_stop_cleanup'] = result('fail', f'did not complete both generations: stopped at {step} '
                                               f'({detail}), after {len(nonces)} completed generation(s); failing '
                                               'payload rows never stop the run')
    finally:
        if launched and grant:
            try:
                stopped = call(account, 'kill', {'tmux_name': tmux_name, 'acc_id': session_id,
                               'container': {'generation': grant['generation'], 'policy_id': plan['policy_id']}})
                clean = stopped.get('container', {}).get('stopped') is True
                if not clean:  # e.g. the launcher names a pause process still holding the runtime dir
                    report['cleanup_detail'] = str(stopped.get('container', {}).get('error') or 'not stopped')[:300]
            except (OSError, RuntimeError, ValueError) as error:
                clean = False
                report['cleanup_detail'] = 'the launcher could not be asked to stop: ' + type(error).__name__
        # Revocation also lets installed reconciliation retry partial/failed cleanup.
        if grant_owned:
            (grants / (session_id + '.json')).unlink(missing_ok=True)
        report['cleanup_verified'] = clean
    report['go_phase_1'] = clean and verdict(checks, trusted_launcher=checks['trusted_launcher']['status'] == 'pass')
    report['status'] = 'go' if report['go_phase_1'] else 'no-go'
    return report
