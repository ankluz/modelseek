"""Limits on what data from model sites can make modelseek do on the network.

Sites are untrusted: a response, redirect or thumbnail address may point into the local network
(router, NAS, cloud metadata, modelseek itself). Requests may go to private addresses only when the
user configured that exact host and port (SearXNG, Ollama, a webhook, an own source).
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 8 * 2**20
MAX_IMAGE_BYTES = 4 * 2**20
MAX_REDIRECTS = 5
REDIRECTS = (301, 302, 303, 307, 308)
DNS_TIMEOUT = 3.0
DNS_TTL = 120.0

Resolver = Callable[[str, int], Awaitable[list[str]]]


class BlockedRequest(httpx.RequestError):
    """A request to an address that data from a site must not reach."""


class ResponseTooLarge(Exception):
    pass


def endpoint(url: str) -> tuple[str, str, int] | None:
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower().rstrip(".")
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not host:
        return None
    return p.scheme, host, port


def same_origin(a: str, b: str) -> bool:
    ea = endpoint(a)
    return ea is not None and ea == endpoint(b)


def keeps_credentials(a: str, b: str) -> bool:
    """Same origin, or the same host moving from http to https on default ports (as httpx allows)."""
    if same_origin(a, b):
        return True
    ea, eb = endpoint(a), endpoint(b)
    return bool(ea and eb) and ea[1] == eb[1] and (ea[0], ea[2], eb[0], eb[2]) == ("http", 80, "https", 443)


def is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def no_resolver(host: str, port: int) -> list[str]:
    return []


class NetGuard:
    def __init__(self, trusted: Iterable[str] = (), resolver: Resolver | None = None):
        self.trusted: set[tuple[str, int]] = set()
        for url in trusted:
            ep = endpoint(str(url)) if url else None
            if ep:
                self.trusted.add((ep[1], ep[2]))
        self.resolver = resolver or system_resolver
        self._cache: dict[tuple[str, int], tuple[float, list[str]]] = {}

    def trust(self, *urls: str) -> None:
        for url in urls:
            ep = endpoint(url.replace("{", "").replace("}", "")) if url else None
            if ep:
                self.trusted.add((ep[1], ep[2]))

    def is_trusted(self, url: str) -> bool:
        ep = endpoint(url)
        return bool(ep) and (ep[1], ep[2]) in self.trusted

    async def _addresses(self, host: str, port: int) -> list[str]:
        try:
            ipaddress.ip_address(host.strip("[]"))
            return [host.strip("[]")]
        except ValueError:
            pass
        hit = self._cache.get((host, port))
        if hit and time.monotonic() - hit[0] < DNS_TTL:
            return hit[1]
        try:
            ips = await asyncio.wait_for(self.resolver(host, port), DNS_TIMEOUT)
        except (OSError, asyncio.TimeoutError, UnicodeError):
            ips = []
        if len(self._cache) > 2000:
            self._cache.clear()
        self._cache[(host, port)] = (time.monotonic(), ips)
        return ips

    async def problem(self, url: str, use_trust: bool = True) -> str | None:
        """Why the url must not be requested, or None when it is fine.

        use_trust=False: the address came from a site (redirect, thumbnail), so even the user's own
        local services are off limits.
        """
        ep = endpoint(url)
        if ep is None:
            return f"адрес {str(url)[:80]} — не http(s)"
        _, host, port = ep
        if use_trust and (host, port) in self.trusted:
            return None
        if host == "localhost" or host.endswith(".localhost"):
            return f"{host} — это этот компьютер"
        bad = [ip for ip in await self._addresses(host, port) if not is_public(ip)]
        if bad:
            return f"{host} ведёт во внутреннюю сеть ({bad[0]})"
        return None

    async def check(self, url: str, request: httpx.Request | None = None, use_trust: bool = True) -> None:
        why = await self.problem(url, use_trust)
        if why:
            raise BlockedRequest(f"запрос заблокирован: {why}", request=request)

    async def allowed(self, url: str, use_trust: bool = True) -> bool:
        return await self.problem(url, use_trust) is None

    async def httpx_hook(self, request: httpx.Request) -> None:
        await self.check(str(request.url), request)


def trusted_urls(cfg: dict[str, Any]) -> list[str]:
    """Addresses the user configured themselves: these may be on the local network."""
    out = [(cfg.get("translate") or {}).get("url"), (cfg.get("notify") or {}).get("url")]
    out.append(((cfg.get("sources") or {}).get("searxng") or {}).get("url") or "http://127.0.0.1:8089")
    for sc in (cfg.get("custom_sources") or {}).values():
        if isinstance(sc, dict):
            out += [sc.get("url"), sc.get("homepage"), sc.get("search_page")]
    return [str(u).replace("{", "").replace("}", "") for u in out if u]


async def read_limited(chunks, declared: str | None, limit: int) -> bytes:
    if declared and declared.isdigit() and int(declared) > limit:
        raise ResponseTooLarge(f"ответ больше {limit // 2**20} МБ")
    buf = bytearray()
    async for chunk in chunks:
        buf += chunk
        if len(buf) > limit:
            raise ResponseTooLarge(f"ответ больше {limit // 2**20} МБ")
    return bytes(buf)


def decode(data: bytes, encoding: str | None) -> str:
    try:
        return data.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def next_hop(method: str, status: int, url: str, location: str) -> tuple[str, str]:
    target = str(httpx.URL(url).join(location))
    if status == 303 or (status in (301, 302) and method == "POST"):
        method = "GET"
    return method, target
