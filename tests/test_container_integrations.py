"""Phase-2 adapters use synthetic sign-ins and fake transports, never host credentials."""
import json
import os
import socket
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize('provider', ['claude', 'codex', 'copilot'])
def test_selected_credentials_are_narrow_and_generation_bound(tmp_path, provider):
    from acc.container_credentials import snapshot, validate
    values = {
        'claude': ('.claude/.credentials.json', {'claudeAiOauth': {'accessToken': 'synthetic-claude-token',
            'refreshToken': 'REFRESH-NEVER-DELIVER', 'expiresAt': int((time.time()+3600)*1000), 'scopes': ['user:inference']}, 'history': 'PRIVATE-HISTORY'}),
        'codex': ('.codex/auth.json', {'auth_mode': 'apikey', 'OPENAI_API_KEY': 'synthetic-openai-key', 'other': 'PRIVATE-HISTORY'}),
        'copilot': ('.copilot/config.json', {'lastLoggedInUser': {'login': 'alex'}, 'authTokens': {
            'https://github.com:alex': {'token': 'synthetic-github-token'},
            'https://github.com:bea': {'token': 'OTHER-PERSON-TOKEN'}}, 'trustedFolders': ['/private'], 'hooks': {'evil': True}}),
    }
    relative, value = values[provider]
    file = tmp_path / relative
    file.parent.mkdir()
    file.write_text(json.dumps(value)); file.chmod(0o600)
    envelope = snapshot(tmp_path, provider, 's12345678', 2, owner=os.getuid())
    encoded = json.dumps(envelope)
    assert 'REFRESH-NEVER-DELIVER' not in encoded and 'PRIVATE-HISTORY' not in encoded and 'OTHER-PERSON' not in encoded
    assert 'trustedFolders' not in encoded and 'evil' not in encoded
    assert validate(envelope, provider, 's12345678', 2) == envelope
    with pytest.raises(ValueError): validate(envelope, provider, 's99999999', 2)
    with pytest.raises(ValueError): validate(envelope, provider, 's12345678', 3)
    with pytest.raises(ValueError): validate(envelope, 'other', 's12345678', 2)


@pytest.mark.parametrize('attack', ['expired', 'refresh_only', 'symlink', 'hardlink', 'writable', 'oversized', 'foreign_owner'])
def test_credential_sources_fail_closed(tmp_path, attack):
    from acc.container_credentials import snapshot
    file = tmp_path / '.claude/.credentials.json'
    file.parent.mkdir()
    value = {'claudeAiOauth': {'accessToken': 'synthetic-token', 'expiresAt': int((time.time()+3600)*1000)}}
    if attack == 'expired': value['claudeAiOauth']['expiresAt'] = 1
    if attack == 'refresh_only': value['claudeAiOauth'] = {'refreshToken': 'DO-NOT-DELIVER'}
    file.write_text(json.dumps(value)); file.chmod(0o600)
    if attack == 'symlink':
        file.rename(file.parent / 'real'); file.symlink_to(file.parent / 'real')
    if attack == 'hardlink': os.link(file, file.parent / 'alias')
    if attack == 'writable': file.chmod(0o666)
    if attack == 'oversized': file.write_text('x' * (131072+1))
    with pytest.raises(ValueError) as error:
        snapshot(tmp_path, 'claude', 's12345678', 1, owner=os.getuid()+1 if attack == 'foreign_owner' else os.getuid())
    assert 'DO-NOT-DELIVER' not in str(error.value)


def test_rotating_codex_signin_requires_explicit_supported_adapter(tmp_path):
    from acc.container_credentials import snapshot
    file = tmp_path / '.codex/auth.json'; file.parent.mkdir()
    file.write_text(json.dumps({'tokens': {'access_token': 'token', 'refresh_token': 'never-copy'}})); file.chmod(0o600)
    with pytest.raises(ValueError, match='unsupported'):
        snapshot(tmp_path, 'codex', 's12345678', 1, owner=os.getuid())


