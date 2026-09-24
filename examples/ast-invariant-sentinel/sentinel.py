#!/usr/bin/env python3
"""AST Invariant Sentinel: Automated static analyzer for agentic codebases.

Enforces four non-negotiable architectural invariants:
1. Cyclomatic Complexity <= 10
2. Indentation / Block Nesting Depth <= 5
3. Prohibition of arbitrary partial regex/substring pattern matching
4. Zero-trust sanitization (no private host addresses, standardized mock domains)
"""

# sentinel: allow[ZeroTrustSanitization] — this module defines the private-address policy;
# the ranges below are that definition, not an endpoint.

from __future__ import annotations

import argparse
import ast
import io
import ipaddress
import itertools
import os
import re
import sys
import tokenize
import tomllib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# Closed-domain RFC 5737 and loopback networks permitted in code/documentation
ALLOWED_DOCUMENTATION_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    # The cloud metadata endpoint is a well-known public constant, not anybody's machine.
    # Any SSRF guard or egress test must name it to block it, and a rule that forbids
    # naming it forbids defending against it. Scoped to the single address, not the
    # surrounding link-local /16, which is still somebody's autoconfigured network.
    ipaddress.ip_network("169.254.169.254/32"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("::1/128"),
)

# The ranges that name a real machine on a private network. Kept in step with
# `tools/sanitization_policy.py`, which is the single definition; this sample application is
# standalone by design and cannot import it, so `tests/test_address_policy_agreement.py`
# asserts the two agree on a shared corpus.
#
# Enumerated rather than delegated to `ipaddress.is_private`, which CPython documents as
# "not globally reachable by iana-ipv4-special-registry" — a wider set covering `0.0.0.0`,
# `255.255.255.255`, RFC 2544 benchmarking space, reserved `240.0.0.0/4` and IETF protocol
# assignments. Every one of those was blocked here under a message reading "RFC 1918", a
# clause that covers none of them.
PRIVATE_HOST_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

# Standard dummy mock domain; subdomains are strictly prohibited
CANONICAL_MOCK_DOMAIN = "example.com"

# IPv4 regex for detecting hardcoded addresses in string literals
IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Closed domain of waivable invariants. Structural caps (complexity, nesting) are
# never waivable: a metric you can opt out of is not an invariant.
WAIVABLE_INVARIANTS = frozenset({"ZeroTrustSanitization"})
WAIVER_PRAGMA_RE = re.compile(
    r"^#\s*sentinel:\s*allow\[([A-Za-z]+)\]\s*(?:[-—:]\s*)?(?P<reason>\S.*)$"
)
WAIVER_HEADER_LINE_LIMIT = 15
MIN_WAIVER_JUSTIFICATION_CHARS = 12


@dataclass(frozen=True)
class Violation:
    """Represents an architectural invariant violation."""

    file_path: str
    line_number: int
    invariant: str
    message: str
    metric_value: int | None = None
    threshold: int | None = None


@dataclass
class AuditReport:
    """Summary of sentinel audit execution."""

    files_checked: int = 0
    violations: list[Violation] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """Return True if zero architectural invariant violations were detected."""
        return len(self.violations) == 0


ALL_INVARIANT_RULES: Final[frozenset[str]] = frozenset({
    "CyclomaticComplexity",
    "NestingDepth",
    "ZeroTrustSanitization",
    "SyntaxIntegrity",
    "TargetIntegrity",
    "WaiverIntegrity",
})

RULE_ALIASES: Final[dict[str, str]] = {
    "cyclomaticcomplexity": "CyclomaticComplexity",
    "complexity": "CyclomaticComplexity",
    "c901": "CyclomaticComplexity",
    "cc001": "CyclomaticComplexity",
    "nestingdepth": "NestingDepth",
    "nesting": "NestingDepth",
    "nd001": "NestingDepth",
    "zerotrustsanitization": "ZeroTrustSanitization",
    "sanitization": "ZeroTrustSanitization",
    "security": "ZeroTrustSanitization",
    "zt001": "ZeroTrustSanitization",
    "s101": "ZeroTrustSanitization",
    "syntaxintegrity": "SyntaxIntegrity",
    "syntax": "SyntaxIntegrity",
    "si001": "SyntaxIntegrity",
    "targetintegrity": "TargetIntegrity",
    "targets": "TargetIntegrity",
    "ti001": "TargetIntegrity",
    "waiverintegrity": "WaiverIntegrity",
    "waivers": "WaiverIntegrity",
    "wi001": "WaiverIntegrity",
}


