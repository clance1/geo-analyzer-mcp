"""Shared async HTTP client with an SSRF guard.

The MCP server can be exposed over the internet, and ``page_audit`` fetches
arbitrary URLs. Every request (including each redirect hop) is checked so the
server cannot be used to probe the home/office network it runs on.

The check happens twice, and the second one is the authoritative one:

1. ``assert_public`` runs from a request hook: it resolves the hostname and
   refuses non-public answers *before* the request is sent.
2. The transport's network backend resolves the hostname **again**, at connect
   time, and opens the socket to that exact address. A name that answers with a
   public IP during the check and a private IP at connect time (DNS rebinding)
   therefore still cannot reach an internal host.

Without step 2 the two lookups were independent: the address that was checked
was not necessarily the address the socket was opened to.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from contextlib import asynccontextmanager
from typing import cast
from urllib.parse import urlparse

import httpcore
import httpx

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# RFC 6598 — shared address space (CGNAT). Widely used by Tailscale and by
# carrier/service internal ranges. ``ipaddress.is_private`` does not cover it.
CGNAT = ipaddress.ip_network("100.64.0.0/10")

INTERNAL_HOSTNAMES = {"localhost", "metadata.google.internal"}


class BlockedTargetError(ValueError):
    """Raised when a URL resolves to a private / loopback / reserved address."""


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("URL is required")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"Invalid URL: {url}")
    return url


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address):
        # ``::ffff:127.0.0.1`` and friends must be judged as the IPv4 address
        # they carry, otherwise the IPv4 deny ranges can be side-stepped.
        mapped = addr.ipv4_mapped
        if mapped is not None:
            addr = mapped
    if addr.version == 4 and addr in CGNAT:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


async def resolve_addresses(host: str, port: int = 0) -> list[str]:
    """Resolve ``host`` to the list of IP addresses it currently points at."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port or None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise httpx.ConnectError(f"DNS lookup failed for {host}: {exc}") from exc
    return [str(info[4][0]) for info in infos]


async def validated_addresses(host: str, port: int = 0) -> list[str]:
    """Resolve ``host`` and return its addresses, refusing non-public ones.

    A single non-public answer refuses the whole host: a name that mixes a
    public and a private address is treated as hostile.
    """
    addresses = await resolve_addresses(host, port)
    if not addresses:
        raise httpx.ConnectError(f"DNS lookup for {host} returned no addresses")
    for ip in addresses:
        if not _is_public_ip(ip):
            raise BlockedTargetError(
                f"Refusing to fetch {host}: resolves to non-public address {ip}. "
                "Set GEO_ALLOW_PRIVATE_TARGETS=true to allow."
            )
    return addresses


async def assert_public(url: str) -> None:
    host = urlparse(url).hostname
    if not host:
        raise BlockedTargetError(f"No host in URL: {url}")
    if host.lower() in INTERNAL_HOSTNAMES:
        raise BlockedTargetError(f"Refusing to fetch internal host: {host}")
    await validated_addresses(host)


class _PinningBackend(httpcore.AsyncNetworkBackend):
    """Resolves the host at connect time and connects to the validated address.

    ``connect_tcp`` is called by httpcore with the *hostname* from the request
    URL. We resolve it here, refuse anything that is not public, and hand the
    inner backend an IP literal — so the socket can only ever be opened to an
    address that was validated moments earlier. The URL (and therefore the
    ``Host`` header, the TLS SNI and the certificate check) still uses the
    hostname, because only the socket target changes.
    """

    def __init__(self, allow_private: bool = False,
                 inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self.allow_private = allow_private
        # ``httpcore.AnyIOBackend`` is a conditional import, so it is typed as a
        # bare stub when anyio is missing — cast it to the interface we use.
        self._inner: httpcore.AsyncNetworkBackend = (
            inner if inner is not None else cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend())
        )

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None,
                          socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None
                          ) -> httpcore.AsyncNetworkStream:
        target = host
        if not self.allow_private:
            target = (await validated_addresses(host, port))[0]
        return await self._inner.connect_tcp(
            target, port, timeout=timeout, local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None
                                  ) -> httpcore.AsyncNetworkStream:
        return await self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connections are pinned to validated addresses.

    httpx 0.28 does not expose a resolver hook on ``AsyncHTTPTransport``, so
    the pool is rebuilt here with a network backend that resolves and
    validates. Everything else (TLS, SNI, redirect handling, the ``Host``
    header) stays exactly as httpx does it.
    """

    def __init__(self, *, allow_private: bool = False, verify: bool = True, trust_env: bool = True,
                 limits: httpx.Limits | None = None, http1: bool = True, http2: bool = False) -> None:
        limits = limits or httpx.Limits(max_connections=20, max_keepalive_connections=10)
        super().__init__(verify=verify, trust_env=trust_env, limits=limits, http1=http1, http2=http2)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=verify, trust_env=trust_env),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=http1,
            http2=http2,
            network_backend=_PinningBackend(allow_private=allow_private),
        )


@asynccontextmanager
async def http_client(timeout: float = 20.0, allow_private: bool = False, **kwargs):
    async def _guard(request: httpx.Request) -> None:
        if not allow_private:
            await assert_public(str(request.url))

    hooks = {"request": [_guard]}
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    async with httpx.AsyncClient(
        transport=_PinnedTransport(allow_private=allow_private, limits=limits),
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        max_redirects=8,
        limits=limits,
        event_hooks=hooks,
        headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
        **kwargs,
    ) as client:
        yield client
