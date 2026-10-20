"""
Which Windows session a loopback connection comes from.

127.0.0.1 is shared by every user signed in to the PC. With two doctors signed
in (fast user switching, or a remote session), the second one's CMED page
reaches the first one's recorder: capture happens on the wrong desktop, and the
patient-refusal control - the only way to erase a recording - shows where
nobody can press it. So a connection is accepted only from the session this
recorder runs in.

The owner of the client end of the TCP connection is read from the system's
connection table (GetExtendedTcpTable), and its session from
ProcessIdToSessionId. Off Windows, or when the table cannot be read, the answer
is None and the caller lets the connection through: loopback and the Origin
check still apply, and refusing every connection on a lookup failure would stop
recording altogether.
"""
from __future__ import annotations

import logging
import socket
import struct
import sys
from typing import Optional

logger = logging.getLogger(__name__)

AF_INET, AF_INET6 = 2, 23
TCP_TABLE_OWNER_PID_ALL = 5
NO_ERROR, ERROR_INSUFFICIENT_BUFFER = 0, 122


def _session_of(pid: int) -> Optional[int]:
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    out = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(pid, ctypes.byref(out)):
        return None
    return int(out.value)


def own_session() -> Optional[int]:
    if sys.platform != "win32":
        return None
    import os
    return _session_of(os.getpid())


def _owner_pid(client_port: int, server_port: int, family: int) -> Optional[int]:
    """The process owning the loopback socket client_port -> server_port."""
    import ctypes
    iphlpapi = ctypes.WinDLL("iphlpapi")
    size = ctypes.c_ulong(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, family,
                                 TCP_TABLE_OWNER_PID_ALL, 0)
    for _ in range(3):
        buffer = ctypes.create_string_buffer(size.value + 4096)
        size = ctypes.c_ulong(len(buffer))
        result = iphlpapi.GetExtendedTcpTable(buffer, ctypes.byref(size), False, family,
                                              TCP_TABLE_OWNER_PID_ALL, 0)
        if result == NO_ERROR:
            break
        if result != ERROR_INSUFFICIENT_BUFFER:
            return None
    else:
        return None
    raw = buffer.raw
    (count,) = struct.unpack_from("<I", raw, 0)
    # MIB_TCPROW_OWNER_PID: state, local addr, local port, remote addr, remote port, pid
    # MIB_TCP6ROW_OWNER_PID: local addr[16], scope, local port, remote addr[16], scope,
    #                        remote port, state, pid
    row, offset = (24, 4) if family == AF_INET else (56, 4)
    for i in range(count):
        base = offset + i * row
        if family == AF_INET:
            _state, _laddr, lport, _raddr, rport, pid = struct.unpack_from("<IIIIII", raw, base)
        else:
            lport = struct.unpack_from("<I", raw, base + 20)[0]
            rport = struct.unpack_from("<I", raw, base + 44)[0]
            pid = struct.unpack_from("<I", raw, base + 52)[0]
        # Ports are in network byte order in the low 16 bits.
        if socket.ntohs(lport & 0xFFFF) == client_port and \
                socket.ntohs(rport & 0xFFFF) == server_port:
            return int(pid)
    return None


def session_of_peer(client_host: str, client_port: int, server_port: int) -> Optional[int]:
    """The Windows session of the process at the other end, or None if unknown."""
    if sys.platform != "win32" or not client_port or not server_port:
        return None
    try:
        family = AF_INET6 if ":" in (client_host or "") else AF_INET
        pid = _owner_pid(int(client_port), int(server_port), family)
        return _session_of(pid) if pid is not None else None
    except Exception as exc:
        logger.debug("Could not tell which session a connection is from: %s", exc)
        return None


def same_session(client_host: str, client_port: int, server_port: int) -> bool:
    """False only when the peer is known to be in another Windows session."""
    mine = own_session()
    if mine is None:
        return True
    theirs = session_of_peer(client_host, client_port, server_port)
    return theirs is None or theirs == mine


__all__ = ["same_session", "session_of_peer", "own_session"]
