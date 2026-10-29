"""Session-only HTTPS CONNECT gateway. The container itself keeps network=none.

No runtime operations, credentials in URLs, forwarding headers, or general HTTP.
DNS answers are all checked, then a numeric address is dialed without resolving
again. A failed or restarted broker closes the only path out of the namespace.
"""
import errno
import ipaddress
import json
import os
import re
import select
import socket
import stat
import struct
import threading
import time
from dataclasses import dataclass

# Pin mandatory special-use ranges explicitly as well as checking is_global:
# classifications differ between the host Python and the pinned image's Python.
_DENIED = tuple(ipaddress.ip_network(value) for value in (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
    '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.88.99.0/24', '192.168.0.0/16',
    '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4',
    '::/96', '::ffff:0:0/96', '64:ff9b::/96', '64:ff9b:1::/48', '2001::/23',
    '2001:db8::/32', '2002::/16', '3ffe::/16', '3fff::/20', 'fc00::/7', 'fe80::/10', 'fec0::/10', 'ff00::/8',
    # Cloud platform endpoints on public addresses (the others are link-local): Azure's WireServer and DNS.
    '168.63.129.16/32',
))


class Denied(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    deny_hosts: tuple = ()
    deny_networks: tuple = ()

    def __post_init__(self):
        if (len(self.deny_hosts) > 128 or len(self.deny_networks) > 128
                or any(not isinstance(h, str) or not re.fullmatch(r'[a-z0-9.-]{1,253}', h) for h in self.deny_hosts)):
            raise ValueError('invalid egress exclusions')
        for network in self.deny_networks:
            ipaddress.ip_network(network, strict=True)

    def permits(self, address):
        try:
            if not isinstance(address, str) or '%' in address:
                return False
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast or ip.is_reserved:
                return False
            if isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local:
                return False
            return not any(ip in network for network in _DENIED) and not any(
                ip in ipaddress.ip_network(network) for network in self.deny_networks)
        except ValueError:
            return False


def authority(value):
    if not isinstance(value, str) or len(value) > 260 or any(c in value for c in '@%/\\\r\n\t '):
        raise Denied('invalid destination')
    match = re.fullmatch(r'(?:\[([0-9a-fA-F:]+)\]|([A-Za-z0-9.-]+)):443', value)
    if not match:
        raise Denied('only HTTPS on port 443 is supported')
    host = (match[1] or match[2]).lower().rstrip('.')
    if not host or '..' in host or host.startswith('.'):
        raise Denied('invalid destination')
    return host


def host_networks(proc='/proc/net'):
    """The host's own addresses and every network it reaches without a router, read on each connection.

    The gateway dials from the host's namespace: a publicly addressed LAN (usual for IPv6) or the host's own
    public address would otherwise pass every special-use check and reach the machine or its neighbours. Routes
    shorter than /8 are VPN full-tunnel halves, not neighbours. Unreadable or oversized routing state denies
    everything.
    """
    networks = []

    def lines(name, *, required):
        try:
            with open(f'{proc}/{name}', encoding='ascii') as file:
                contents = file.read((1 << 22) + 1)
                if len(contents) > 1 << 22:
                    raise Denied('host routes unavailable')
                return contents.splitlines()
        except FileNotFoundError:
            if required:
                raise Denied('host routes unavailable') from None
            return []  # IPv6 disabled: no IPv6 neighbours

    try:
        for line in lines('route', required=True)[1:]:
            fields = line.split()
            gateway, mask = int(fields[2], 16), int(fields[7], 16)
            prefix = bin(mask).count('1')
            if gateway == 0 and prefix >= 8:
                networks.append(ipaddress.ip_network(
                    (ipaddress.IPv4Address(bytes.fromhex(fields[1])[::-1]), prefix), strict=False))
        # fib_trie lists every table; /proc/net/route only the main one. The host's own addresses and local ranges
        # (AnyIP) are LOCAL routes of any length, and policy routing can put an on-link network in another table.
        last = None
        for line in lines('fib_trie', required=True):
            text = line.strip()
            if text.startswith('|--'):
                last = text[3:].split()[0]
            elif text.startswith('+--') or text.endswith(':'):
                last = None
            elif last and (entry := re.match(r'/(\d+) (\S+) (\S+)', text)):
                prefix, scope, kind = int(entry[1]), entry[2], entry[3]
                if kind == 'LOCAL' or kind == 'UNICAST' and scope == 'link' and prefix >= 8:
                    networks.append(ipaddress.ip_network(f'{last}/{prefix}', strict=False))
        for line in lines('if_inet6', required=False):
            fields = line.split()
            address = ipaddress.IPv6Address(bytes.fromhex(fields[0]))
            networks += [ipaddress.ip_network((address, int(fields[2], 16)), strict=False), ipaddress.ip_network((address, 128))]
        for line in lines('ipv6_route', required=False):
            fields = line.split()
            prefix, nexthop = int(fields[1], 16), int(fields[4], 16)
            if nexthop == 0 and prefix >= 8:
                networks.append(ipaddress.ip_network((ipaddress.IPv6Address(bytes.fromhex(fields[0])), prefix), strict=False))
    except (OSError, ValueError, IndexError):
        raise Denied('host routes unavailable') from None
    return networks


def _resolve(host, port):
    return list({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def _dial(address, port):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    stream = socket.socket(family, socket.SOCK_STREAM)
    try:
        stream.settimeout(5)
        stream.connect((address, port))
        return stream
    except BaseException:
        stream.close()
        raise


def connect(policy, destination, *, resolve=_resolve, dial=_dial, local=host_networks):
    host = authority(destination)
    if any(host == denied or host.endswith('.' + denied) for denied in policy.deny_hosts):
        raise Denied('control-plane destination denied')
    try:
        # Literals are checked directly. DNS is repeated for every connection.
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = resolve(host, 443)
    if not addresses or len(addresses) > 8 or any(not policy.permits(ip) for ip in addresses):
        raise Denied('non-public or excluded destination denied')
    neighbours = local()
    if any(ipaddress.ip_address(ip) in network for ip in addresses for network in neighbours):
        raise Denied('this host or a directly connected network denied')
    return dial(addresses[0], 443)


# A process-wide bound also covers many concurrent sessions.
_CAPACITY = threading.BoundedSemaphore(32)


def relay(left, right):
    """Bounded, opaque TLS transport; never inspect or log content."""
    deadline = time.monotonic() + 300
    remaining = 64 << 20
    while remaining > 0 and time.monotonic() < deadline:
        readable, _, _ = select.select([left, right], [], [], min(30, max(0, deadline-time.monotonic())))
        if not readable:
            return
        for source in readable:
            data = source.recv(min(65536, remaining))
            if not data:
                return
            remaining -= len(data)
            (right if source is left else left).sendall(data)


def read_header(client):
    """Bound the entire header wait, including a peer sending one byte at a time."""
    deadline = time.monotonic() + 5
    header = bytearray()
    while len(header) <= 8192:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Denied('header deadline exceeded')
        client.settimeout(remaining)
        byte = client.recv(1)
        if not byte:
            raise Denied('incomplete header')
        header.extend(byte)
        if header == b'ACC-BOOTSTRAP\n' or header.endswith(b'\r\n\r\n'):
            return header
    raise Denied('header too large')


class Gateway:
    """One run directory, one generation. Bootstrap is root-only via SO_PEERCRED.

    A same-account agent can request public connections, but cannot turn this
    socket into a launcher, a credential broker, or a LAN bypass.
    """
    def __init__(self, path, session_id, generation, provider, credentials, policy, *,
                 control_uid=0, enabled=True, policy_digest='', connector=connect):
        self.path = path
        self.metadata = dict(session_id=session_id, generation=generation, provider=provider,
                             credentials=credentials, policy_digest=policy_digest)
        self.policy, self.control_uid, self.enabled = policy, control_uid, enabled
        self.connector = connector
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clients = set()
        self._capacity = threading.BoundedSemaphore(4)

    def __enter__(self):
        self.listener = socket.socket(socket.AF_UNIX)
        self.directory_fd = None
        try:
            self.directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            address = f'/proc/self/fd/{self.directory_fd}/{self.path.name}'
            try:
                self.listener.bind(address)
            except OSError as collision:
                if collision.errno != errno.EADDRINUSE:
                    raise
                previous = os.stat(self.path.name, dir_fd=self.directory_fd, follow_symlinks=False)
                if (not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.getuid()
                        or previous.st_mode & 0o077):
                    raise collision
                # A broker crash leaves this session's dead inode. Never remove
                # a live listener, link, regular file or a different account's socket.
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(0.2)
                    try:
                        probe.connect(address)
                    except OSError as failure:
                        if failure.errno != errno.ECONNREFUSED:
                            raise collision from None
                    else:
                        raise collision
                current = os.stat(self.path.name, dir_fd=self.directory_fd, follow_symlinks=False)
                if (previous.st_dev, previous.st_ino) != (current.st_dev, current.st_ino):
                    raise collision
                os.unlink(self.path.name, dir_fd=self.directory_fd)
                self.listener.bind(address)
            os.chmod(self.path.name, 0o600, dir_fd=self.directory_fd, follow_symlinks=False)
            self.identity = os.stat(self.path.name, dir_fd=self.directory_fd, follow_symlinks=False)
            self.listener.listen(8)
            self.listener.settimeout(0.2)
            self.thread = threading.Thread(target=self._serve, daemon=True)
            self.thread.start()
        except BaseException:
            self.listener.close()
            if self.directory_fd is not None:
                os.close(self.directory_fd)
            raise
        return self

    def _serve(self):
        while not self._stop.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not _CAPACITY.acquire(False):
                client.close(); continue
            if not self._capacity.acquire(False):
                _CAPACITY.release(); client.close(); continue
            with self._lock:
                self._clients.add(client)
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        upstream = None
        try:
            client.settimeout(5)
            # Do not consume the initial TLS bytes after the bounded header.
            header = read_header(client)
            if header == b'ACC-BOOTSTRAP\n':
                _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                with self._lock:
                    if uid != self.control_uid or self.metadata['credentials'] is None or self._stop.is_set():
                        raise Denied('bootstrap requires launcher')
                    reply = json.dumps(self.metadata).encode() + b'\n'
                    self.metadata['credentials'] = None  # one trusted handoff, no refresh service
                client.sendall(reply)
                return
            line = header.decode('ascii').split('\r\n')[0].split(' ')
            if not self.enabled or len(line) != 3 or line[0] != 'CONNECT' or line[2] != 'HTTP/1.1':
                raise Denied('HTTPS gateway only')
            client.settimeout(5)
            if self._stop.is_set():
                return
            upstream = self.connector(self.policy, line[1])
            with self._lock:
                if self._stop.is_set():
                    return
                self._clients.add(upstream)
            client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            relay(client, upstream)
        except (OSError, ValueError, UnicodeError):
            try:
                client.sendall(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
        finally:
            with self._lock:
                self._clients.discard(client)
                self._clients.discard(upstream)
            client.close()
            if upstream is not None:
                upstream.close()
            self._capacity.release(); _CAPACITY.release()

    def close(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self.listener.close()
        with self._lock:
            for client in self._clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                client.close()
        self.thread.join(1)
        try:
            info = os.stat(self.path.name, dir_fd=self.directory_fd, follow_symlinks=False)
            if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == (self.identity.st_dev, self.identity.st_ino):
                os.unlink(self.path.name, dir_fd=self.directory_fd)
        except FileNotFoundError:
            pass
        os.close(self.directory_fd)
        self.metadata['credentials'] = None

    def __exit__(self, *args):
        self.close()