def normalize_rule_name(name: str) -> str:
    """Normalize a rule alias, code, or invariant name to its canonical rule name."""
    cleaned = name.strip().lower()
    return RULE_ALIASES.get(cleaned, name.strip())


@dataclass(frozen=True)
class RulePreset:
    """Named configuration preset with calibrated invariant thresholds and active rules."""

    name: str
    description: str
    max_complexity: int
    max_depth: int
    active_rules: frozenset[str]


PRESETS: Final[dict[str, RulePreset]] = {
    "standard": RulePreset(
        name="standard",
        description="Standard baseline invariants (M <= 10, depth <= 5, zero-trust sanitization)",
        max_complexity=10,
        max_depth=5,
        active_rules=ALL_INVARIANT_RULES,
    ),
    "strict": RulePreset(
        name="strict",
        description="Strict proactive headroom invariants (M <= 6, depth <= 3, zero-trust sanitization)",
        max_complexity=6,
        max_depth=3,
        active_rules=ALL_INVARIANT_RULES,
    ),
    "pedantic": RulePreset(
        name="pedantic",
        description="Pedantic ultra-compact invariants for critical concurrency/functional kernels (M <= 4, depth <= 2)",
        max_complexity=4,
        max_depth=2,
        active_rules=ALL_INVARIANT_RULES,
    ),
    "relaxed": RulePreset(
        name="relaxed",
        description="Relaxed migration thresholds for legacy codebases (M <= 15, depth <= 6)",
        max_complexity=15,
        max_depth=6,
        active_rules=ALL_INVARIANT_RULES,
    ),
    "security_only": RulePreset(
        name="security_only",
        description="Zero-trust egress and sanitization audit only; complexity/nesting ignored",
        max_complexity=999,
        max_depth=99,
        active_rules=frozenset({
            "ZeroTrustSanitization",
            "SyntaxIntegrity",
            "TargetIntegrity",
            "WaiverIntegrity",
        }),
    ),
    "structural_only": RulePreset(
        name="structural_only",
        description="Structural AST complexity and nesting depth audit only; sanitization ignored",
        max_complexity=10,
        max_depth=5,
        active_rules=frozenset({
            "CyclomaticComplexity",
            "NestingDepth",
            "SyntaxIntegrity",
            "TargetIntegrity",
        }),
    ),
}


@dataclass(frozen=True)
class SentinelConfig:
    """Consolidated configuration for AST invariant sentinel execution."""

    preset_name: str = "standard"
    max_complexity: int = 10
    max_depth: int = 5
    active_rules: frozenset[str] = ALL_INVARIANT_RULES
    selected_rules: frozenset[str] = field(default_factory=frozenset)
    ignored_rules: frozenset[str] = field(default_factory=frozenset)
    extended_rules: frozenset[str] = field(default_factory=frozenset)

    def is_rule_active(self, rule_name: str) -> bool:
        """Report whether an invariant rule is enabled under this configuration."""
        canonical = normalize_rule_name(rule_name)
        return canonical in self.active_rules


@dataclass(frozen=True)
class ConfigOverrides:
    """Explicit CLI or caller overrides for configuration resolution."""

    preset_name: str | None = None
    max_complexity: int | None = None
    max_depth: int | None = None
    select: Sequence[str] | None = None
    ignore: Sequence[str] | None = None
    extend_select: Sequence[str] | None = None
    config_file: Path | None = None


def _find_default_config() -> Path | None:
    """Find pyproject.toml or sentinel.toml in the current or ancestor directories."""
    cwd = Path.cwd()
    for directory in [cwd, *cwd.parents]:
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            return pyproject
        sentinel_toml = directory / "sentinel.toml"
        if sentinel_toml.is_file():
            return sentinel_toml
    return None


