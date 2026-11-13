"""Portable, closed capability sets shared by accd and the execution broker.

None means unconstrained by this grant, [] means no authority. Machine policy
and operation validation remain additional ceilings. Folders name project data,
not the runtime's read-only system image; 'workspace' is the managed checkout.
"""
from __future__ import annotations

import ipaddress
import posixpath
import re
import time

FIELDS = ('folders', 'network', 'installs', 'ports', 'providers', 'models', 'expires_at')


class Refused(ValueError):
    pass


def normalize(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(FIELDS):
        raise Refused('unknown session capability dimension')
    result = dict.fromkeys(FIELDS)
    for key, items in value.items():
        if items is None:
            continue
        if key == 'expires_at':
            if type(items) is not int or not 0 < items < 253402300800:
                raise Refused('expires_at must be a positive UTC Unix timestamp')
            result[key] = items
            continue
        if not isinstance(items, list) or len(items) > 256:
            raise Refused(f'{key} must be a bounded list or null')
        for item in items:
            valid = False
            if key == 'ports':
                valid = type(item) is int and 1 <= item <= 65535
            elif isinstance(item, str) and 0 < len(item) <= 512:
                if key == 'folders':
                    valid = item == 'workspace' or (item.startswith('/') and '\\' not in item
                        and posixpath.normpath(item) == item and not any(ord(c) < 32 for c in item))
                elif key == 'network':
                    try:
                        ipaddress.ip_address(item)
                        valid = True
                    except ValueError:
                        valid = bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', item)) and '..' not in item
                elif key in ('installs', 'providers'):
                    valid = bool(re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', item))
                elif key == 'models':
                    valid = bool(re.fullmatch(r'[a-z][a-z0-9_-]{0,39}:[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,159}', item))
            if not valid:
                raise Refused(f'invalid {key} capability')
        result[key] = sorted(set(items))
    return result


def _under(path, root):
    return path == root or root != 'workspace' and path.startswith(root.rstrip('/') + '/')


def meet(left, right):
    left, right = normalize(left), normalize(right)
    result = {}
    for key in FIELDS:
        a, b = left[key], right[key]
        if a is None or b is None:
            result[key] = b if a is None else a
        elif key == 'expires_at':
            result[key] = min(a, b)
        elif key == 'folders':
            # Hierarchical roots intersect by retaining the narrower root.
            result[key] = sorted({p for p in a for q in b if _under(p, q)}
                                 | {q for p in a for q in b if _under(q, p)})
        else:
            result[key] = sorted(set(a) & set(b))
    return result


def require(caps, dimension, value=None, *, parent=None, now=None):
    caps = normalize(caps)
    prefix = f'limited by {parent}: ' if parent else 'session capability: '
    timestamp = time.time() if now is None else now
    if caps['expires_at'] is not None and timestamp >= caps['expires_at']:
        raise Refused(prefix + 'time limit ended')
    if dimension == 'expires_at':
        return
    allowed = caps[dimension]
    if allowed is None:
        return
    okay = (any(_under(value, root) for root in allowed) if dimension == 'folders'
            and isinstance(value, str) else value in allowed)
    if not okay:
        raise Refused(prefix + f'{dimension} permission refused')


def launch(caps, provider, model, *, container=False, parent=None, now=None):
    """Final executor barrier, including unsupported sandbox configurations.

    Native arbitrary processes cannot be given enforceable folder, egress,
    account-install or host-port allow-lists by the existing executor. Refuse that generation entirely.
    Container data mounts are workspace-only; its network namespace is offline
    except for the per-session HTTPS gateway, which enforces the network list.
    """
    caps = normalize(caps)
    require(caps, 'providers', provider, parent=parent, now=now)
    require(caps, 'models', f'{provider}:{model}' if model else None, parent=parent, now=now)
    prefix = f'limited by {parent}: ' if parent else 'session capability: '
    for dimension in ('folders', 'network', 'installs', 'ports'):
        if caps[dimension] is not None and not container:
            raise Refused(prefix + f'native executor cannot enforce {dimension}; use a qualified container')
    if container and caps['folders'] is not None:
        require(caps, 'folders', 'workspace', parent=parent, now=now)
    return caps
