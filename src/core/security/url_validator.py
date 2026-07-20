"""URL validation to prevent SSRF attacks.

Single source of truth for blocked networks and hostnames used by both
property list resolution and webhook URL validation.
"""

import ipaddress
import socket
from urllib.parse import urlparse

# Link-local / cloud-metadata / multicast ranges. ALWAYS blocked — never a
# legitimate webhook target, in ANY environment (the cloud-credential-exfiltration
# and internal-multicast surface). Mirrors AdCP security.mdx §SSRF rule 2's
# always-block list; the membership-pin test enumerates rule 2 verbatim so a future
# omission reddens.
METADATA_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),  # IPv4 link-local (AWS/GCP IMDS 169.254.169.254)
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("fd00:ec2::/32"),  # AWS IMDSv2 over IPv6
    ipaddress.ip_network("224.0.0.0/4"),  # IPv4 multicast
    ipaddress.ip_network("ff00::/8"),  # IPv6 multicast
]

# RFC 6052 NAT64 well-known prefix: an embedded IPv4 that, on a NAT64/DNS64
# network, is translated to the IPv4 in its low 32 bits — so ``64:ff9b::a9fe:a9fe``
# reaches 169.254.169.254. Checked specially (the embedded v4 is re-run through the
# block lists), because ``ipaddress`` classifies these as ordinary global IPv6.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")

# RFC-1918 private + loopback + CGNAT + unique-local ranges. Blocked by default, but
# a LEGITIMATE target for a trusted test/dev deployment (local receiver, Docker
# compose network), so allowed when the caller passes ``allow_private=True``.
PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # RFC 6598 CGNAT (cloud VPC / k8s pod CIDRs)
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

# Union of every reserved range (metadata + private); no external importer, kept as
# the flat "is this IP reserved at all" set for callers that don't distinguish the
# always-blocked subset from the opt-in-allowed one.
BLOCKED_NETWORKS = METADATA_NETWORKS + PRIVATE_NETWORKS

# Cloud-metadata hostnames — ALWAYS blocked (see METADATA_NETWORKS).
METADATA_HOSTNAMES = {
    "metadata.google.internal",
    "169.254.169.254",
    "metadata",
    "instance-data",
}

# Localhost / Docker-internal aliases. Blocked by default, allowed with
# ``allow_private=True`` (a trusted test/dev receiver reachable by these names).
LOCAL_HOSTNAMES = {
    "localhost",
    "host.docker.internal",
    "gateway.docker.internal",
    "docker.host.internal",
}

# Backward-compatible union (importers depend on this name).
BLOCKED_HOSTNAMES = METADATA_HOSTNAMES | LOCAL_HOSTNAMES


def check_url_ssrf(url: str, *, require_https: bool = False, allow_private: bool = False) -> tuple[bool, str]:
    """Check a URL for SSRF safety.

    Thin wrapper over :func:`resolve_and_validate_target` for callers that only
    need a yes/no (e.g. registration-time validation). Delivery callers should use
    ``resolve_and_validate_target`` and CONNECT to the returned IP (connection
    pinning) so the address checked is the address used.
    """
    _ip, error = resolve_and_validate_target(url, require_https=require_https, allow_private=allow_private)
    return (error == ""), error


