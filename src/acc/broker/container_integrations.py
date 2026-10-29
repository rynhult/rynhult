"""Broker-owned lifetime for off-by-default container integrations."""
import json
import os
import stat
import threading
from pathlib import Path

from acc.container_credentials import integration_policy, policy_digest, snapshot
from acc.container_egress import Gateway, Policy
from acc.env import environment

from .containers import ContainerError

_lock = threading.RLock()
_gateways = {}


def _read_policy(path, policy_id):
    # Fixed machine policy; no policy path accepted in launch parameters.
    # Missing policy keeps the original offline path. Check absence before the
    # trust walk, so fixture/private prefixes do not turn absence into an opt-in.
    path.lstat()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('unsafe policy parent')
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        os.close(fd); fd = child
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 65536:
            raise ValueError('unsafe container policy')
        document = json.loads(os.read(fd, 65537))
        if document.get('version') != 1:
            raise ValueError('unsupported container policy')
        return document['images'][policy_id]
    finally:
        os.close(fd)


def start(config, session_id, container, provider, run_dir):
    try:
        try:
            entry = _read_policy(config.container_policy, container['policy_id'])
        except FileNotFoundError:
            return  # original offline image: root helper remains authoritative
        options = integration_policy(entry)
        if options is None:
            return
        if environment.get('ACC_CONTAINERS') != '1':
            raise ValueError('ACC_CONTAINERS is disabled')
        credentials = snapshot(Path.home(), provider, session_id, container['generation'])
        egress = options['egress']
        policy = Policy(tuple(egress['deny_hosts']), tuple(egress['deny_networks'])) if egress else Policy()
        gateway = Gateway(run_dir / '.acc-integrations.sock', session_id, container['generation'], provider,
                          credentials, policy, enabled=bool(egress), policy_digest=policy_digest(entry))
        with _lock:
            previous = _gateways.get((config.run_dir, session_id))
            if previous is not None and previous.metadata['generation'] >= container['generation']:
                raise ValueError('session integrations already exist')
            if previous is not None:
                # An older generation's container ended without a broker stop: its gateway must not block (or
                # outlive) the new generation, and its socket path is the one the new gateway binds.
                _gateways.pop((config.run_dir, session_id))
                previous.close()
            gateway.__enter__()
            _gateways[(config.run_dir, session_id)] = gateway
    except (OSError, ValueError, KeyError, TypeError):
        raise ContainerError('container credentials or egress policy refused; canonical sign-in may need renewal') from None


def stop(config, session_id, *, generation=None):
    with _lock:
        gateway = _gateways.get((config.run_dir, session_id))
        if gateway is not None and generation is not None and gateway.metadata['generation'] != generation:
            return
        _gateways.pop((config.run_dir, session_id), None)
    if gateway is not None:
        gateway.close()
