"""Access-only credential snapshots, delivered once per session generation. Never copy a provider directory.

The delivery is session-bound; the credential is not: an API key or GitHub token keeps its own authority and
expiry (docs/CONTAINERS.md, "Credential boundary").

This module is also installed, root-owned, beside the privileged launcher. Keep it
stdlib-only. Unsupported rotating sign-ins require a separate qualified adapter.
"""
import json
import os
import re
import stat
import time

LIMIT = 131072
PATHS = {'claude': ('.claude', '.credentials.json'), 'codex': ('.codex', 'auth.json'),
         'copilot': ('.copilot', 'config.json')}


def _token(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 16384 or any(ord(c) < 33 for c in value):
        raise ValueError('unsupported credential format')
    return value


def _narrow(provider, value):
    if not isinstance(value, dict):
        raise ValueError('unsupported credential format')
    if provider == 'claude':
        auth = value.get('claudeAiOauth', {})
        expires = auth.get('expiresAt')
        if 'scopes' in auth and (not isinstance(auth['scopes'], list) or 'user:inference' not in auth['scopes']):
            raise ValueError('unsupported credential scope')
        if type(expires) is not int or expires < (time.time() + 300) * 1000:
            raise ValueError('credential expired or unsupported')
        # Only inference access, never refresh, account history or preferences.
        return {'claudeAiOauth': {'accessToken': _token(auth.get('accessToken')),
                                 'expiresAt': expires, 'scopes': ['user:inference']}}
    if provider == 'codex':
        if value.get('auth_mode') != 'apikey' or 'tokens' in value:
            raise ValueError('unsupported rotating Codex sign-in; use an API key')
        return {'auth_mode': 'apikey', 'OPENAI_API_KEY': _token(value.get('OPENAI_API_KEY'))}
    if provider == 'copilot':
        user = value.get('lastLoggedInUser', {})
        if user.get('host', 'https://github.com') != 'https://github.com':
            raise ValueError('unsupported Copilot host')
        login = user.get('login')
        if not isinstance(login, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', login):
            raise ValueError('unsupported Copilot account')
        key = 'https://github.com:' + login
        token = _token(value.get('authTokens', {}).get(key, {}).get('token'))
        return {'lastLoggedInUser': {'host': 'https://github.com', 'login': login}, 'authTokens': {key: {'token': token}}}
    raise ValueError('unsupported credential provider')


def validate(value, provider, session_id, generation):
    """Revalidate on the trusted side; return the exact, narrow envelope."""
    try:
        if (not isinstance(value, dict) or set(value) != {'version', 'provider', 'session_id', 'generation', 'credentials'}
                or type(value['version']) is not int or value['version'] != 1
                or value['provider'] != provider or value['session_id'] != session_id
                or type(value['generation']) is not int or value['generation'] != generation
                or value['credentials'] != _narrow(provider, value['credentials'])):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid session credential snapshot') from None
    return value


def snapshot(home, provider, session_id, generation, *, owner=None):
    """Read only the selected account's canonical file, using held no-follow dirs."""
    owner = os.getuid() if owner is None else owner
    fd = None
    try:
        if provider not in PATHS:
            raise ValueError('unsupported credential provider')
        # Walk the absolute home without following even an ancestor symlink.
        parts = os.path.abspath(home).split('/')[1:]
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        for part in [*parts, PATHS[provider][0]]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        info = os.fstat(fd)
        if info.st_uid != owner or info.st_mode & 0o022:
            raise ValueError('unsafe credential directory')
        child = os.open(PATHS[provider][1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        os.close(fd); fd = child
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > LIMIT):
            raise ValueError('unsafe credential file')
        raw = os.read(fd, LIMIT+1).decode('utf-8')
        if provider == 'copilot':
            raw = '\n'.join(line for line in raw.splitlines() if not line.lstrip().startswith('//'))
        value = json.loads(raw)
        narrow = _narrow(provider, value)
        return validate({'version': 1, 'provider': provider, 'session_id': session_id,
                         'generation': generation, 'credentials': narrow}, provider, session_id, generation)
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError('unsupported, expired or unsafe provider credentials') from None
    finally:
        if fd is not None:
            os.close(fd)


def integration_policy(entry):
    """Explicit second opt-in inside the root-owned image policy (default absent)."""
    mode = entry.get('credentials')
    egress = entry.get('egress')
    if mode is None and egress is None:
        return None
    if mode != 'readonly-v1':
        raise ValueError('unsupported credential adapter policy')
    if egress is not None:
        import ipaddress
        if (not isinstance(egress, dict) or set(egress) != {'mode', 'deny_hosts', 'deny_networks'}
                or egress['mode'] != 'internet-v1' or not isinstance(egress['deny_hosts'], list)
                or not isinstance(egress['deny_networks'], list)
                or not 1 <= len(egress['deny_hosts']) <= 128 or not 1 <= len(egress['deny_networks']) <= 128
                or any(not isinstance(h, str) or not re.fullmatch(r'[a-z0-9.-]{1,253}', h) for h in egress['deny_hosts'])):
            raise ValueError('explicit control-plane egress exclusions required')
        for network in egress['deny_networks']:
            ipaddress.ip_network(network, strict=True)
    return {'credentials': mode, 'egress': egress}


def policy_digest(entry):
    import hashlib
    binding = {'image': entry.get('image'), 'providers': entry.get('providers'),
               'network': entry.get('network', 'none'), 'integrations': integration_policy(entry)}
    return hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
