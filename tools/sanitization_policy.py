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

from __future__ import annotations

import ipaddress
from typing import Final

IPv4Network = ipaddress.IPv4Network

# RFC 5737 documentation blocks and loopback, which the sanitization mandate requires
# documentation to use, plus the cloud metadata endpoint. The latter is a well-known
# public constant rather than anybody's machine, and a rule forbidding you to name it
# forbids writing the guard that blocks it. Scoped to the single address, never the
# surrounding link-local /16.
DOCUMENTABLE_NETWORKS: Final[tuple[IPv4Network, ...]] = (
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("192.0.2.0/24"),
    ipaddress.IPv4Network("198.51.100.0/24"),
    ipaddress.IPv4Network("203.0.113.0/24"),
    ipaddress.IPv4Network("169.254.169.254/32"),
)


def is_documentable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True when an address may appear in code or documentation."""
    return any(address in net for net in DOCUMENTABLE_NETWORKS if net.version == address.version)
