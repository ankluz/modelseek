"""Who is on the other end of a connection inside a Docker container.

Docker Desktop hands every connection to a published port to the container from the network's
gateway: the owner on this computer and a guest from the LAN look the same. Docker Engine on Linux
does that for connections from the host itself. Other containers keep their own addresses.

So inside Docker the gateway stands for "came through a published port", and the real address of a
guest behind it is unknown.
"""
from __future__ import annotations

import functools
import ipaddress
import logging
import os
import socket
import struct

log = logging.getLogger("modelseek.docker")

IN_DOCKER = os.environ.get("MODELSEEK_IN_DOCKER") == "1"
Addr = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_routes(route: str = "", route6: str = "") -> frozenset[Addr]:
    """Default gateways from /proc/net/route and /proc/net/ipv6_route."""
    out: set[Addr] = set()
    for line in route.splitlines()[1:]:
        f = line.split()
        if len(f) > 2 and f[1] == "00000000" and f[2] != "00000000":
            try:
                out.add(ipaddress.IPv4Address(socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))))
            except (ValueError, struct.error, OSError):
                continue
    for line in route6.splitlines():
        f = line.split()
        if len(f) > 4 and f[0] == "0" * 32 and f[1] == "00" and f[4] != "0" * 32:
            try:
                out.add(ipaddress.IPv6Address(bytes.fromhex(f[4])))
            except ValueError:
                continue
    return frozenset(out)


def _read(path: str) -> str:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read()
    except OSError:
        return ""


@functools.cache
def gateways() -> frozenset[Addr]:
    """The container's gateways; empty outside Docker or when they cannot be read."""
    if not IN_DOCKER:
        return frozenset()
    found = parse_routes(_read("/proc/net/route"), _read("/proc/net/ipv6_route"))
    if not found:
        log.warning("не удалось определить шлюз Docker: админский профиль примет запрос с любого адреса сети контейнера")
    return found


def is_gateway(ip: Addr | None) -> bool:
    return ip is not None and ip in gateways()
