"""Uninstalled root edge helper. No caller-selected files, programs, sockets or routes.

Root provisions policy.json, base.json and device-ca.pem in /etc/acc-remote-access;
base.json is the complete public-OFF Caddy JSON, reviewed with published sites. During a
planned authority rollover root also provisions device-ca-previous.pem (the old CA).
Only the designated HTTPS server receives a fixed device gate and ACC route.
"""
from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from acc.dns_helper import DNSHelper, Refused, caddy_lock, open_root, trusted

MAX_MESSAGE = 1024 * 1024
STATE = '/var/lib/acc-remote-access'
POLICY = '/etc/acc-remote-access'
ADMIN = '/run/caddy/admin.sock'
EDGE_HEADER = 'X-Acc-Edge-Client'
NATIVE_HEADER = 'X-Acc-Native-Edge'
# Caddy's subject placeholder is Go's RFC 2253-like string of the verified leaf's subject.
# Go orders named attributes itself; accept either order, nothing else.
MACHINE_SUBJECT = r'^(CN=[0-9a-f]{16,32},OU=acc-machine|OU=acc-machine,CN=[0-9a-f]{16,32})$'
MACHINE_PATHS = ['/api/nodes/connect', '/api/nodes/bundle', '/api/git/*']


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def revision(config):
    return hashlib.sha256(canonical(config).encode()).hexdigest()