@pytest.mark.parametrize('address', ['127.0.0.1', '10.2.3.4', '172.31.1.2', '192.168.1.2', '169.254.169.254',
    '100.64.0.1', '0.0.0.0', '224.0.0.1', '::1', 'fe80::1', 'fc00::1', 'fec0::1', 'ff02::1',
    '::ffff:127.0.0.1', '64:ff9b::a00:1', '2002:0a00:0001::1', '2001:0:1234::1'])
def test_lan_metadata_and_transition_addresses_are_denied(address):
    from acc.container_egress import Policy
    assert not Policy().permits(address)


def test_resolution_is_checked_and_pinned_for_every_connection():
    from acc.container_egress import Denied, Policy, connect
    calls = []
    addresses = ['8.8.8.8']
    policy = Policy(deny_hosts=('control.example.com',), deny_networks=('9.9.9.9/32',))
    def resolve(host, port): return list(addresses)
    def dial(address, port): calls.append((address, port)); return 'fake-stream'
    assert connect(policy, 'api.example.com:443', resolve=resolve, dial=dial) == 'fake-stream'
    assert calls == [('8.8.8.8', 443)]
    addresses[:] = ['8.8.8.8', '10.0.0.1']
    with pytest.raises(Denied): connect(policy, 'api.example.com:443', resolve=resolve, dial=dial)
    assert len(calls) == 1
    for authority in ['control.example.com:443', '9.9.9.9:443', 'api.example.com:22', 'user@api.example.com:443',
                      '[::1]:443', 'api.example.com:0443', 'api.example.com:443\r\nX: y']:
        with pytest.raises(Denied): connect(policy, authority, resolve=resolve, dial=dial)


def test_gateway_bootstrap_and_connect_use_one_session_only(tmp_path):
    from acc.container_egress import Gateway, Policy
    secret = {'synthetic': 'CANARY'}
    gateway = Gateway(tmp_path / 'gateway.sock', 's12345678', 2, 'claude', secret, Policy(),
                      control_uid=os.getuid(), enabled=False)
    with gateway:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(gateway.path)); client.sendall(b'ACC-BOOTSTRAP\n')
            reply = json.loads(client.makefile('rb').readline())
            assert reply['session_id'] == 's12345678' and reply['generation'] == 2 and reply['credentials'] == secret
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(gateway.path)); client.sendall(b'CONNECT api.example.com:443 HTTP/1.1\r\n\r\n')
            assert b'403' in client.recv(4096)
    assert not gateway.path.exists()


