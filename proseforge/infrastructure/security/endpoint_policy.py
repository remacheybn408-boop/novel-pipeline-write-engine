from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def _parse_ipv4_lenient(host: str) -> ipaddress.IPv4Address | None:
    """Parse non-canonical IPv4 forms (127.1 / 2130706433 / 0x7f000001).

    ``ipaddress.ip_address`` only accepts canonical dotted-quad, but HTTP
    clients resolve short/decimal/hex forms to loopback just the same — so
    they must be classified here instead of slipping through as "hostnames".
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    def parse_part(part: str) -> int:
        if not part:
            raise ValueError("empty IPv4 part")
        # libc accepts leading-zero octets as octal, while int(..., 0) rejects
        # them on modern Python. Classify them before handing the host to HTTP.
        base = 16 if part.lower().startswith("0x") else 8 if len(part) > 1 and part.startswith("0") else 10
        return int(part[2:] if base == 16 else part, base)

    try:
        numbers = [parse_part(part) for part in parts]
    except ValueError:
        return None
    if any(number < 0 or number > 0xFFFFFFFF for number in numbers):
        return None
    if len(numbers) == 1:
        value = numbers[0]
    else:
        trailing_bits = 8 * (5 - len(numbers))
        if any(number > 255 for number in numbers[:-1]) or numbers[-1] > (1 << trailing_bits) - 1:
            return None
        value = 0
        for number in numbers[:-1]:
            value = (value << 8) | number
        value = (value << trailing_bits) | numbers[-1]
    try:
        return ipaddress.IPv4Address(value)
    except ValueError:
        return None


class EndpointPolicy:
    def __init__(self, allowed_local_hosts: tuple[str, ...] = ()):
        self.allowed_local_hosts = set(allowed_local_hosts)

    def validate(self, url: str, *, allow_local: bool = False) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("endpoint must be an http(s) URL without userinfo")
        host = parsed.hostname.lower().rstrip(".")
        local = host in {"localhost", "ip6-localhost"} or host == "127.0.0.1" or host == "::1"
        try:
            address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
        except ValueError:
            address = _parse_ipv4_lenient(host)
        if address is not None:
            local = local or address.is_private or address.is_loopback or address.is_link_local
        if local and not (allow_local and host in self.allowed_local_hosts):
            raise ValueError("local or private endpoint is not allowed")
        return parsed.geturl()