class RemoteAccessHelper(DNSHelper):
    def __init__(self, directory_fd, policy, base, ca, runner, *, owner=0, upstream='unix//run/accd.sock'):
        super().__init__(directory_fd, policy, runner, owner=owner)
        # The current authority first; a second only during a planned rollover (device-ca-previous.pem).
        self.cas = [pem for pem in ([ca] if isinstance(ca, str) else list(ca)) if pem]
        self.base, self.upstream = base, upstream

    def read(self, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise Refused('unsafe_file') from None
        try:
            trusted(os.fstat(fd), self.owner)
            raw = os.read(fd, MAX_MESSAGE + 1)
            if len(raw) > MAX_MESSAGE:
                raise Refused('oversize_file')
            return raw.decode()
        finally:
            os.close(fd)

    def render(self, hostname, leaves):
        if (not isinstance(hostname, str) or len(hostname) > 253
                or not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,63}', hostname)
                or hostname not in self.policy.get('hostnames', [])
                or hostname in canonical(self.base)):
            raise Refused('hostname_not_provisioned')
        if not isinstance(leaves, list) or len(leaves) > 200:
            raise Refused('invalid_leaves')
        cas = [x509.load_pem_x509_certificate(pem.encode()) for pem in self.cas]
        if not 1 <= len(cas) <= 2 or len({c.public_bytes(serialization.Encoding.DER) for c in cas}) != len(cas):
            raise Refused('invalid_authorities')
        encoded, issuers = [], set()
        native = False
        for pem in leaves:
            if not isinstance(pem, str) or len(pem) > 8192:
                raise Refused('invalid_leaf')
            cert = x509.load_pem_x509_certificate(pem.encode())
            for index, ca in enumerate(cas):
                try:
                    cert.verify_directly_issued_by(ca)
                except (ValueError, TypeError, InvalidSignature):
                    continue
                issuers.add(index)
                break
            else:
                raise Refused('invalid_leaf')
            if (cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                    or set(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) != {ExtendedKeyUsageOID.CLIENT_AUTH}):
                raise Refused('invalid_leaf')
            native = native or 'acc-native' in [a.value for a in cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)]
            encoded.append(base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode())
        native_key = self.policy.get('native_edge_key')
        if native and (not isinstance(native_key, str) or not re.fullmatch('[0-9a-f]{64}', native_key)):
            raise Refused('native_edge_not_provisioned')
        if len(set(encoded)) != len(encoded):
            raise Refused('duplicate_leaf')
        config = copy.deepcopy(self.base)
        server = config['apps']['http']['servers'][self.policy['server']]
        server.update(strict_sni_host=True, protocols=['h1', 'h2'])
        # Skip only this hostname (its certificate comes from the root base); other sites keep certificates and redirects.
        automatic = server.setdefault('automatic_https', {})
        if not automatic.get('disable'):
            automatic['skip'] = sorted(set(automatic.get('skip', [])) | {hostname})
        config['apps'].setdefault('tls', {})['session_tickets'] = {'disabled': True}
        # Only authorities that issued a listed leaf (the current one when none is listed): the old one leaves the pool
        # on the first apply after its last identity is renewed. The leaf allow list remains the gate either way.
        pool = [cas[i] for i in sorted(issuers or {0})]
        gate = {'mode': 'require_and_verify',
                'trusted_ca_certs': [base64.b64encode(ca.public_bytes(serialization.Encoding.DER)).decode() for ca in pool],
                'verifiers': [{'verifier': 'leaf', 'TrustedLeafCerts': [{'Raw': raw} for raw in sorted(encoded)]}]}
        server['tls_connection_policies'].insert(0, {'match': {'sni': [hostname]}, 'client_authentication': gate})
        # Bootstrap/issuance and previews have no public exception, for any identity. Machine identities (the signer's
        # OU) reach only their link, update download and git backups; browser identities never reach those.
        # Strip untrusted identity assertions; the edge alone sets the client fingerprint accd checks per request.
        # Caddy replaces X-Forwarded-For/-Proto/-Host itself and applies deletions after sets: keep its values.
        proxy = {'handler': 'reverse_proxy', 'upstreams': [{'dial': self.upstream}],
                 'headers': {'request': {'set': {EDGE_HEADER: ['{http.request.tls.client.fingerprint}']},
                                         'delete': ['X-Acc-Device-*', 'Forwarded', 'X-Real-Ip']}}}
        if native:
            proxy['headers']['request']['set'][NATIVE_HEADER] = [native_key]
        machine = {'vars_regexp': {'{http.request.tls.client.subject}': {'pattern': MACHINE_SUBJECT}}}
        denied = {'handler': 'static_response', 'status_code': 403}
        server['routes'].insert(0, {'match': [{'host': [hostname]}], 'terminal': True, 'handle': [
            {'handler': 'subroute', 'routes': [
                {'match': [{'path': MACHINE_PATHS, **machine}], 'handle': [proxy], 'terminal': True},
                {'match': [{'path': ['/pair-device', '/pair-device/*', '/api/device-pairing/*', '/api/devices/pairings*',
                                      '/pair-mobile*', '/api/mobile/v1/pairings*', '/api/mobile/v1/claim*',
                                      '/api/mobile/v1/credential*', '/api/mobile/v1/devices/*/rotate*',
                                      '/api/nodes*', '/api/git*', '/api/preview*', '/preview*', '/install.sh', '/bundle']}],
                 'handle': [denied], 'terminal': True},
                {'match': [machine], 'handle': [denied], 'terminal': True},
                {'handle': [proxy]}]}]})
        return config

    def change(self, config):
        """What Apply adds to the root base: never the base itself, which holds DNS credentials and other sites."""
        server = config['apps']['http']['servers'][self.policy['server']]
        route = copy.deepcopy(server['routes'][0])
        def redact(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == NATIVE_HEADER:
                        value[key] = ['[redacted]']
                    else:
                        redact(child)
            elif isinstance(value, list):
                for child in value:
                    redact(child)
        redact(route)
        return {'base_revision': revision(self.base), 'server': self.policy['server'],
                'server_settings': {k: server[k] for k in ('strict_sni_host', 'protocols', 'automatic_https')},
                'tls_session_tickets': config['apps']['tls']['session_tickets'],
                'tls_connection_policy': server['tls_connection_policies'][0], 'route': route}

    def handle(self, message):
        if not isinstance(message, dict) or len(canonical(message).encode()) > MAX_MESSAGE:
            raise Refused('invalid_message')
        action = message.get('action')
        fields = {'review': {'action', 'hostname', 'leaves'}, 'apply': {'action', 'hostname', 'leaves', 'revision'},
                  'off': {'action'}, 'status': {'action'}}
        if action not in fields or set(message) != fields[action]:
            raise Refused('invalid_message')
        if action in ('review', 'apply'):
            config = self.render(message['hostname'], message['leaves'])
            rev = revision(config)
            if action == 'review':
                return {'configuration': self.change(config), 'revision': rev, 'devices_allowed': len(message['leaves'])}
            if self.policy.get('qualified') is not True:
                raise Refused('release_unqualified')
            if message['revision'] != rev:
                raise Refused('review_changed')
        else:
            config, rev = copy.deepcopy(self.base), revision(self.base)
        trusted(os.fstat(self.fd), self.owner, True)
        if os.fstat(self.fd).st_mode & 0o777 != 0o700:
            raise Refused('unsafe_directory')
        # Serialize status with writes; a process death leaves a durable unknown result.
        self.read('.lock')
        lock = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
        try:
            trusted(os.fstat(lock), self.owner)
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self.read('state.json') or '{"enabled":false,"last_applied_revision":null,"devices_allowed":0,"last_reload_result":"never"}')
            if action == 'status':
                current = self.read('current.json')
                if current and state['last_reload_result'] == 'verified' and not self.runner('verify', json.loads(current)):
                    state.update(enabled=None, last_reload_result='configuration drift')
                    self.write('state.json', canonical(state))
                return state
            self.read('candidate.json')
            # A reviewed snapshot must never clobber concurrent DNS or published-site changes. Caddy may still run
            # the generation before a failed or interrupted reload (previous.json): it is ours, so OFF replaces it
            # instead of refusing it as drift and leaving a revoked leaf allowed.
            known = [json.loads(raw) for raw in (self.read('current.json'), self.read('previous.json')) if raw] or [self.base]
            if action == 'off':
                known.append(self.base)
            live = next((candidate for candidate in known if self.runner('verify', candidate)), None)
            if live is None:
                raise Refused('configuration_drift')
            self.write('previous.json', canonical(live))
            self.write('candidate.json', canonical(config))
            if not self.runner('validate', config):
                raise Refused('validation_failed')
            state.update(enabled=None, last_reload_result='pending')
            self.write('state.json', canonical(state))
            self.write('current.json', canonical(config))
            try:
                if not self.runner('reload', config) or not self.runner('verify', config):
                    raise Refused('reload_failed')
            except Exception:
                # Never restore a policy containing a revoked leaf. Attempt the reviewed OFF base instead.
                state.update(enabled=None, last_reload_result='failed')
                try:
                    self.write('current.json', canonical(self.base))
                    if self.runner('reload', self.base) and self.runner('verify', self.base):
                        state.update(enabled=False, devices_allowed=0, hostname='')
                finally:
                    self.write('state.json', canonical(state))
                raise Refused('reload_failed') from None
            state.update(enabled=action == 'apply', hostname=message.get('hostname', ''),
                         devices_allowed=len(message.get('leaves', [])), last_applied_revision=rev, last_reload_result='verified')
            self.write('state.json', canonical(state))
            return state
        finally:
            os.close(lock)