def _image_module(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / 'deploy/containers' / (name+'.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('provider,relative', [('claude', 'claude/.credentials.json'), ('copilot', 'copilot/config.json')])
def test_image_credential_view_does_not_copy_or_write_back(provider, relative, tmp_path):
    runtime = _image_module('session_runtime')
    (tmp_path / 'opt/acc').mkdir(parents=True)
    (tmp_path / 'opt/acc/state-directories.json').write_text(json.dumps(['/tmp/acc-state/claude', '/tmp/acc-state/copilot']))
    (tmp_path / 'opt/acc/state-files.json').write_text('[]')
    (tmp_path / 'run/acc-container').mkdir(parents=True)
    (tmp_path / 'run/acc-container/contract.json').write_text(json.dumps({'integrations': 'readonly-v1', 'provider': provider, 'egress': False}))
    env = runtime.prepare(tmp_path)
    link = tmp_path / 'tmp/acc-state' / relative
    assert link.is_symlink() and os.readlink(link) == '/run/acc-credentials/auth.json'
    assert env['HOME'] == '/home/acc'
    assert 'HTTPS_PROXY' not in env


def test_image_bridge_is_loopback_only_and_has_no_credential_environment(monkeypatch):
    runtime = _image_module('session_runtime')
    opened = []
    class Listener:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, address): opened.append(address)
        def listen(self, count): pass
        def fileno(self): return 42
    monkeypatch.setattr(runtime.socket, 'socket', lambda *args: Listener())
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *args, **kwargs: opened.append((args, kwargs)))
    runtime.start_bridge()
    assert opened[0] == ('127.0.0.1', 18765)
    assert opened[1][1]['env'] == {'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
    assert opened[1][1]['pass_fds'] == (42,)


def test_copilot_jsonc_host_and_selected_account_are_explicit(tmp_path):
    from acc.container_credentials import snapshot
    file = tmp_path / '.copilot/config.json'; file.parent.mkdir()
    value = {'lastLoggedInUser': {'host': 'https://github.com', 'login': 'alex'},
             'authTokens': {'https://github.com:alex': {'token': 'synthetic-token'}}}
    file.write_text('// Copilot CLI settings\n' + json.dumps(value)); file.chmod(0o600)
    result = snapshot(tmp_path, 'copilot', 's12345678', 1)
    assert result['credentials']['lastLoggedInUser']['host'] == 'https://github.com'
    value['lastLoggedInUser']['host'] = 'https://other.example.com'
    file.write_text(json.dumps(value))
    with pytest.raises(ValueError): snapshot(tmp_path, 'copilot', 's12345678', 1)


def test_gateway_denies_unprivileged_bootstrap_and_live_lan_connect(tmp_path):
    from acc.container_egress import Gateway, Policy
    with Gateway(tmp_path / 'deny.sock', 's12345678', 1, 'claude', {'canary': 'NEVER-RETURN'}, Policy(),
                 control_uid=os.getuid()+1) as gateway:
        for data in (b'ACC-BOOTSTRAP\n', b'CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n',
                     b'CONNECT [::1]:443 HTTP/1.1\r\n\r\n', b'GET http://10.0.0.1/ HTTP/1.1\r\n\r\n'):
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(2); client.connect(str(gateway.path)); client.sendall(data)
                reply = client.recv(4096)
                assert b'403' in reply and b'NEVER-RETURN' not in reply


def test_public_gateway_tunnels_only_through_fake_pinned_transport_and_closes_on_stop(tmp_path):
    from acc.container_egress import Gateway, Policy, connect
    transport, remote = socket.socketpair()
    called = []
    def connector(policy, destination):
        return connect(policy, destination, resolve=lambda *args: ['8.8.8.8'],
                       dial=lambda ip, port: called.append((ip, port)) or transport)
    gateway = Gateway(tmp_path / 'https.sock', 's12345678', 1, 'codex', {}, Policy(), connector=connector)
    with gateway:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(2); remote.settimeout(2)
            client.connect(str(gateway.path)); client.sendall(b'CONNECT api.example.com:443 HTTP/1.1\r\n\r\n')
            assert b'200 Connection Established' in client.recv(4096)
            client.sendall(b'fake-TLS-request'); assert remote.recv(4096) == b'fake-TLS-request'
            remote.sendall(b'fake-TLS-response'); assert client.recv(4096) == b'fake-TLS-response'
            gateway.close()
            assert client.recv(4096) == b''
    remote.close()
    assert called == [('8.8.8.8', 443)]


def test_gateway_reclaims_only_a_dead_owned_socket_for_recreation(tmp_path):
    from acc.container_egress import Gateway, Policy
    path = tmp_path / 'stale.sock'
    with socket.socket(socket.AF_UNIX) as dead:
        dead.bind(str(path)); os.chmod(path, 0o600)
    with Gateway(path, 's12345678', 2, 'claude', {}, Policy(), control_uid=os.getuid()):
        with pytest.raises(OSError):
            Gateway(path, 's12345678', 3, 'claude', {}, Policy()).__enter__()
        assert path.exists()
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path)); client.sendall(b'ACC-BOOTSTRAP\n')
            assert json.loads(client.makefile('rb').readline())['generation'] == 2
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path)); client.sendall(b'ACC-BOOTSTRAP\n')
            assert b'403' in client.recv(4096)  # one-shot handoff


