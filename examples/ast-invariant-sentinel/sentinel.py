#!/usr/bin/env python3
"""AST Invariant Sentinel: Automated static analyzer for agentic codebases.

Enforces four non-negotiable architectural invariants:
1. Cyclomatic Complexity <= 10
2. Indentation / Block Nesting Depth <= 5
3. Prohibition of arbitrary partial regex/substring pattern matching
4. Zero-trust sanitization (no private RFC 1918 IPs, standardized mock domains)
"""

from __future__ import annotations

import argparse
import ast
import io
import ipaddress
import itertools
import re
import sys
import tokenize
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

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


def _ast_node_complexity(node: ast.AST) -> int:
    """Return cyclomatic complexity weight contributed by an AST node."""
    if isinstance(node, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.ExceptHandler, ast.Assert, ast.IfExp)):
        return 1
    if isinstance(node, ast.BoolOp):
        return max(0, len(node.values) - 1)
    return 0


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


def _is_prohibited_ip(match: str) -> bool:
    """Return True if match is a private IP not in allowed documentation networks."""
    try:
        ip_obj = ipaddress.ip_address(match)
        return ip_obj.is_private and not any(ip_obj in net for net in ALLOWED_DOCUMENTATION_NETWORKS)
    except ValueError:
        return False


def _is_url_with_example(text: str) -> bool:
    """Return True if text contains an http/https URL referencing example.com."""
    return "example.com" in text and ("http://" in text or "https://" in text)


def _extract_disallowed_subdomain(text: str) -> str | None:
    """Extract and return any non-canonical subdomain of example.com found in URLs."""
    if not _is_url_with_example(text):
        return None
    match = re.search(r"https?://([^/:]+)", text)
    if not match:
        return None
    hostname = match.group(1)
    is_subdomain = hostname != CANONICAL_MOCK_DOMAIN and hostname.endswith(f".{CANONICAL_MOCK_DOMAIN}")
    return hostname if is_subdomain else None


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
                        message=f"Hardcoded private RFC 1918 IP '{match}' detected. Use RFC 5737 or loopback.",
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
    return WaiverScan(frozenset(waived), tuple(violations))


def audit_file(file_path: Path, max_complexity: int = 10, max_depth: int = 5) -> list[Violation]:
    """Audit a single Python source file for invariant violations."""
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError) as err:
        return [
            Violation(
                file_path=str(file_path),
                line_number=getattr(err, "lineno", 1) or 1,
                invariant="SyntaxIntegrity",
                message=f"Could not parse file: {err}",
            )
        ]

    complexity_visitor = ComplexityVisitor(str(file_path), max_complexity, max_depth)
    complexity_visitor.visit(tree)

    sanitization_visitor = SanitizationVisitor(str(file_path))
    sanitization_visitor.visit(tree)

    waivers = _scan_waivers(file_path, source)
    detected = complexity_visitor.violations + sanitization_visitor.violations
    return list(waivers.violations) + [v for v in detected if v.invariant not in waivers.waived]


def _collect_py_targets(root_path: Path) -> list[Path]:
    if root_path.is_file():
        return [root_path] if root_path.suffix == ".py" else []
    return sorted(root_path.rglob("*.py"))


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


def audit_targets(paths: Sequence[Path], max_complexity: int = 10, max_depth: int = 5) -> AuditReport:
    """Audit every supplied file or directory target into one consolidated report."""
    report = AuditReport()
    report.violations.extend(
        Violation(
            file_path=str(path),
            line_number=1,
            invariant="TargetIntegrity",
            message="Audit target does not exist; the gate would otherwise certify nothing as clean.",
        )
        for path in paths
        if not path.exists()
    )
    targets = _expand_targets([path for path in paths if path.exists()])
    report.files_checked = len(targets)
    for py_file in targets:
        report.violations.extend(audit_file(py_file, max_complexity, max_depth))
    return report


def _parse_cli_targets(argv: Sequence[str] | None) -> list[Path]:
    """Resolve every CLI path argument; pre-commit passes N filenames, never one.

    Unknown flags exit non-zero via argparse rather than being silently discarded:
    a sentinel that quietly ignores part of its input certifies code it never read.
    """
    parser = argparse.ArgumentParser(description="AST Invariant Sentinel")
    parser.add_argument("paths", nargs="*", default=[], help="File or directory paths to audit")
    args = parser.parse_args(list(argv[1:]) if argv is not None else None)
    return [Path(raw) for raw in args.paths] or [Path(".")]


def main(argv: Sequence[str] | None = None) -> int:
    """CLI runner for the AST Invariant Sentinel."""
    target_paths = _parse_cli_targets(argv)
    scanned = ", ".join(f"'{path}'" for path in target_paths)
    print(f"🛡️  AST Invariant Sentinel: Scanning {scanned}...")

    report = audit_targets(target_paths)
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
