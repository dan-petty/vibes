"""Tests that every address policy in this repository is the same policy.

`tools/sanitization_policy.py` is the single definition, and three sample applications
cannot import it — they are standalone exhibits by design. That exception is why the policy
lived in five places and had drifted in four: each delegated to `ipaddress.is_private`
behind a message reading "RFC 1918", so each blocked `0.0.0.0`, `255.255.255.255`, RFC 2544
benchmarking space and reserved ranges under a clause covering none of them — and none
scanned IPv6, so `http://[fd00::1]/` walked through every egress guard untouched.
"""

# sentinel: allow[ZeroTrustSanitization] — the corpus below is the policy under test; each
# address is a fixture asserting its classification, not an endpoint anything contacts.

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "examples" / "ast-invariant-sentinel"))
sys.path.insert(0, str(REPO_ROOT / "examples" / "prompt-mutation-fuzzer"))
sys.path.insert(0, str(REPO_ROOT / "examples" / "agentic-ide-hook-sentinel"))

import fuzzer
import hook_sentinel
import sentinel
from sanitization_policy import is_private_host, private_hosts_in

# Every address whose classification the five implementations must agree on. The first
# group is what "private host" means; the second is everything `is_private` also covers and
# this policy deliberately does not.
CORPUS: dict[str, bool] = {
    "10.0.0.1": True,
    "172.16.0.9": True,
    "172.31.255.254": True,
    "192.168.1.5": True,
    "169.254.1.1": True,
    "100.64.0.1": True,
    "fd00::1": True,
    "fe80::1": True,
    "0.0.0.0": False,
    "255.255.255.255": False,
    "198.18.0.1": False,
    "240.0.0.1": False,
    "192.0.0.1": False,
    "127.0.0.1": False,
    "::1": False,
    "192.0.2.5": False,
    "198.51.100.7": False,
    "203.0.113.9": False,
    "2001:db8::1": False,
    "169.254.169.254": False,
    "8.8.8.8": False,
    "172.32.0.1": False,
}

IMPLEMENTATIONS = {
    "sanitization_policy": lambda a: is_private_host(ipaddress.ip_address(a)),
    "ast-invariant-sentinel": lambda a: sentinel._is_prohibited_ip(a),
    "prompt-mutation-fuzzer": lambda a: fuzzer.InvariantAuditor._is_private_leak(a),
    "agentic-ide-hook-sentinel": lambda a: hook_sentinel._check_private_ip(a),
}


@pytest.mark.parametrize("address", sorted(CORPUS))
def test_every_implementation_agrees_on_every_address(address: str) -> None:
    """One policy, five implementations, one answer per address."""
    expected = CORPUS[address]
    verdicts = {name: bool(fn(address)) for name, fn in IMPLEMENTATIONS.items()}
    assert verdicts == dict.fromkeys(IMPLEMENTATIONS, expected)


def test_a_bind_address_is_not_a_private_host() -> None:
    """The case that started this: `0.0.0.0` names nobody's machine.

    `is_private` is the IANA not-globally-reachable set, which includes it. Four detectors
    reported it as an RFC 1918 leak, and `endpoint: 0.0.0.0:4317` appears in this
    repository's own collector config.
    """
    assert not any(bool(fn("0.0.0.0")) for fn in IMPLEMENTATIONS.values())


@pytest.mark.parametrize("text", [
    "connect to http://[fd00::1]/admin",
    "peer fe80::1234 is unreachable",
])
def test_an_ipv6_private_address_is_found_in_text(text: str) -> None:
    """Every scanner matched a dotted quad only, so IPv6 walked straight through."""
    assert private_hosts_in(text)
    assert hook_sentinel._scan_for_forbidden_ips(text)


def test_a_host_comparison_is_case_folded() -> None:
    """DNS is case-insensitive (RFC 4343), so a check that respects case is one you disable
    with the shift key."""
    leaks: list[str] = []
    fuzzer.InvariantAuditor._check_subdomain_leak("https://API.EXAMPLE.COM/v1", 1, leaks)
    assert leaks