def test_gateway_header_has_an_absolute_deadline(monkeypatch):
    from types import SimpleNamespace

    import acc.container_egress as egress
    now = iter([0, 6])
    monkeypatch.setattr(egress, 'time', SimpleNamespace(monotonic=lambda: next(now)))
    client = SimpleNamespace(settimeout=lambda timeout: None, recv=lambda count: pytest.fail('read after deadline'))
    with pytest.raises(egress.Denied): egress.read_header(client)


def test_mandatory_lan_ranges_do_not_depend_on_python_ip_classification(monkeypatch):
    import ipaddress

    import acc.container_egress as egress
    class ClassifiedPublic(ipaddress.IPv4Address):
        @property
        def is_global(self): return True
        @property
        def is_reserved(self): return False
    monkeypatch.setattr(egress.ipaddress, 'ip_address', ClassifiedPublic)
    # The pinned image's Python may classify special-use ranges differently.
    for address in ('10.0.0.1', '172.16.0.1', '192.168.0.1', '169.254.169.254', '100.64.0.1', '192.0.0.8'):
        assert not egress.Policy().permits(address)


def test_bootstrap_digest_binds_pinned_image_and_provider_command():
    from acc.container_credentials import policy_digest
    base = {'credentials': 'readonly-v1', 'image': 'sha256:'+'ab'*32,
            'providers': {'codex': '/usr/local/bin/codex'}}
    digest = policy_digest(base)
    assert digest == policy_digest({**base, 'network': 'none'})
    assert digest != policy_digest({**base, 'image': 'sha256:'+'cd'*32})
    assert digest != policy_digest({**base, 'providers': {'codex': '/usr/local/bin/other'}})



def _proc(tmp_path, *, ipv6=True):
    """A VPS on a public IPv4 /24 with a globally addressed IPv6 LAN, a second on-link IPv6 prefix and a
    full-tunnel VPN's halves. Ordinary public numbers: no special-use check knows they are this host's."""
    def le(address): return bytes(reversed(bytes(int(o) for o in address.split('.')))).hex().upper()
    (tmp_path / 'route').write_text(
        'Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n'
        f"eth0\t{le('0.0.0.0')}\t{le('45.33.32.1')}\t0003\t0\t0\t0\t{le('0.0.0.0')}\t0\t0\t0\n"
        f"eth0\t{le('45.33.32.0')}\t{le('0.0.0.0')}\t0001\t0\t0\t0\t{le('255.255.255.0')}\t0\t0\t0\n"
        f"wg0\t{le('0.0.0.0')}\t{le('0.0.0.0')}\t0001\t0\t0\t0\t{le('128.0.0.0')}\t0\t0\t0\n")
    (tmp_path / 'fib_trie').write_text('Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 45.33.32.7\n        /32 host LOCAL\n'
                                       '     |-- 45.33.32.0\n        /24 link UNICAST\n'
                                       '     |-- 151.101.1.9\n        /32 host LOCAL\n')
    if ipv6:
        (tmp_path / 'if_inet6').write_text('2a0104f8000c0c0000000000000000aa 02 40 00 00 eth0\n')
        zeros = '0' * 32
        (tmp_path / 'ipv6_route').write_text(
            f'2a0104f8000c0d000000000000000000 40 {zeros} 00 {zeros} 00000100 00000001 00000000 00000001 eth1\n'
            f'{zeros} 01 {zeros} 00 {zeros} 00000100 00000001 00000000 00000001 wg0\n')
    return str(tmp_path)


@pytest.mark.parametrize('address', ['45.33.32.7', '45.33.32.50', '151.101.1.9', '2a01:4f8:c:c00::aa',
                                     '2a01:4f8:c:c00::1234', '2a01:4f8:c:d00::99'])
