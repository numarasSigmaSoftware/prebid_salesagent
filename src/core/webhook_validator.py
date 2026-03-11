"""Shared outbound URL validation to prevent SSRF attacks."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class WebhookURLValidator:
    """Validates outbound HTTP(S) URLs to prevent SSRF attacks."""

    BLOCKED_NETWORKS = [
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("100.64.0.0/10"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.0.0.0/24"),
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("198.18.0.0/15"),
        ipaddress.ip_network("198.51.100.0/24"),
        ipaddress.ip_network("203.0.113.0/24"),
        ipaddress.ip_network("224.0.0.0/4"),
        ipaddress.ip_network("240.0.0.0/4"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("fe80::/10"),
    ]

    BLOCKED_HOSTNAMES = {
        "localhost",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }

    @classmethod
    def _resolve_all_addresses(cls, hostname: str) -> list[ipaddress._BaseAddress]:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        resolved = []
        for family, _socktype, _proto, _canonname, sockaddr in infos:
            address = sockaddr[0]
            if family == socket.AF_INET6 and "%" in address:
                address = address.split("%", 1)[0]
            resolved.append(ipaddress.ip_address(address))
        if not resolved:
            raise socket.gaierror(f"No addresses resolved for {hostname}")
        return resolved

    @classmethod
    def _is_blocked_ip(cls, ip: ipaddress._BaseAddress) -> bool:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_unspecified
            or ip.is_multicast
            or ip.is_reserved
        ):
            return True
        return any(ip in network for network in cls.BLOCKED_NETWORKS)

    @classmethod
    def validate_outbound_url(cls, url: str, *, purpose: str = "Outbound URL") -> tuple[bool, str]:
        """Validate a generic outbound HTTP(S) destination."""
        try:
            parsed = urlparse(url)

            if parsed.scheme not in ("http", "https"):
                return False, f"{purpose} must use http or https protocol"

            if not parsed.hostname:
                return False, f"{purpose} must have a valid hostname"

            hostname = parsed.hostname.lower()
            if hostname in cls.BLOCKED_HOSTNAMES:
                return False, f"{purpose} hostname '{parsed.hostname}' is blocked for security reasons"

            try:
                resolved_addresses = cls._resolve_all_addresses(hostname)
            except socket.gaierror:
                return False, f"Cannot resolve hostname: {parsed.hostname}"
            except ValueError as exc:
                return False, f"Invalid resolved IP address: {exc}"

            for resolved_ip in resolved_addresses:
                if cls._is_blocked_ip(resolved_ip):
                    return False, f"{purpose} resolves to blocked private/internal IP address: {resolved_ip}"

            return True, ""
        except Exception as exc:
            return False, f"Invalid {purpose.lower()}: {exc}"

    @classmethod
    def validate_webhook_url(cls, url: str) -> tuple[bool, str]:
        """Backwards-compatible webhook validator."""
        return cls.validate_outbound_url(url, purpose="Webhook URL")

    @classmethod
    def validate_signals_agent_url(cls, url: str) -> tuple[bool, str]:
        """Signals-agent specific alias for the shared outbound policy."""
        return cls.validate_outbound_url(url, purpose="Signals agent URL")

    @classmethod
    def validate_for_testing(cls, url: str, allow_localhost: bool = False) -> tuple[bool, str]:
        """Validate URL with optional localhost allowance for tests."""
        is_valid, error = cls.validate_webhook_url(url)
        if not is_valid and allow_localhost:
            lowered = error.lower()
            if "localhost" in lowered or "loopback" in lowered or "127.0.0." in lowered:
                return True, ""
        return is_valid, error
