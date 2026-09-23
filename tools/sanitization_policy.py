#!/usr/bin/env python3
"""The single definition of which addresses documentation and code may name.

Three copies of this policy existed: the AST sentinel, the workbench's scanner, and the
documentation rules. Adding the cloud metadata address to one of them left the other two
rejecting it, which is the parallel-list divergence `AGENTS.md` §8.6 describes — a rule
implemented twice is two rules that agree until someone edits one.

`examples/ast-invariant-sentinel/sentinel.py` deliberately keeps its own copy: it is a
standalone, copy-pasteable exhibit and must not import from `tools/`. That exception is
stated here so the next reader knows it is a decision rather than an oversight.
"""

# sentinel: allow[ZeroTrustSanitization] — this module *is* the policy; the ranges below
# are its definition, not a leak.

from __future__ import annotations

import ipaddress
import re
from ipaddress import IPv4Network, IPv6Network
from typing import Final

# RFC 5737 documentation blocks and loopback, which the sanitization mandate requires
# documentation to use, plus the cloud metadata endpoint. The latter is a well-known
# public constant rather than anybody's machine, and a rule forbidding you to name it
# forbids writing the guard that blocks it. Scoped to the single address, never the
# surrounding link-local /16.
DOCUMENTABLE_NETWORKS: Final[tuple[IPv4Network | IPv6Network, ...]] = (
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("192.0.2.0/24"),
    ipaddress.IPv4Network("198.51.100.0/24"),
    ipaddress.IPv4Network("203.0.113.0/24"),
    ipaddress.IPv4Network("169.254.169.254/32"),
    # RFC 3849 documentation prefix and IPv6 loopback. Absent before, so `::1` — the one
    # address every local example uses — was not documentable while `127.0.0.1` was.
    ipaddress.IPv6Network("2001:db8::/32"),
    ipaddress.IPv6Network("::1/128"),
)

# The ranges that name a real machine on a real private network.
#
# Enumerated rather than delegated to `ipaddress.is_private`, which CPython documents as
# "not globally reachable by iana-ipv4-special-registry" — a wider set that also covers
# `0.0.0.0`, `255.255.255.255`, the RFC 2544 benchmarking block `198.18.0.0/15`, reserved
# space `240.0.0.0/4` and IETF protocol assignments `192.0.0.0/24`. Four components used
# `is_private` behind a message that says "RFC 1918", so each of them blocked six classes
# of address under a clause covering none of them, and told the reader the wrong reason.
#
# IPv6 is here because it was nowhere: every scanner matched a dotted-quad pattern only, so
# `http://[fd00::1]/` passed a private-egress guard untouched.
PRIVATE_HOST_NETWORKS: Final[tuple[IPv4Network | IPv6Network, ...]] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("100.64.0.0/10"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)

# A dotted quad in prose or in a URL.
IPV4_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Matches a bracketed IPv6 literal in a URL, per RFC 3986 §3.2.2, and a bare one in prose.
# A candidate finder, not a validator: writing a correct IPv6 grammar in a regular
# expression is how brittle patterns are born, and `ipaddress.ip_address` is already the
# authority. This matches anything with two or more colons and hands it over; `12:34:56`
# is found, refused by the parser, and never reported. The previous pattern required two
# to seven full groups and therefore missed every compressed address — `fe80::1234`, the
# commonest form there is.
IPV6_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\[([0-9A-Fa-f:]+)\]|((?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f]{0,4})"
)


def is_documentable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True when an address may appear in code or documentation."""
    return any(address in net for net in DOCUMENTABLE_NETWORKS if net.version == address.version)


def is_private_host(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True when an address names a real machine on a private network.

    This is the predicate a leak rule and an egress guard both want, and it is narrower
    than `is_private`: a bind-to-all `0.0.0.0` and a broadcast `255.255.255.255` name
    nobody's machine, so reporting them as a homelab leak is a false positive in the one
    rule whose false positives cost the most to argue with.
    """
    if is_documentable(address) or address.is_loopback or address.is_unspecified:
        return False
    return any(address in net for net in PRIVATE_HOST_NETWORKS if net.version == address.version)


def private_hosts_in(text: str) -> list[str]:
    """Return every private host address named in a block of text, IPv4 and IPv6 alike."""
    found: list[str] = []
    for match in IPV4_PATTERN.finditer(text):
        found.extend(_kept(match.group(0)))
    for match in IPV6_PATTERN.finditer(text):
        found.extend(_kept(match.group(1) or match.group(2)))
    return found


def _kept(token: str) -> list[str]:
    """Return the address if it parses and names a private host, else nothing."""
    address = parse_address(token)
    return [token] if address is not None and is_private_host(address) else []


def parse_address(token: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an address the way a resolver does, or return None if it is not one.

    `ipaddress.ip_address` is deliberately strict and refuses any octet with a leading
    zero, because `0177` meant octal historically and decimal now. `inet_aton` and every
    libc-backed client still accept it, so a guard that treats the `ValueError` as "not an
    address" fails **open** on exactly the notation an attacker would choose.

    That rule was written into `AGENTS.md` when one copy of this policy learned it, and the
    other four kept failing open — which is what an agreement test is for, and what the
    first version of that test missed by testing only canonical spellings.
    """
    parts = token.split(".")
    if len(parts) != 4:
        return _strict(token)
    try:
        octets = [_octet(part) for part in parts]
    except ValueError:
        return _strict(token)
    if any(value < 0 or value > 255 for value in octets):
        return _strict(token)
    return ipaddress.ip_address(".".join(str(value) for value in octets))


def _octet(part: str) -> int:
    """Read one octet as a resolver would: 0x hex, leading-zero octal, else decimal."""
    lowered = part.lower()
    if lowered.startswith("0x"):
        return int(lowered, 16)
    if lowered.startswith("0") and len(lowered) > 1:
        return int(lowered, 8)
    return int(lowered, 10)


def _strict(token: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Fall back to the strict parser for anything that is not a dotted quad."""
    try:
        return ipaddress.ip_address(token)
    except ValueError:
        return None