def run_caddy(action, config, *, deadline=None):
    remaining = 5 if deadline is None else min(5, deadline - time.monotonic())
    if remaining <= 0:
        return False
    if action in ('validate', 'reload'):
        args = ['/usr/bin/caddy', action, '--config', '-']
        if action == 'reload':
            args += ['--address', 'unix/' + ADMIN]
        return subprocess.run(args, input=canonical(config).encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              user='caddy', group='caddy', extra_groups=[], timeout=remaining, env={'PATH': '/usr/bin:/bin', 'HOME': '/var/lib/caddy'}).returncode == 0
    if action == 'verify':
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(remaining)
            sock.connect(ADMIN)
            sock.sendall(b'GET /config/ HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
            response = http.client.HTTPResponse(sock)
            response.begin()
            raw = response.read(MAX_MESSAGE + 1)
            return response.status == 200 and len(raw) <= MAX_MESSAGE and json.loads(raw) == config
    raise Refused('invalid_operation')


def main():
    """Socket-activated, one bounded JSON line; no argv/env overrides."""
    policy_fd = state_fd = None
    try:
        if len(sys.argv) != 1 or os.geteuid() != 0:
            raise Refused('invalid_invocation')
        policy_fd, state_fd = open_root(POLICY), open_root(STATE)
        reader = RemoteAccessHelper(policy_fd, {}, {}, '', None)
        policy = json.loads(reader.read('policy.json') or '{}')
        from acc.peercred import peer_uid
        with socket.socket(fileno=os.dup(0)) as peer:
            if peer_uid(peer) != policy['accd_uid']:
                raise Refused('untrusted_peer')
        raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
            raise Refused('invalid_message')
        base = json.loads(reader.read('base.json') or '{}')
        if base.get('admin', {}).get('listen') != 'unix/' + ADMIN:
            raise Refused('invalid_base')
        message = json.loads(raw)
        ca = ([reader.read('device-ca.pem') or '', reader.read('device-ca-previous.pem') or '']
              if message.get('action') in ('review', 'apply') else [])
        with caddy_lock(timeout=3):
            deadline = time.monotonic() + 20
            result = RemoteAccessHelper(state_fd, policy, base, ca,
                lambda action, config: run_caddy(action, config, deadline=deadline)).handle(message)
        print(canonical(result))
        return 0
    except Exception:
        print('{"error":"remote_access_helper_refused"}')
        return 1
    finally:
        for fd in (policy_fd, state_fd):
            if fd is not None:
                os.close(fd)


if __name__ == '__main__':
    sys.exit(main())