def test_host_and_directly_connected_public_networks_are_denied(tmp_path, address):
    """The gateway dials from the host: its own public addresses and publicly addressed LANs are not the Internet."""
    from acc.container_egress import Denied, Policy, connect, host_networks
    proc, dialled = _proc(tmp_path), []
    assert Policy().permits(address)  # only the host's routing tables reveal it is local
    with pytest.raises(Denied):
        connect(Policy(), 'neighbour.example.net:443', resolve=lambda *a: [address],
                dial=lambda *a: dialled.append(a), local=lambda: host_networks(proc))
    assert dialled == []


def test_vpn_full_tunnel_halves_do_not_block_the_internet_and_unreadable_routes_fail_closed(tmp_path):
    from acc.container_egress import Denied, Policy, connect, host_networks
    proc = _proc(tmp_path, ipv6=False)  # IPv6 disabled: absent if_inet6/ipv6_route is fine
    for address in ('8.8.8.8', '2001:4860:4860::8888'):
        assert connect(Policy(), 'api.example.com:443', resolve=lambda *a, ip=address: [ip], dial=lambda *a: 'stream',
                       local=lambda: host_networks(proc)) == 'stream'
    (tmp_path / 'route').unlink()
    with pytest.raises(Denied):
        connect(Policy(), 'api.example.com:443', resolve=lambda *a: ['8.8.8.8'], dial=lambda *a: 'stream',
                local=lambda: host_networks(proc))


def test_oversized_host_route_inventory_cannot_hide_local_destinations(tmp_path):
    from acc.container_egress import Denied, Policy, connect, host_networks

    proc = _proc(tmp_path, ipv6=False)
    # A complete-looking header consumes the old read bound; the on-link route
    # beyond it must never be silently omitted from the gateway's deny inventory.
    routes = (tmp_path / 'route').read_text()
    (tmp_path / 'route').write_text('x' * (1 << 22) + '\n' + routes)
    dialled = []
    with pytest.raises(Denied, match='host routes unavailable'):
        connect(Policy(), 'neighbour.example.com:443', resolve=lambda *a: ['45.33.32.50'],
                dial=lambda *a: dialled.append(a), local=lambda: host_networks(proc))
    assert dialled == []


@pytest.mark.parametrize('address', ['151.101.64.77', '203.0.114.9', '185.199.108.20'])
def test_local_ranges_and_on_link_routes_in_every_table_are_denied(tmp_path, address):
    """Review CP2-1: /proc/net/route lists only the main table, and the host's local ranges (AnyIP) are prefix
    LOCAL routes. fib_trie lists every table: a public LAN on a policy-routing table, or a local /24, is the host
    or its neighbours, not the Internet."""
    from acc.container_egress import Denied, Policy, connect, host_networks
    proc = _proc(tmp_path, ipv6=False)
    (tmp_path / 'fib_trie').write_text((tmp_path / 'fib_trie').read_text() + (
        'Id 100:\n  +-- 0.0.0.0/0 2 0 2\n     |-- 203.0.114.0\n        /24 link UNICAST\n'
        '     |-- 0.0.0.0\n        /0 universe UNICAST\n'
        'Local:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 185.199.108.0\n        /22 host LOCAL\n'
        '     |-- 151.101.64.0\n        /24 host LOCAL\n'))
    dialled = []
    with pytest.raises(Denied):
        connect(Policy(), 'neighbour.example.net:443', resolve=lambda *a: [address],
                dial=lambda *a: dialled.append(a), local=lambda: host_networks(proc))
    assert dialled == []
    # A table's default route is the Internet, not a neighbour.
    assert connect(Policy(), 'api.example.com:443', resolve=lambda *a: ['8.8.8.8'], dial=lambda *a: 'stream',
                   local=lambda: host_networks(proc)) == 'stream'


def test_cloud_platform_endpoints_on_public_addresses_are_denied():
    """Review CP2-4: Azure's platform endpoint (WireServer and DNS) is a public address on every Azure VM."""
    from acc.container_egress import Policy
    assert not Policy().permits('168.63.129.16')
    assert Policy().permits('168.63.129.17')