def resolve_and_validate_target(
    url: str, *, require_https: bool = False, allow_private: bool = False
) -> tuple[str | None, str]:
    """Validate a URL for SSRF safety and return a single validated IP to pin.

    Resolves the hostname ONCE, validates EVERY resolved A/AAAA record, and returns
    ``(pinned_ip, "")`` where ``pinned_ip`` is a validated address the caller should
    connect to directly — eliminating the re-resolution / DNS-rebinding gap between
    validation and connection. On any failure returns ``(None, error_message)``.

    Args:
        url: The URL to validate.
        require_https: If True, reject non-HTTPS schemes.
        allow_private: If True, permit loopback / RFC-1918 private targets and
            localhost/Docker aliases (a trusted test/dev receiver). Cloud-metadata
            and link-local ranges/hostnames (169.254.x, fe80::,
            metadata.google.internal) remain blocked regardless.
    """
    try:
        parsed = urlparse(url)

        if require_https:
            if parsed.scheme != "https":
                return None, f"URL must use HTTPS scheme, got '{parsed.scheme}'"
        elif parsed.scheme not in ("http", "https"):
            return None, "URL must use http or https protocol"

        hostname = parsed.hostname
        if not hostname:
            return None, "URL must have a valid hostname"

        lowered = hostname.lower()
        if lowered in METADATA_HOSTNAMES:
            return None, f"URL hostname '{hostname}' is a blocked cloud-metadata endpoint"
        if lowered in LOCAL_HOSTNAMES and not allow_private:
            return None, f"URL hostname '{hostname}' is blocked (internal/private)"

        try:
            resolved = _resolve_ips(hostname)
        except OSError:
            return None, f"Cannot resolve hostname: {hostname}"
        if not resolved:
            return None, f"Cannot resolve hostname: {hostname}"

        # Validate EVERY resolved A/AAAA record — a hostname with one public and one
        # private/IPv6 record must not pass on the strength of its public record
        # (multi-record / DNS-rebinding surface). The caller connects to the returned
        # address (connection pinning) so the checked IP is the one actually used.
        for ip_str in resolved:
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                return None, f"Invalid IP address from hostname resolution: {ip_str}"

            reason = _ip_block_reason(ip, allow_private=allow_private)
            if reason:
                # Report the class, not the resolved IP itself — a per-record IP echo
                # to the buyer is a mild resolver oracle; the IP stays in server logs.
                return None, f"URL resolves to a blocked {reason} address"

        # Every record validated — pin to the first (a literal-IP host pins to itself).
        return resolved[0], ""

    except Exception as e:
        return None, f"Invalid URL: {e}"


def _canonicalize_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Collapse an IPv6 address to the IPv4 it actually reaches, if any.

    IPv4-mapped (``::ffff:a.b.c.d``) and RFC 6052 NAT64 (``64:ff9b::a.b.c.d``)
    addresses are ordinary global IPv6 to :mod:`ipaddress`, but on the wire they
    route to the embedded IPv4 — so a metadata/private v4 could slip through the
    v6 classification. Return the embedded IPv4 so the block lists see the real
    destination; otherwise return the address unchanged.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _NAT64_PREFIX:
            return ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    return ip


def _ip_block_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool) -> str:
    """Return a non-empty reason string if ``ip`` must be blocked, else ``""``.

    Always-blocked (every environment): link-local, cloud-metadata, and multicast
    ranges — plus the IPv4 embedded in an IPv4-mapped/NAT64 v6 address. Blocked
    unless ``allow_private``: loopback, RFC-1918 private, CGNAT, unique-local.
    """
    ip = _canonicalize_ip(ip)

    if any(ip in network for network in METADATA_NETWORKS) or ip.is_link_local or ip.is_multicast:
        return "link-local/metadata/multicast"

    if not allow_private and (any(ip in network for network in PRIVATE_NETWORKS) or ip.is_loopback or ip.is_private):
        return "private/internal"

    return ""


def _resolve_ips(hostname: str) -> list[str]:
    """Resolve a hostname to ALL of its A/AAAA records (not just the first IPv4).

    SSRF validation must consider every address the connection could use: a single
    ``gethostbyname`` call returns one IPv4 and would miss a second (private) record
    or an IPv6 record. Raises ``OSError``/``socket.gaierror`` when the name does not
    resolve. Deduplicates while preserving order.
    """
    infos = socket.getaddrinfo(hostname, None)
    seen: dict[str, None] = {}
    for info in infos:
        seen.setdefault(str(info[4][0]), None)
    return list(seen)