def load_toml_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load configuration from a TOML file (e.g. pyproject.toml or sentinel.toml)."""
    target = config_path or _find_default_config()
    if not target or not target.is_file():
        return {}
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    if target.name == "pyproject.toml":
        tool_section = data.get("tool", {})
        return dict(tool_section.get("sentinel", {}))
    return dict(data)


def _split_csv_tokens(text: str) -> list[str]:
    """Split comma-separated text into stripped non-empty tokens."""
    return [token.strip() for token in text.split(",") if token.strip()]


def _parse_rule_sequence(raw: Any) -> list[str]:
    """Parse comma-separated string or sequence into a list of rule strings."""
    if isinstance(raw, str):
        return _split_csv_tokens(raw)
    if isinstance(raw, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _normalize_rule_set(rules: Sequence[str]) -> frozenset[str]:
    """Return a frozenset of normalized rule names from a sequence."""
    return frozenset(normalize_rule_name(r) for r in rules)


def _resolve_active_rules(
    preset: RulePreset,
    select: Sequence[str],
    extend: Sequence[str],
    ignore: Sequence[str],
) -> frozenset[str]:
    """Compute final active rule set from preset, explicit select, extensions, and ignores."""
    base = _normalize_rule_set(select) if select else preset.active_rules
    if extend:
        base = base | _normalize_rule_set(extend)
    if ignore:
        base = base - _normalize_rule_set(ignore)
    return base


def _extract_threshold(override_val: int | None, toml_val: Any, default_val: int) -> int:
    """Extract an integer threshold from override, TOML data, or preset default."""
    if override_val is not None:
        return override_val
    if isinstance(toml_val, int):
        return toml_val
    return default_val


def _pick_raw_sequence(override: Sequence[str] | None, toml_val: Any) -> Any:
    """Select override sequence if provided, otherwise fallback to TOML value."""
    if override is not None:
        return override
    return toml_val


def _resolve_preset_name(override_name: str | None, toml_name: Any) -> str:
    """Determine effective preset name from override or TOML configuration."""
    if override_name is not None:
        return override_name
    if isinstance(toml_name, str):
        return toml_name
    return "standard"


def resolve_config(overrides: ConfigOverrides | None = None) -> SentinelConfig:
    """Resolve final configuration combining defaults, TOML settings, and overrides."""
    opts = overrides or ConfigOverrides()
    toml_data = load_toml_config(opts.config_file)

    preset_name = _resolve_preset_name(opts.preset_name, toml_data.get("preset"))
    preset = PRESETS.get(preset_name, PRESETS["standard"])

    max_c = _extract_threshold(opts.max_complexity, toml_data.get("max_complexity"), preset.max_complexity)
    max_d = _extract_threshold(opts.max_depth, toml_data.get("max_depth"), preset.max_depth)

    select = _parse_rule_sequence(_pick_raw_sequence(opts.select, toml_data.get("select")))
    extend = _parse_rule_sequence(_pick_raw_sequence(opts.extend_select, toml_data.get("extend_select")))
    ignore = _parse_rule_sequence(_pick_raw_sequence(opts.ignore, toml_data.get("ignore")))

    active = _resolve_active_rules(preset, select, extend, ignore)
    return SentinelConfig(
        preset_name=preset.name,
        max_complexity=max_c,
        max_depth=max_d,
        active_rules=active,
        selected_rules=frozenset(normalize_rule_name(r) for r in select),
        ignored_rules=frozenset(normalize_rule_name(r) for r in ignore),
        extended_rules=frozenset(normalize_rule_name(r) for r in extend),
    )


def render_presets_table() -> str:
    """Render a formatted markdown table of available presets."""
    lines = [
        "| Preset Name | Complexity ($M$) | Max Depth | Active Invariant Rules | Description |",
        "|---|---|---|---|---|",
    ]
    for preset in PRESETS.values():
        rule_desc = f"{len(preset.active_rules)} rules" if preset.active_rules == ALL_INVARIANT_RULES else ", ".join(sorted(preset.active_rules))
        lines.append(
            f"| `{preset.name}` | $\\le {preset.max_complexity}$ | $\\le {preset.max_depth}$ | {rule_desc} | {preset.description} |"
        )
    return "\n".join(lines)


# Every construct that puts a branch in the control flow graph. The list used to stop at
# `If`/`While`/`For`/`ExceptHandler`/`Assert`/`IfExp`/`BoolOp`, which left two whole
# families of decision scoring zero:
#
#   * comprehensions and generator expressions, whose `for` is a loop and whose `if`
#     clauses are predicates — `[r for r in rows if r.a if r.b]` is three branches;
#   * `match`, whose every `case` is an arm of a multiway decision — NIST SP 500-235 §4.1
#     handles a multiway decision by counting its arms.
#
# The consequence was not theoretical: four functions in this repository passed the M<=10
# gate while radon scored them 11 to 13, and an eleven-case `match` dispatcher — the shape
# §10.1 steers agents toward — measured 1. `test_sentinel.py` now asserts agreement with
# radon over a corpus rather than restating the rules, because a hand-maintained list of
# branch constructs is a list that falls behind the language.
def _loop_weight(node: ast.AST) -> int:
    """A loop is a decision; its `else` is a second exit path and radon counts it too."""
    return 1 + (1 if getattr(node, "orelse", None) else 0)


def _try_weight(node: ast.AST) -> int:
    """`try` is not a decision; its `else` runs only when no handler did. Handlers count alone."""
    return 1 if getattr(node, "orelse", None) else 0


def _comprehension_weight(node: ast.AST) -> int:
    """The `for` itself, plus one per `if` clause attached to it."""
    return 1 + len(getattr(node, "ifs", ()))


def _match_case_weight(node: ast.AST) -> int:
    """Every arm is a decision except the wildcard, which is the fall-through.

    A guard adds nothing: radon scores `case int() if x > 0` the same as `case int()`, and
    this gate's declared reference is radon (§1a — prefer the tool's own metric to a
    recomputation of it). Measured, not assumed.
    """
    pattern = getattr(node, "pattern", None)
    wildcard = isinstance(pattern, ast.MatchAs) and pattern.pattern is None
    return 0 if wildcard else 1


def _boolop_weight(node: ast.AST) -> int:
    """`a and b and c` is two decisions, not one."""
    return max(0, len(getattr(node, "values", ())) - 1)


def _one(_node: ast.AST) -> int:
    """A plain decision node."""
    return 1


# Table dispatch on the exact node type (§10.1). The predecessor was a chain of `isinstance`
# checks that stopped at `If`/`While`/`For`/`ExceptHandler`/`Assert`/`IfExp`/`BoolOp`, which
# left two whole families of decision scoring zero: comprehensions, whose `for` is a loop and
# whose `if` clauses are predicates, and `match`, whose every arm is a multiway decision.
#
# The consequence was not theoretical. Four functions in this repository passed the M<=10
# gate while radon scored them 11 to 13, and an eleven-case `match` dispatcher — the shape
# §10.1 steers agents toward — measured 1. `test_sentinel.py` asserts agreement with radon
# over a corpus rather than restating these rules, because a hand-maintained list of
# branching constructs is a list that falls behind the language.
#
# The one deliberate divergence: radon scores `except*` as zero, because it predates
# PEP 654. A handler is a branch, so it is counted here, and the gate is therefore never
# *lower* than radon — a gate stricter than its reference is safe, a laxer one is the defect
# this table was rewritten to fix.
_WEIGHTS: dict[type[ast.AST], Callable[[ast.AST], int]] = {
    ast.While: _loop_weight,
    ast.For: _loop_weight,
    ast.AsyncFor: _loop_weight,
    ast.Try: _try_weight,
    ast.TryStar: _try_weight,
    ast.comprehension: _comprehension_weight,
    ast.match_case: _match_case_weight,
    ast.BoolOp: _boolop_weight,
    ast.If: _one,
    ast.IfExp: _one,
    ast.ExceptHandler: _one,
    ast.Assert: _one,
}


def _ast_node_complexity(node: ast.AST) -> int:
    """Return the cyclomatic complexity weight contributed by one AST node."""
    handler = _WEIGHTS.get(type(node))
    return handler(node) if handler else 0


class ComplexityVisitor(ast.NodeVisitor):
    """Measures Cyclomatic Complexity and Nesting Depth per function."""

    def __init__(self, file_path: str, max_complexity: int = 10, max_depth: int = 5) -> None:
        self.file_path = file_path
        self.max_complexity = max_complexity
        self.max_depth = max_depth
        self.violations: list[Violation] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Audit synchronous function definition for complexity and nesting depth."""
        self._audit_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Audit asynchronous function definition for complexity and nesting depth."""
        self._audit_function(node)
        self.generic_visit(node)

    def _audit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        complexity = self._calculate_complexity(node)
        if complexity > self.max_complexity:
            self.violations.append(
                Violation(
                    file_path=self.file_path,
                    line_number=node.lineno,
                    invariant="CyclomaticComplexity",
                    message=f"Function '{node.name}' has cyclomatic complexity of {complexity} (limit: {self.max_complexity}).",
                    metric_value=complexity,
                    threshold=self.max_complexity,
                )
            )

        max_depth = self._calculate_max_nesting(node)
        if max_depth > self.max_depth:
            self.violations.append(
                Violation(
                    file_path=self.file_path,
                    line_number=node.lineno,
                    invariant="NestingDepth",
                    message=f"Function '{node.name}' has nesting depth of {max_depth} (limit: {self.max_depth}).",
                    metric_value=max_depth,
                    threshold=self.max_depth,
                )
            )

    def _calculate_complexity(self, node: ast.AST) -> int:
        """Calculate McCabe Cyclomatic Complexity M = E - N + 2P."""
        return 1 + sum(_ast_node_complexity(child) for child in ast.walk(node))

    def _calculate_max_nesting(self, root: ast.AST) -> int:
        """Calculate the deepest indentation nesting level within a function."""
        nesting_node_types = (
            ast.If,
            ast.While,
            ast.For,
            ast.AsyncFor,
            ast.With,
            ast.AsyncWith,
            ast.Try,
            ast.ExceptHandler,
            # Every compound statement that opens a suite. `match` and its `case` blocks are
            # two indentation levels the language reference lists and this tuple did not, and
            # PEP 654's `try`/`except*` parses to `TryStar`, a distinct node type from `Try` —
            # so a genuinely six-deep function measured two and the depth ceiling never saw it.
            ast.TryStar,
            ast.Match,
            ast.match_case,
        )

        def _walk_depth(node: ast.AST, current_depth: int) -> int:
            sub_depth = current_depth + 1 if isinstance(node, nesting_node_types) else current_depth
            deepest = sub_depth
            for child in ast.iter_child_nodes(node):
                child_deepest = _walk_depth(child, sub_depth)
                if child_deepest > deepest:
                    deepest = child_deepest
            return deepest

        # Root function itself is level 0, immediate statements inside are level 1
        max_seen = 0
        for statement in getattr(root, "body", []):
            depth = _walk_depth(statement, 1)
            if depth > max_seen:
                max_seen = depth
        return max_seen


def _octet(part: str) -> int:
    """Read one octet as a resolver would: 0x hex, leading-zero octal, else decimal."""
    lowered = part.lower()
    if lowered.startswith("0x"):
        return int(lowered, 16)
    if lowered.startswith("0") and len(lowered) > 1:
        return int(lowered, 8)
    return int(lowered, 10)


def _valid_octets(octets: Sequence[int]) -> bool:
    """Validate that every decoded octet is within IPv4 byte boundaries (0-255)."""
    return all(0 <= val <= 255 for val in octets)


def _parse_dotted_quad(token: str) -> ipaddress.IPv4Address | None:
    """Parse dotted-quad with resolver octets (hex, octal, decimal)."""
    parts = token.split(".")
    if len(parts) != 4:
        return None
    try:
        octets = [_octet(part) for part in parts]
    except ValueError:
        return None
    if not _valid_octets(octets):
        return None
    return ipaddress.IPv4Address(".".join(str(val) for val in octets))


def _parse_address(token: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an address the way a resolver does, or return None if it is not one.

    `ipaddress.ip_address` refuses any octet with a leading zero; `inet_aton` and every
    libc-backed client accept it. A guard that reads the `ValueError` as "not an address"
    therefore fails **open** on the one notation an attacker would choose. Kept in step with
    `tools/sanitization_policy.py`; `tests/test_address_policy_agreement.py` asserts it.
    """
    quad = _parse_dotted_quad(token)
    if quad is not None:
        return quad
    try:
        return ipaddress.ip_address(token)
    except ValueError:
        return None


