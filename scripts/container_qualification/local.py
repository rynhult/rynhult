"""Current-account rootless image qualification; never substitutes for trusted launcher evidence."""
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from phase1 import MANDATORY, result


def container_command(engine, base, image, session, challenge, uid, gid):
    run = '/qualification/input'
    policy = (base / 'inputs/guardrails.json').read_bytes() if (base / 'inputs/guardrails.json').exists() else b''
    return [*engine, 'run', '--detach', '--rm', '--pull=never', '--read-only', '--read-only-tmpfs=false',
            '--default-mounts-file=/dev/null', '--events-backend=none', '--hooks-dir=/etc/acc/containers/hooks.d',
            '--image-volume=ignore', '--no-healthcheck', '--log-driver=none', '--network=none',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--userns=keep-id', f'--user={uid}:{gid}',
            '--pid=private', '--ipc=private', '--cgroupns=private', '--http-proxy=false',
            '--pids-limit=512', '--memory=4096m', '--memory-swap=4096m', '--cpus=2',
            f'--cidfile={base / (challenge + ".cid")}', '--tmpfs=/tmp:rw,nosuid,nodev,size=1g,mode=1777',
            f'--mount=type=bind,src={base / "checkout"},dst=/workspace',
            f'--mount=type=bind,src={base / "inputs"},dst={run},ro',
            f'--mount=type=bind,src={base / "events.jsonl"},dst={run}/events.jsonl',
            f'--mount=type=bind,src={base / "statusline.json"},dst={run}/statusline.json',
            f'--mount=type=bind,src={base / "inputs/.acc-launch-contract.json"},dst=/run/acc-container/contract.json,ro',
            '--workdir=/workspace', '--env=HOME=/home/acc', '--env=PATH=/usr/local/bin:/usr/bin:/bin',
            f'--env=ACC_RUN_DIR={run}', f'--env=ACC_SESSION_ID={session}',
            '--env=ACC_GUARD_FILES=guardrails.json=' + hashlib.sha256(policy).hexdigest(),
            '--entrypoint=/usr/local/bin/acc-session', image, run + '/session.env',
            '/usr/bin/python3', '/opt/acc/qualification/phase1.py', challenge]


def engine_run(engine, env, *args, timeout=30):
    return subprocess.run([*engine, *args], env=env, capture_output=True, text=True, timeout=timeout)


def qualify(engine, env, image, base):
    from driver import kernel_limits, payload_report

    checks = {name: result('skip', 'image container did not reach this check') for name in MANDATORY}
    checks['trusted_launcher'] = result('skip', 'user-manager prototype; registered broker/root launcher not exercised')
    report = {'version': 1, 'status': 'no-go', 'go_phase_1': False, 'checks': checks, 'image': image,
              'scope': 'rootless local image; synthetic APIs; no trusted-launcher claim'}
    session = uuid.uuid4().hex
    inputs = base / 'inputs'
    inputs.mkdir()
    for name in ('session.env', 'events.jsonl', 'statusline.json'):
        (inputs / name).touch(mode=0o444)
    (inputs / 'guardrails.json').write_text(json.dumps({'rules': {}, 'workspace': '/workspace', 'custom': [
        {'pattern': '^touch /tmp/acc-qualification-forbidden$', 'reason': 'qualification forbidden command'}]}))
    (inputs / 'guardrails.json').chmod(0o444)
    contract = inputs / '.acc-launch-contract.json'
    contract.write_text(json.dumps({'version': 1, 'issuer': 'acc-containers', 'sandbox': 'container',
                                    'network': 'none', 'session_id': session, 'generation': 1}))
    contract.chmod(0o444)
    inputs.chmod(0o555)
    for name in ('events.jsonl', 'statusline.json'):
        (base / name).touch(mode=0o600)
    cleanup = True
    cid = None
    try:
        inventory = engine_run(engine, env, 'image', 'inspect', image)
        if inventory.returncode:
            report['reason'] = 'pinned image is absent from the selected local store; no pull attempted'
            return report
        report['image_size_bytes'] = json.loads(inventory.stdout)[0]['Size']
        for generation in (1, 2):
            challenge = uuid.uuid4().hex
            started = engine_run(engine, env, *container_command(engine, base, image, session, challenge,
                                                                 os.getuid(), os.getgid())[len(engine):], timeout=60)
            cidfile = base / (challenge + '.cid')
            cid = cidfile.read_text().strip() if cidfile.exists() else None
            if started.returncode or not cid or not re.fullmatch('[0-9a-f]{64}', cid):
                report['reason'] = 'rootless container start failed'
                report['runtime_error'] = started.stderr[-3000:]
                checks['restart_stop_cleanup'] = result('fail', 'container did not start')
                break
            inspected = engine_run(engine, env, 'inspect', cid)
            pid = json.loads(inspected.stdout)[0]['State']['Pid']
            relative = next(line.split('::', 1)[1] for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines()
                            if line.startswith('0::'))
            limits = kernel_limits(Path('/sys/fs/cgroup') / relative.lstrip('/'),
                                   {'cpus': 2, 'memory_mib': 4096, 'pids': 512})
            if generation == 1 or checks['per_session_limits']['status'] == 'pass':
                checks['per_session_limits'] = result('pass' if limits else 'fail',
                                                      'kernel CPU/memory/swap/PID controls for the actual payload cgroup')
            deadline = time.monotonic() + 330
            payload = None
            artifact = base / 'checkout/.acc/artifacts/qualification.json'
            while time.monotonic() < deadline:
                if artifact.exists():
                    candidate = payload_report(json.loads(artifact.read_text()))
                    if candidate['nonce'] == challenge and candidate['uid'] == os.getuid():
                        payload = candidate
                        break
                if engine_run(engine, env, 'container', 'exists', cid).returncode == 1:
                    break
                time.sleep(1)
            if payload is None:
                report['reason'] = 'payload exited or timed out without a fresh report'
                checks['restart_stop_cleanup'] = result('fail', report['reason'])
                break
            for name in MANDATORY:
                if name not in ('trusted_launcher', 'per_session_limits', 'restart_stop_cleanup'):
                    if generation == 1 or checks[name]['status'] == 'pass':
                        checks[name] = payload['checks'][name]
            report.setdefault('generations', []).append({'generation': generation, 'nonce': challenge})
            engine_run(engine, env, 'stop', '--time=5', cid, timeout=20)
            absent = engine_run(engine, env, 'container', 'exists', cid).returncode == 1
            if not absent:
                cleanup = False
                break
            cid = None
        else:
            checks['restart_stop_cleanup'] = result('pass', 'two fresh payloads; both stopped and automatically removed')
    except (OSError, KeyError, ValueError, StopIteration, subprocess.SubprocessError) as error:
        report['reason'] = 'local qualification interrupted: ' + type(error).__name__
    finally:
        if cid and re.fullmatch('[0-9a-f]{64}', cid):
            engine_run(engine, env, 'stop', '--time=5', cid, timeout=20)
            cleanup = engine_run(engine, env, 'container', 'exists', cid).returncode == 1
        report['cleanup_verified'] = cleanup
        inputs.chmod(0o755)
    return report
