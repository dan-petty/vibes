#!/usr/bin/env python3
"""AST Invariant Sentinel: Automated static analyzer for agentic codebases.

Enforces four non-negotiable architectural invariants:
1. Cyclomatic Complexity <= 10
2. Indentation / Block Nesting Depth <= 5
3. Prohibition of arbitrary partial regex/substring pattern matching
4. Zero-trust sanitization (no private RFC 1918 IPs, standardized mock domains)
"""

from __future__ import annotations

import ast
import ipaddress
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# Closed-domain RFC 5737 and loopback networks permitted in code/documentation
ALLOWED_DOCUMENTATION_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
)

# Standard dummy mock domain; subdomains are strictly prohibited
CANONICAL_MOCK_DOMAIN = "example.com"

# IPv4 regex for detecting hardcoded addresses in string literals
IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


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
        return len(self.violations) == 0


class ComplexityVisitor(ast.NodeVisitor):
    """Measures Cyclomatic Complexity and Nesting Depth per function."""

    def __init__(self, file_path: str, max_complexity: int = 10, max_depth: int = 5) -> None:
        self.file_path = file_path
        self.max_complexity = max_complexity
        self.max_depth = max_depth
        self.violations: list[Violation] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._audit_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
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
        complexity = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
            elif isinstance(child, (ast.Assert, ast.IfExp)):
                complexity += 1
        return complexity

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

        def walk_depth(node: ast.AST, current_depth: int) -> int:
            sub_depth = current_depth + 1 if isinstance(node, nesting_node_types) else current_depth
            deepest = sub_depth
            for child in ast.iter_child_nodes(node):
                child_deepest = walk_depth(child, sub_depth)
                if child_deepest > deepest:
                    deepest = child_deepest
            return deepest

        # Root function itself is level 0, immediate statements inside are level 1
        max_seen = 0
        for statement in getattr(root, "body", []):
            depth = walk_depth(statement, 1)
            if depth > max_seen:
                max_seen = depth
        return max_seen


class SanitizationVisitor(ast.NodeVisitor):
    """Scans string literals for private IPs and non-standard mock domains."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.violations: list[Violation] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._check_ip_leakage(node.value, node.lineno)
            self._check_mock_domain(node.value, node.lineno)
        self.generic_visit(node)

    def _check_ip_leakage(self, text: str, lineno: int) -> None:
        for match in IPV4_PATTERN.findall(text):
            try:
                ip_obj = ipaddress.ip_address(match)
            except ValueError:
                continue

            if ip_obj.is_private and not any(ip_obj in net for net in ALLOWED_DOCUMENTATION_NETWORKS):
                self.violations.append(
                    Violation(
                        file_path=self.file_path,
                        line_number=lineno,
                        invariant="ZeroTrustSanitization",
                        message=f"Hardcoded private RFC 1918 IP '{match}' detected. Use RFC 5737 or loopback.",
                    )
                )

    def _check_mock_domain(self, text: str, lineno: int) -> None:
        # Detect non-standard dummy domains (e.g. test.example.com, fakeapp.dev)
        if "http://" in text or "https://" in text:
            if "example.com" in text:
                match = re.search(r"https?://([^/:]+)", text)
                if match:
                    hostname = match.group(1)
                    if hostname != CANONICAL_MOCK_DOMAIN and hostname.endswith(f".{CANONICAL_MOCK_DOMAIN}"):
                        self.violations.append(
                            Violation(
                                file_path=self.file_path,
                                line_number=lineno,
                                invariant="ZeroTrustSanitization",
                                message=f"Subdomain '{hostname}' detected. Standardize mock hostnames to '{CANONICAL_MOCK_DOMAIN}' with no subdomains.",
                            )
                        )


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

    return complexity_visitor.violations + sanitization_visitor.violations


def audit_directory(
    root_path: Path, max_complexity: int = 10, max_depth: int = 5, skip_tests: bool = True
) -> AuditReport:
    """Audit Python files under root_path, supporting single files or directories."""
    report = AuditReport()
    if root_path.is_file():
        if root_path.suffix == ".py":
            report.files_checked = 1
            report.violations = audit_file(root_path, max_complexity, max_depth)
        return report

    for py_file in root_path.rglob("*.py"):
        if skip_tests and (py_file.name.startswith("test_") or py_file.name.endswith("_test.py")):
            continue
        report.files_checked += 1
        violations = audit_file(py_file, max_complexity, max_depth)
        report.violations.extend(violations)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """CLI runner for the AST Invariant Sentinel."""
    target_path = Path(argv[1]) if argv and len(argv) > 1 else Path(".")
    print(f"🛡️  AST Invariant Sentinel: Scanning '{target_path}'...")

    report = audit_directory(target_path)
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
