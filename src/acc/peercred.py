"""Who is at the other end of a connected UNIX socket, as the kernel reports it.

The broker gates every connection on the peer's uid, and accd checks who holds a broker socket. Linux reports it with
SO_PEERCRED, macOS with LOCAL_PEERCRED. Anywhere else there is no answer we trust, so callers refuse the connection.
"""
from __future__ import annotations

import socket
import struct
import sys

PLATFORM = sys.platform  # tests switch it to exercise the other branch

# Linux <sys/socket.h>: struct ucred { pid_t pid; uid_t uid; gid_t gid; }
UCRED = struct.Struct("@iII")
# macOS <sys/ucred.h>: struct xucred { u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]; }
SOL_LOCAL = 0
LOCAL_PEERCRED = 0x001
XUCRED_VERSION = 0
XUCRED = struct.Struct("@IIh16I")


class PeerCredError(OSError):
    """The peer's uid is unknown: treat the connection as untrusted."""


def peer_uid(sock) -> int:
    """The uid of the process that holds the other end of `sock` (a connected AF_UNIX stream socket)."""
    if PLATFORM.startswith("linux"):
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, UCRED.size)
        if len(creds) < UCRED.size:
            raise PeerCredError(f"SO_PEERCRED returned {len(creds)} bytes")
        return UCRED.unpack_from(creds)[1]
    if PLATFORM == "darwin":
        creds = sock.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, XUCRED.size)
        if len(creds) < 8:  # at least cr_version and cr_uid
            raise PeerCredError(f"LOCAL_PEERCRED returned {len(creds)} bytes")
        version, uid = struct.unpack_from("@II", creds)
        if version != XUCRED_VERSION:
            raise PeerCredError(f"LOCAL_PEERCRED returned xucred version {version}")
        return uid
    raise PeerCredError(f"no trusted way to read a socket peer's uid on {PLATFORM}")
