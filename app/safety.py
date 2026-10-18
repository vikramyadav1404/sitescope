"""Stops the public crawler from being pointed at private networks (SSRF),
e.g. localhost, the VPS itself or the cloud metadata service 169.254.169.254."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx


class BlockedTarget(ValueError):
    pass


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified)


def check_host(host: str) -> None:
    if not host:
        raise BlockedTarget("Missing host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise BlockedTarget(f"Cannot resolve {host}") from exc
    for info in infos:
        if not _is_public(info[4][0]):
            raise BlockedTarget(f"{host} points to a private address")


def check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedTarget("Only http and https URLs are allowed")
    check_host(parts.hostname or "")


async def guard_request(request: httpx.Request) -> None:
    """httpx event hook: runs before every request, including redirects."""
    check_host(request.url.host)