def _in_network_list(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """Check if IP address falls within any network of matching version."""
    return any(net.version == ip_obj.version and ip_obj in net for net in networks)


def _is_prohibited_ip(match: str) -> bool:
    """Return True if match is a private IP not in allowed documentation networks."""
    ip_obj = _parse_address(match)
    if ip_obj is None or ip_obj.is_loopback or ip_obj.is_unspecified:
        return False
    if _in_network_list(ip_obj, ALLOWED_DOCUMENTATION_NETWORKS):
        return False
    return _in_network_list(ip_obj, PRIVATE_HOST_NETWORKS)


def _is_url_with_example(text: str) -> bool:
    """Return True if text contains an http/https URL referencing example.com."""
    return "example.com" in text and ("http://" in text or "https://" in text)


def _extract_disallowed_subdomain(text: str) -> str | None:
    """Return the first non-canonical subdomain of example.com in any URL in the text.

    Every URL in the string, never only the first. `re.search` stopped at the first match,
    so a literal holding a compliant URL followed by a subdomain was read as clean — and a
    docstring or a fixture naming two endpoints is the ordinary case, not a contrived one.
    """
    if not _is_url_with_example(text):
        return None
    for match in re.finditer(r"https?://([^/:\s\)\]\"']+)", text):
        hostname = match.group(1)
        if hostname != CANONICAL_MOCK_DOMAIN and hostname.endswith(f".{CANONICAL_MOCK_DOMAIN}"):
            return hostname
    return None


class SanitizationVisitor(ast.NodeVisitor):
    """Scans string literals for private IPs and non-standard mock domains."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.violations: list[Violation] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        """Scan string literal constant for private IPs and non-standard mock domains."""
        if isinstance(node.value, str):
            self._check_ip_leakage(node.value, node.lineno)
            self._check_mock_domain(node.value, node.lineno)
        self.generic_visit(node)

    def _check_ip_leakage(self, text: str, lineno: int) -> None:
        for match in IPV4_PATTERN.findall(text):
            if _is_prohibited_ip(match):
                self.violations.append(
                    Violation(
                        file_path=self.file_path,
                        line_number=lineno,
                        invariant="ZeroTrustSanitization",
                        message=f"Private host address '{match}' detected. Use RFC 5737 or loopback.",
                    )
                )

    def _check_mock_domain(self, text: str, lineno: int) -> None:
        hostname = _extract_disallowed_subdomain(text)
        if hostname:
            self.violations.append(
                Violation(
                    file_path=self.file_path,
                    line_number=lineno,
                    invariant="ZeroTrustSanitization",
                    message=f"Subdomain '{hostname}' detected. Standardize mock hostnames to '{CANONICAL_MOCK_DOMAIN}' with no subdomains.",
                )
            )


@dataclass(frozen=True)
class WaiverScan:
    """Result of parsing module-header waiver pragmas."""

    waived: frozenset[str] = frozenset()
    violations: tuple[Violation, ...] = ()


def _header_tokens(source: str) -> Iterator[tokenize.TokenInfo]:
    """Yield tokens from the module header, tolerating sources that fail to tokenize."""
    try:
        yield from itertools.takewhile(
            lambda token: token.start[0] <= WAIVER_HEADER_LINE_LIMIT,
            tokenize.generate_tokens(io.StringIO(source).readline),
        )
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return


def _iter_header_comments(source: str) -> list[tuple[int, str]]:
    """Return real comment tokens within the module header, ignoring string literals."""
    return [
        (token.start[0], token.string)
        for token in _header_tokens(source)
        if token.type == tokenize.COMMENT
    ]


def _waiver_defect(match: re.Match[str] | None) -> str | None:
    """Return why a waiver pragma is invalid, or None when it is well-formed."""
    if match is None:
        return "Malformed waiver. Expected: # sentinel: allow[<Invariant>] <justification>"
    invariant = match.group(1)
    if invariant not in WAIVABLE_INVARIANTS:
        return f"Invariant '{invariant}' is not waivable. Waivable: {', '.join(sorted(WAIVABLE_INVARIANTS))}."
    if len(match.group("reason").strip()) < MIN_WAIVER_JUSTIFICATION_CHARS:
        return f"Waiver for '{invariant}' needs >= {MIN_WAIVER_JUSTIFICATION_CHARS} characters of justification."
    return None


def _scan_waivers(file_path: Path, source: str) -> WaiverScan:
    """Parse `# sentinel: allow[<Invariant>] <justification>` pragmas from the module header."""
    waived: set[str] = set()
    violations: list[Violation] = []
    for line_number, text in _iter_header_comments(source):
        if "sentinel:" not in text:
            continue
        match = WAIVER_PRAGMA_RE.match(text.strip())
        defect = _waiver_defect(match)
        if defect or match is None:
            violations.append(
                Violation(str(file_path), line_number, "WaiverIntegrity", defect or "Invalid waiver.")
            )
            continue
        waived.add(match.group(1))
    violations.extend(_late_waiver_violations(file_path, source, waived))
    return WaiverScan(frozenset(waived), tuple(violations))


def _late_waiver_violations(file_path: Path, source: str, waived: set[str]) -> list[Violation]:
    """Report a waiver written below the header window, where it has no effect.

    A pragma on line 16 of a 15-line header is ignored in silence: the file's violations are
    reported exactly as if nobody had written one, and the author reads a message about the
    code rather than about the waiver. Costing an hour once is enough; say it plainly.
    """
    late: list[Violation] = []
    for index, line in enumerate(source.splitlines()[WAIVER_HEADER_LINE_LIMIT:], WAIVER_HEADER_LINE_LIMIT + 1):
        match = WAIVER_PRAGMA_RE.match(line.strip())
        if match and match.group(1) not in waived:
            late.append(Violation(
                str(file_path), index, "WaiverIntegrity",
                f"Waiver for '{match.group(1)}' is below line {WAIVER_HEADER_LINE_LIMIT} and has no "
                "effect. Move it into the module header.",
            ))
    return late


def _parse_source_tree(file_path: Path) -> tuple[str, ast.AST | None, Exception | None]:
    """Read source code and parse AST, catching syntax or decode errors."""
    try:
        source = file_path.read_text(encoding="utf-8")
        return source, ast.parse(source, filename=str(file_path)), None
    except (SyntaxError, UnicodeDecodeError) as err:
        return "", None, err


def _syntax_violation(file_path: Path, err: Exception) -> Violation:
    """Construct a SyntaxIntegrity violation from a parse/decode exception."""
    return Violation(
        file_path=str(file_path),
        line_number=getattr(err, "lineno", 1) or 1,
        invariant="SyntaxIntegrity",
        message=f"Could not parse file: {err}",
    )


def _run_visitors(
    tree: ast.AST,
    file_path: str,
    config: SentinelConfig,
) -> list[Violation]:
    """Execute AST visitors and collect detected violations."""
    complexity_visitor = ComplexityVisitor(file_path, config.max_complexity, config.max_depth)
    complexity_visitor.visit(tree)
    sanitization_visitor = SanitizationVisitor(file_path)
    if config.is_rule_active("ZeroTrustSanitization"):
        sanitization_visitor.visit(tree)
    return complexity_visitor.violations + sanitization_visitor.violations


def _filter_active_violations(
    detected: Sequence[Violation],
    waivers: WaiverScan,
    config: SentinelConfig,
) -> list[Violation]:
    """Filter raw violations by active waivers and active configuration rules."""
    unwaived = (v for v in detected if v.invariant not in waivers.waived)
    combined = itertools.chain(waivers.violations, unwaived)
    return [v for v in combined if config.is_rule_active(v.invariant)]


def audit_file(
    file_path: Path,
    max_complexity: int = 10,
    max_depth: int = 5,
    config: SentinelConfig | None = None,
) -> list[Violation]:
    """Audit a single Python source file for invariant violations."""
    active_config = config or SentinelConfig(
        max_complexity=max_complexity,
        max_depth=max_depth,
        active_rules=ALL_INVARIANT_RULES,
    )
    source, tree, parse_err = _parse_source_tree(file_path)
    if parse_err is not None or tree is None:
        if active_config.is_rule_active("SyntaxIntegrity") and parse_err:
            return [_syntax_violation(file_path, parse_err)]
        return []

    detected = _run_visitors(tree, str(file_path), active_config)
    waivers = _scan_waivers(file_path, source)
    return _filter_active_violations(detected, waivers, active_config)


# Directory names holding code this repository did not write. Kept in step with
# `tools/source_tree_policy.py`, which is the repository's single definition of its own
# corpus; this sample application is standalone by design and cannot import it, so
# `tests/test_source_tree_policy.py` asserts the two agree on the real tree rather than
# trusting them to. Without this, `sentinel.py` with no arguments audited `node_modules`
# and failed on a vendored package — the corpus defect of Observation 16, in the one
# instrument that had never been invoked that way by CI or by a hook.
VENDORED_DIR_NAMES: frozenset[str] = frozenset({"node_modules", "__pycache__", "venv"})
VENDORED_DIR_SUFFIXES: tuple[str, ...] = (".egg-info",)


def _is_repository_dir(name: str) -> bool:
    """Report whether a directory belongs to this repository rather than to a dependency."""
    return not (
        name.startswith(".") or name in VENDORED_DIR_NAMES or name.endswith(VENDORED_DIR_SUFFIXES)
    )


def _py_files_in_dir(parent: str, files: Sequence[str]) -> Iterator[Path]:
    """Yield Python file paths from a single directory."""
    parent_path = Path(parent)
    return (parent_path / name for name in files if name.endswith(".py"))


def _walk_repository_py_files(root: Path) -> Iterator[Path]:
    """Walk directory tree yielding repository Python files."""
    for parent, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if _is_repository_dir(d)]
        yield from _py_files_in_dir(parent, files)


def _collect_py_targets(root_path: Path) -> list[Path]:
    """Expand one target into the repository's own Python files beneath it.

    Prunes during the walk. `rglob` cannot prune, so it descends into every vendored
    package before discarding the result: measured on a tree carrying a Node toolchain
    that is most of the walk.
    """
    if root_path.is_file():
        return [root_path] if root_path.suffix == ".py" else []
    return sorted(_walk_repository_py_files(root_path))


def _dedupe_key(path: Path) -> Path:
    """Return a canonical identity for path, tolerating unresolvable symlinks."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _expand_targets(paths: Sequence[Path]) -> list[Path]:
    """Expand every file or directory target into a deduplicated, order-preserving file list."""
    candidates = itertools.chain.from_iterable(_collect_py_targets(path) for path in paths)
    unique: dict[Path, Path] = {}
    for py_file in candidates:
        unique.setdefault(_dedupe_key(py_file), py_file)
    return list(unique.values())


def _find_missing_targets(paths: Sequence[Path]) -> list[Violation]:
    """Identify non-existent audit paths as TargetIntegrity violations."""
    return [
        Violation(
            file_path=str(path),
            line_number=1,
            invariant="TargetIntegrity",
            message="Audit target does not exist; the gate would otherwise certify nothing as clean.",
        )
        for path in paths
        if not path.exists()
    ]


def audit_targets(
    paths: Sequence[Path],
    max_complexity: int = 10,
    max_depth: int = 5,
    config: SentinelConfig | None = None,
) -> AuditReport:
    """Audit every supplied file or directory target into one consolidated report."""
    active_config = config or SentinelConfig(
        max_complexity=max_complexity,
        max_depth=max_depth,
        active_rules=ALL_INVARIANT_RULES,
    )
    report = AuditReport()
    if active_config.is_rule_active("TargetIntegrity"):
        report.violations.extend(_find_missing_targets(paths))
    targets = _expand_targets([path for path in paths if path.exists()])
    report.files_checked = len(targets)
    for py_file in targets:
        report.violations.extend(audit_file(py_file, config=active_config))
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser for the sentinel."""
    parser = argparse.ArgumentParser(
        description="AST Invariant Sentinel: Automated static analyzer for agentic codebases.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", default=[], help="File or directory paths to audit")
    parser.add_argument(
        "--preset",
        "-p",
        choices=list(PRESETS.keys()),
        default=None,
        help="Selectable rule preset (standard, strict, relaxed, pedantic, security_only, structural_only)",
    )
    parser.add_argument("--select", "-s", help="Comma-separated rule names or codes to activate exclusively")
    parser.add_argument("--extend-select", help="Comma-separated rule names or codes to activate alongside preset")
    parser.add_argument("--ignore", "-i", help="Comma-separated rule names or codes to ignore")
    parser.add_argument("--max-complexity", "-C", type=int, help="Override maximum cyclomatic complexity ceiling")
    parser.add_argument("--max-depth", "-D", type=int, help="Override maximum nesting depth ceiling")
    parser.add_argument("--config", "-c", type=Path, help="Path to TOML configuration file (e.g. pyproject.toml)")
    parser.add_argument("--list-presets", action="store_true", help="List all available presets and exit")
    return parser


def _split_cli_csv(raw: str | None) -> list[str] | None:
    """Split comma-separated CLI argument string or return None."""
    if raw is None:
        return None
    return [token.strip() for token in raw.split(",") if token.strip()]


def _resolve_cli_targets(raw_paths: Sequence[str]) -> list[Path]:
    """Convert raw path strings to Path objects, defaulting to current directory."""
    if not raw_paths:
        return [Path(".")]
    return [Path(raw) for raw in raw_paths]


def parse_cli_args(argv: Sequence[str] | None) -> tuple[list[Path], SentinelConfig, bool]:
    """Parse command line arguments into target paths, resolved config, and list flag."""
    parser = _build_arg_parser()
    args = parser.parse_args(list(argv[1:]) if argv is not None else None)
    if args.list_presets:
        return [], resolve_config(), True

    overrides = ConfigOverrides(
        preset_name=args.preset,
        max_complexity=args.max_complexity,
        max_depth=args.max_depth,
        select=_split_cli_csv(args.select),
        ignore=_split_cli_csv(args.ignore),
        extend_select=_split_cli_csv(args.extend_select),
        config_file=args.config,
    )
    target_paths = _resolve_cli_targets(args.paths)
    return target_paths, resolve_config(overrides), False


def _parse_cli_targets(argv: Sequence[str] | None) -> list[Path]:
    """Resolve every CLI path argument; pre-commit passes N filenames, never one."""
    targets, _, _ = parse_cli_args(argv)
    return targets


def main(argv: Sequence[str] | None = None) -> int:
    """CLI runner for the AST Invariant Sentinel."""
    target_paths, config, is_list = parse_cli_args(argv)
    if is_list:
        print("Available AST Invariant Sentinel Presets:\n")
        print(render_presets_table())
        return 0

    scanned = ", ".join(f"'{path}'" for path in target_paths)
    print(f"🛡️  AST Invariant Sentinel [preset={config.preset_name}]: Scanning {scanned}...")

    report = audit_targets(target_paths, config=config)
    print(f"Checked {report.files_checked} Python files.")

    if report.is_clean:
        print("✅ All architectural invariants PASSED! Zero violations.")
        return 0

    print(f"\n❌ Found {len(report.violations)} architectural invariant violation(s):")
    for v in report.violations:
        print(f"  [{v.invariant}] {v.file_path}:{v.line_number} — {v.message}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
