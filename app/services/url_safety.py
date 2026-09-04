"""Guard rails for fetching user-supplied URLs server-side (SSRF protection).

Every outbound fetch of a URL that originated from a user (public /score, lead
websites from Google Places or CSV import) goes through ``safe_get``:

* only http/https
* hostname must resolve, and every resolved address must be public (no loopback,
  RFC1918, link-local/cloud-metadata, multicast, reserved, IPv6 equivalents)
* redirects are followed manually so each hop is re-validated
* response body is capped (default 2 MB) and must be HTML/XML/text
"""

import ipaddress
import logging
import socket
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
ALLOWED_SCHEMES = {"http", "https"}
TEXT_CONTENT_TYPES = ("text/", "application/xhtml", "application/xml", "application/json")


class UnsafeURL(ValueError):
    """Raised when a URL must not be fetched from the server."""


class UnresolvableHost(UnsafeURL):
    """The hostname does not resolve (typo, dead domain) — not a policy block."""


def _is_public_address(ip: ipaddress._BaseAddress) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None and not _is_public_address(ip.ipv4_mapped))
        or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))  # CGNAT
    )


def resolve_public(host: str) -> list[str]:
    """Resolve ``host`` and return its addresses, raising UnsafeURL if any is non-public."""
    if not host:
        raise UnsafeURL("empty host")
    host = host.strip("[]").lower()
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        raise UnsafeURL(f"blocked host {host}")

    # Literal IP?
    try:
        ip = ipaddress.ip_address(host)
        if not _is_public_address(ip):
            raise UnsafeURL(f"non-public address {host}")
        return [str(ip)]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnresolvableHost(f"cannot resolve {host}: {exc}") from exc

    addresses = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if not _is_public_address(ip):
            raise UnsafeURL(f"{host} resolves to non-public address {addr}")
        addresses.append(str(ip))
    if not addresses:
        raise UnresolvableHost(f"{host} did not resolve")
    return addresses


def validate_url(url: str) -> str:
    """Return the URL if it is safe to fetch; raise UnsafeURL otherwise."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeURL(f"scheme {parsed.scheme!r} not allowed")
    if not parsed.hostname:
        raise UnsafeURL("missing host")
    if parsed.username or parsed.password:
        raise UnsafeURL("credentials in URL not allowed")
    if parsed.port and parsed.port not in (80, 443, 8080, 8443):
        raise UnsafeURL(f"port {parsed.port} not allowed")
    resolve_public(parsed.hostname)
    return url


def is_safe_url(url: str) -> bool:
    try:
        validate_url(url)
        return True
    except UnsafeURL:
        return False


def _read_capped(response: requests.Response, max_bytes: int) -> bytes:
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            chunks.append(chunk[: max_bytes - (total - len(chunk))])
            break
        chunks.append(chunk)
    return b"".join(chunks)


def safe_get(
    url: str,
    *,
    timeout: float = 15,
    headers: Optional[dict] = None,
    verify: bool = True,
    max_bytes: int = MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    allowed_content_types: Iterable[str] = TEXT_CONTENT_TYPES,
) -> requests.Response:
    """GET ``url`` with SSRF protection. Returns a ``requests.Response`` whose
    ``._content`` has been read (capped) so ``.text`` works as usual. Raises
    ``UnsafeURL`` for disallowed targets and the usual ``requests`` exceptions
    for network errors.
    """
    current = validate_url(url)
    session = requests.Session()
    session.max_redirects = 0
    try:
        for _ in range(max_redirects + 1):
            resp = session.get(
                current,
                timeout=timeout,
                headers=headers,
                verify=verify,
                allow_redirects=False,
                stream=True,
            )
            if resp.is_redirect or resp.is_permanent_redirect:
                location = resp.headers.get("Location")
                resp.close()
                if not location:
                    raise UnsafeURL("redirect without Location")
                current = validate_url(urljoin(current, location))
                continue

            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and not any(ctype.startswith(t) for t in allowed_content_types):
                resp.close()
                raise UnsafeURL(f"unsupported content type {ctype}")

            body = _read_capped(resp, max_bytes)
            resp.close()
            # Freeze the (capped) body so .text / .content behave normally.
            resp._content = body
            resp._content_consumed = True
            resp.url = current
            return resp
        raise requests.exceptions.TooManyRedirects(f"more than {max_redirects} redirects")
    finally:
        session.close()
