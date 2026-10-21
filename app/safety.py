"""Stops the public crawler from being pointed at private networks (SSRF),
e.g. localhost, the VPS itself or the cloud metadata service 169.254.169.254.

Two layers:
1. check_url / guard_request reject a bad URL early with a clear message,
   on the first request and on every redirect hop.
2. GuardedBackend is the network layer under httpx. It resolves the host itself,
   refuses the connection if any address is private, and then connects to the
   exact IP it checked. httpx never resolves the name a second time, so a DNS
   server that answers "public" to the check and "private" to the connect
   (DNS rebinding) cannot slip through. TLS still uses the hostname for SNI and
   certificate checks, because httpcore passes it to start_tls separately.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpcore
import httpx


class BlockedTarget(ValueError):
    pass


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified)


def _public_addresses(host: str, infos) -> list[str]:
    ips = [info[4][0] for info in infos]
    if not ips:
        raise BlockedTarget(f"Cannot resolve {host}")
    for ip in ips:
        if not _is_public(ip):
            raise BlockedTarget(f"{host} points to a private address")
    return ips


def check_host(host: str) -> None:
    if not host:
        raise BlockedTarget("Missing host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise BlockedTarget(f"Cannot resolve {host}") from exc
    _public_addresses(host, infos)


def check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedTarget("Only http and https URLs are allowed")
    check_host(parts.hostname or "")


async def guard_request(request: httpx.Request) -> None:
    """httpx event hook: runs before every request, including redirects."""
    check_host(request.url.host)


async def resolve_public(host: str, port: int) -> str:
    """Resolve once, refuse if any address is private, return the IP to connect to."""
    if not host:
        raise BlockedTarget("Missing host")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedTarget(f"Cannot resolve {host}") from exc
    return _public_addresses(host, infos)[0]


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that only ever connects to a checked public IP."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None, resolver=resolve_public):
        self._inner = inner or httpcore.AnyIOBackend()
        self._resolve = resolver

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        ip = await self._resolve(host, port)
        return await self._inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                             socket_options=socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise BlockedTarget("Unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_transport(**kwargs) -> httpx.AsyncHTTPTransport:
    """An httpx transport whose connections go through GuardedBackend."""
    transport = httpx.AsyncHTTPTransport(**kwargs)
    transport._pool._network_backend = GuardedBackend()  # httpx does not expose this option
    return transport
