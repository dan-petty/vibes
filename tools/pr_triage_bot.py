#!/usr/bin/env python3
"""Closed-Loop PR Triage & Invariant Review Bot.

Orchestrates multi-persona code review (security, architecture, devops, qa)
against pull requests, evaluating mechanical invariants, generating structured
review summaries, and gating PR merges.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

# Private RFC 1918 IP address ranges prohibited across repositories
_RFC1918_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"
)

# Potential credential or secret token patterns
_SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----")),
    ("GENERIC_TOKEN", re.compile(r"(?i)\b(?:api_key|auth_token|secret_key)\s*=\s*['\"][A-Za-z0-9_\-]{20,}['\"]")),
)

# GitHub Action 40-character commit hash pinning pattern
_ACTION_PIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"uses:\s*['\"]?([^@\s'\"]+)@([a-f0-9]{40})(?:\s*#\s*v[\w.-]+)?['\"]?"
)
_UNPINNED_ACTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"uses:\s*['\"]?([^@\s'\"]+)@(?!([a-f0-9]{40}))([\w.-]+)['\"]?"
)


class PersonaKind(StrEnum):
    """Reviewer persona domain."""

    SECURITY = "security"
    ARCHITECTURE = "architecture"
    DEVOPS = "devops"
    QA = "qa"


class Severity(StrEnum):
    """Finding severity level."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ReviewVerdict(StrEnum):
    """PR review decision verdict."""

    APPROVE = "APPROVE"
    COMMENT = "COMMENT"
    REQUEST_CHANGES = "REQUEST_CHANGES"


@dataclass(frozen=True)
class ReviewFinding:
    """A single rule violation or advisory finding identified during triage."""

    persona: PersonaKind
    severity: Severity
    rule_id: str
    message: str
    path: str
    line: int = 1
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert finding into dictionary format."""
        return {
            "persona": self.persona.value,
            "severity": self.severity.value,
            "rule_id": self.rule_id,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class PersonaReview:
    """Consolidated review emitted by an individual persona."""

    persona: PersonaKind
    verdict: ReviewVerdict
    summary: str
    findings: tuple[ReviewFinding, ...]

    def to_dict(self) -> dict[str, Any]:
        """Convert persona review into dictionary format."""
        return {
            "persona": self.persona.value,
            "verdict": self.verdict.value,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class PRReviewReport:
    """Full triage report across all reviewer personas."""

    overall_verdict: ReviewVerdict
    reviews: tuple[PersonaReview, ...]
    total_findings: int

    def to_dict(self) -> dict[str, Any]:
        """Convert report to dictionary format."""
        return {
            "overall_verdict": self.overall_verdict.value,
            "total_findings": self.total_findings,
            "reviews": [r.to_dict() for r in self.reviews],
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize review report to formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent) + "\n"

    def render_markdown(self) -> str:
        """Render review report as structured GitHub PR review markdown."""
        lines = [
            f"## 🤖 Automated PR Triage & Invariant Review: {self.overall_verdict.value}\n",
            f"**Total Findings**: {self.total_findings}\n",
            "| Persona | Verdict | Errors | Warnings | Advisories |",
            "|---|---|---|---|---|",
        ]
        for rev in self.reviews:
            lines.append(_format_table_row(rev))
        lines.append("")
        for rev in self.reviews:
            lines.extend(_format_persona_section(rev))
        return "\n".join(lines).strip() + "\n"


def _format_table_row(rev: PersonaReview) -> str:
    """Format markdown table row for a persona review."""
    counts = {Severity.ERROR: 0, Severity.WARNING: 0, Severity.INFO: 0}
    for f in rev.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return f"| **{rev.persona.value.capitalize()}** | `{rev.verdict.value}` | {counts[Severity.ERROR]} | {counts[Severity.WARNING]} | {counts[Severity.INFO]} |"


def _format_finding_item(finding: ReviewFinding) -> list[str]:
    """Format single finding item with advisory and suggestion."""
    icon_map = {Severity.ERROR: "🔴", Severity.WARNING: "🟡", Severity.INFO: "🔵"}
    icon = icon_map.get(finding.severity, "🔵")
    lines = [f"- {icon} **[{finding.rule_id}]** `{finding.path}:{finding.line}`: {finding.message}"]
    if finding.suggestion:
        lines.append(f"  - *Suggestion*: {finding.suggestion}")
    return lines


def _format_persona_section(rev: PersonaReview) -> list[str]:
    """Format detail section for a single persona review."""
    if not rev.findings:
        return [f"### ✅ {rev.persona.value.capitalize()} Persona: Clean", f"{rev.summary}\n"]
    lines = [
        f"### ⚠️ {rev.persona.value.capitalize()} Persona: {rev.verdict.value}",
        f"{rev.summary}\n",
    ]
    for finding in rev.findings:
        lines.extend(_format_finding_item(finding))
    lines.append("")
    return lines


class SecurityReviewer:
    """Security persona inspecting network egress, secrets, and dangerous calls."""

    def review_file(self, path: Path, text: str) -> list[ReviewFinding]:
        """Scan file content for security vulnerabilities and invariant breaches."""
        findings: list[ReviewFinding] = []
        path_str = str(path)
        self._check_rfc1918_ips(path_str, text, findings)
        self._check_secrets(path_str, text, findings)
        if path.suffix == ".py":
            self._check_python_security(path_str, text, findings)
        return findings

    def _check_rfc1918_ips(self, path: str, text: str, out: list[ReviewFinding]) -> None:
        """Detect prohibited private RFC 1918 IP addresses in file content."""
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = _RFC1918_PATTERN.search(line)
            if match:
                out.append(
                    ReviewFinding(
                        persona=PersonaKind.SECURITY,
                        severity=Severity.ERROR,
                        rule_id="SEC-RFC1918-LEAK",
                        message=f"Prohibited RFC 1918 IP address detected: {match.group(0)!r}",
                        path=path,
                        line=lineno,
                        suggestion="Replace concrete private IPs with documentation abstractions (192.0.2.0/24) or loopback.",
                    )
                )

    def _check_secrets(self, path: str, text: str, out: list[ReviewFinding]) -> None:
        """Detect potential credential or token patterns in text."""
        for lineno, line in enumerate(text.splitlines(), start=1):
            self._scan_line_for_secrets(path, lineno, line, out)

    def _scan_line_for_secrets(self, path: str, lineno: int, line: str, out: list[ReviewFinding]) -> None:
        """Check a single line against all secret regex patterns."""
        for rule_id, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                out.append(
                    ReviewFinding(
                        persona=PersonaKind.SECURITY,
                        severity=Severity.ERROR,
                        rule_id=f"SEC-{rule_id}",
                        message="Potential hardcoded credential or secret key token detected.",
                        path=path,
                        line=lineno,
                        suggestion="Use environment variables or secure keyrings; never store credentials in source.",
                    )
                )

    def _check_python_security(self, path: str, text: str, out: list[ReviewFinding]) -> None:
        """Parse Python AST and identify hazardous execution calls."""
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                self._inspect_security_call(path, node, out)

    def _inspect_security_call(self, path: str, call: ast.Call, out: list[ReviewFinding]) -> None:
        """Identify shell=True or raw dangerous primitives in AST call."""
        func_name = _extract_call_name(call.func)
        if func_name in ("eval", "exec"):
            out.append(
                ReviewFinding(
                    persona=PersonaKind.SECURITY,
                    severity=Severity.ERROR,
                    rule_id="SEC-DANGEROUS-EVAL",
                    message=f"Dangerous dynamic code evaluation via {func_name}()",
                    path=path,
                    line=call.lineno,
                    suggestion="Replace dynamic eval/exec with deterministic AST dispatch or literal evaluation.",
                )
            )
        for keyword in call.keywords:
            if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                out.append(
                    ReviewFinding(
                        persona=PersonaKind.SECURITY,
                        severity=Severity.ERROR,
                        rule_id="SEC-SUBPROCESS-SHELL",
                        message="Subprocess invoked with shell=True exposing injection vulnerabilities.",
                        path=path,
                        line=call.lineno,
                        suggestion="Pass explicit argument lists and set shell=False (default).",
                    )
                )


class ArchitectureReviewer:
    """Architecture persona evaluating cyclomatic complexity, nesting, and assertions."""

    def review_file(self, path: Path, text: str) -> list[ReviewFinding]:
        """Audit Python file against complexity and nesting architectural caps."""
        if path.suffix != ".py":
            return []
        findings: list[ReviewFinding] = []
        path_str = str(path)
        try:
            tree = ast.parse(text)
        except SyntaxError as err:
            findings.append(
                ReviewFinding(
                    persona=PersonaKind.ARCHITECTURE,
                    severity=Severity.ERROR,
                    rule_id="ARCH-SYNTAX-ERROR",
                    message=f"Syntax error: {err.msg}",
                    path=path_str,
                    line=err.lineno or 1,
                    suggestion="Ensure Python code is valid and well-formed.",
                )
            )
            return findings

        self._check_functions(path_str, tree, findings)
        if "test_" in path.name or path.name.endswith("_test.py"):
            self._check_test_assertions(path_str, tree, findings)
        return findings

    def _check_functions(self, path: str, tree: ast.AST, out: list[ReviewFinding]) -> None:
        """Analyze individual function complexity, depth, and parameter cardinality."""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                complexity = _compute_mccabe(node)
                depth = _compute_depth(node)
                param_count = len(node.args.args)
                self._record_function_metrics(path, node, (complexity, depth, param_count), out)

    def _record_function_metrics(
        self,
        path: str,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        metrics: tuple[int, int, int],
        out: list[ReviewFinding],
    ) -> None:
        """Evaluate function metrics against architectural invariant thresholds."""
        complexity, depth, param_count = metrics
        if complexity > 10:
            out.append(
                ReviewFinding(
                    persona=PersonaKind.ARCHITECTURE,
                    severity=Severity.ERROR,
                    rule_id="ARCH-CYCLOMATIC-EXCEEDED",
                    message=f"Function {node.name!r} complexity M={complexity} exceeds maximum ceiling M<=10.",
                    path=path,
                    line=node.lineno,
                    suggestion="Decompose function into single-responsibility helpers or table-driven dispatch.",
                )
            )
        if depth > 5:
            out.append(
                ReviewFinding(
                    persona=PersonaKind.ARCHITECTURE,
                    severity=Severity.ERROR,
                    rule_id="ARCH-NESTING-EXCEEDED",
                    message=f"Function {node.name!r} nesting depth={depth} exceeds maximum ceiling depth<=5.",
                    path=path,
                    line=node.lineno,
                    suggestion="Refactor nested blocks with early returns or guard clauses.",
                )
            )
        if param_count > 4:
            out.append(
                ReviewFinding(
                    persona=PersonaKind.ARCHITECTURE,
                    severity=Severity.WARNING,
                    rule_id="ARCH-PARAM-COUNT",
                    message=f"Function {node.name!r} declares {param_count} parameters exceeding target <=4.",
                    path=path,
                    line=node.lineno,
                    suggestion="Bundle related arguments into a dataclass or typed value object.",
                )
            )

    def _check_test_assertions(self, path: str, tree: ast.AST, out: list[ReviewFinding]) -> None:
        """Identify sequential assertion sprawl that should be consolidated into tuple checks."""
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _count_linear_asserts(node) > 5:
                out.append(
                    ReviewFinding(
                        persona=PersonaKind.ARCHITECTURE,
                        severity=Severity.WARNING,
                        rule_id="ARCH-ASSERTION-SPRAWL",
                        message=f"Test function {node.name!r} contains multiple linear asserts.",
                        path=path,
                        line=node.lineno,
                        suggestion="Consolidate linear asserts into structural tuple checks: assert (a, b) == (exp_a, exp_b).",
                    )
                )


def _count_linear_asserts(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count sequential assert statements in function body."""
    return sum(isinstance(item, ast.Assert) for item in node.body)


class DevOpsReviewer:
    """DevOps persona validating GitHub Actions workflows and CI automation."""

    def review_file(self, path: Path, text: str) -> list[ReviewFinding]:
        """Inspect CI workflow manifests for pin integrity and least privilege."""
        if not (path.suffix in (".yml", ".yaml") and ".github/workflows" in str(path)):
            return []
        findings: list[ReviewFinding] = []
        path_str = str(path)
        lines = text.splitlines()
        self._check_action_pins(path_str, lines, findings)
        self._check_permissions(path_str, lines, findings)
        self._check_script_injections(path_str, lines, findings)
        return findings

    def _check_action_pins(self, path: str, lines: list[str], out: list[ReviewFinding]) -> None:
        """Verify GitHub Actions uses directives are pinned to immutable 40-character SHAs."""
        for lineno, line in enumerate(lines, start=1):
            if _UNPINNED_ACTION_PATTERN.search(line):
                out.append(
                    ReviewFinding(
                        persona=PersonaKind.DEVOPS,
                        severity=Severity.ERROR,
                        rule_id="DEVOPS-UNPINNED-ACTION",
                        message=f"Action step is not pinned to a full 40-character commit SHA: {line.strip()}",
                        path=path,
                        line=lineno,
                        suggestion="Pin action to full 40-character commit SHA with version tag comment (e.g. @<sha> # v4).",
                    )
                )

    def _check_permissions(self, path: str, lines: list[str], out: list[ReviewFinding]) -> None:
        """Verify workflow declares top-level least-privilege permissions."""
        has_permissions = any("permissions:" in line for line in lines)
        if not has_permissions:
            out.append(
                ReviewFinding(
                    persona=PersonaKind.DEVOPS,
                    severity=Severity.WARNING,
                    rule_id="DEVOPS-MISSING-PERMISSIONS",
                    message="Workflow does not declare explicit top-level permissions block.",
                    path=path,
                    line=1,
                    suggestion="Add explicit least-privilege permissions block (e.g. permissions: contents: read).",
                )
            )

    def _check_script_injections(self, path: str, lines: list[str], out: list[ReviewFinding]) -> None:
        """Detect vulnerable GitHub expression interpolations inside inline run scripts."""
        for lineno, line in enumerate(lines, start=1):
            if "${{ github.event." in line and not line.strip().startswith("#"):
                out.append(
                    ReviewFinding(
                        persona=PersonaKind.DEVOPS,
                        severity=Severity.WARNING,
                        rule_id="DEVOPS-EXPRESSION-INJECTION",
                        message="Potential script injection risk: github.event expression directly referenced.",
                        path=path,
                        line=lineno,
                        suggestion="Assign context expression to an intermediate environment variable before use.",
                    )
                )


class QAReviewer:
    """QA persona checking docstring coverage and documentation integrity."""

    def review_file(self, path: Path, text: str) -> list[ReviewFinding]:
        """Scan source files and docs for documentation and contract completeness."""
        findings: list[ReviewFinding] = []
        path_str = str(path)
        if path.suffix == ".py":
            self._check_public_docstrings(path_str, text, findings)
        elif path.suffix == ".md":
            self._check_markdown_integrity(path_str, text, findings)
        return findings

    def _check_public_docstrings(self, path: str, text: str, out: list[ReviewFinding]) -> None:
        """Verify public functions and classes carry complete docstrings."""
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and not node.name.startswith("_")
                and not ast.get_docstring(node)
            ):
                kind = "Class" if isinstance(node, ast.ClassDef) else "Function"
                out.append(
                    ReviewFinding(
                        persona=PersonaKind.QA,
                        severity=Severity.INFO,
                        rule_id="QA-MISSING-DOCSTRING",
                        message=f"Public {kind} {node.name!r} lacks a docstring.",
                        path=path,
                        line=node.lineno,
                        suggestion="Add a concise, descriptive docstring documenting purpose and arguments.",
                    )
                )

    def _check_markdown_integrity(self, path: str, text: str, out: list[ReviewFinding]) -> None:
        """Check for unclosed code fences in Markdown files."""
        fences = [line for line in text.splitlines() if line.strip().startswith("```")]
        if len(fences) % 2 != 0:
            out.append(
                ReviewFinding(
                    persona=PersonaKind.QA,
                    severity=Severity.ERROR,
                    rule_id="QA-UNCLOSED-FENCE",
                    message="Markdown contains an odd number of code fence delimiters (unclosed block).",
                    path=path,
                    line=len(text.splitlines()),
                    suggestion="Ensure every opening ``` code fence has a corresponding closing fence.",
                )
            )


class PRTriageBot:
    """Multi-persona triage and review coordinator."""

    def __init__(self) -> None:
        self.security = SecurityReviewer()
        self.architecture = ArchitectureReviewer()
        self.devops = DevOpsReviewer()
        self.qa = QAReviewer()

    def evaluate_files(self, paths: Iterable[Path]) -> PRReviewReport:
        """Evaluate a set of target files across all reviewer personas."""
        sec_findings: list[ReviewFinding] = []
        arch_findings: list[ReviewFinding] = []
        devops_findings: list[ReviewFinding] = []
        qa_findings: list[ReviewFinding] = []

        for path in paths:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            sec_findings.extend(self.security.review_file(path, text))
            arch_findings.extend(self.architecture.review_file(path, text))
            devops_findings.extend(self.devops.review_file(path, text))
            qa_findings.extend(self.qa.review_file(path, text))

        rev_sec = self._build_persona_review(PersonaKind.SECURITY, sec_findings)
        rev_arch = self._build_persona_review(PersonaKind.ARCHITECTURE, arch_findings)
        rev_dev = self._build_persona_review(PersonaKind.DEVOPS, devops_findings)
        rev_qa = self._build_persona_review(PersonaKind.QA, qa_findings)

        all_reviews = (rev_sec, rev_arch, rev_dev, rev_qa)
        total_count = sum(len(r.findings) for r in all_reviews)
        overall_verdict = self._compute_overall_verdict(all_reviews)

        return PRReviewReport(
            overall_verdict=overall_verdict,
            reviews=all_reviews,
            total_findings=total_count,
        )

    def _build_persona_review(self, persona: PersonaKind, findings: list[ReviewFinding]) -> PersonaReview:
        """Synthesize persona review verdict and summary."""
        errors = [f for f in findings if f.severity == Severity.ERROR]
        warnings = [f for f in findings if f.severity == Severity.WARNING]

        if errors:
            verdict = ReviewVerdict.REQUEST_CHANGES
            summary = f"Identified {len(errors)} error(s) requiring resolution before merge."
        elif warnings:
            verdict = ReviewVerdict.COMMENT
            summary = f"Clean of blocking errors; noted {len(warnings)} advisory warning(s)."
        else:
            verdict = ReviewVerdict.APPROVE
            summary = "All persona quality and architectural invariants passed cleanly."

        return PersonaReview(
            persona=persona,
            verdict=verdict,
            summary=summary,
            findings=tuple(findings),
        )

    def _compute_overall_verdict(self, reviews: tuple[PersonaReview, ...]) -> ReviewVerdict:
        """Compute consolidated PR verdict across all individual persona reviews."""
        verdicts = {r.verdict for r in reviews}
        if ReviewVerdict.REQUEST_CHANGES in verdicts:
            return ReviewVerdict.REQUEST_CHANGES
        if ReviewVerdict.COMMENT in verdicts:
            return ReviewVerdict.COMMENT
        return ReviewVerdict.APPROVE


def _node_decision_points(child: ast.AST) -> int:
    """Calculate cyclomatic decision points contributed by an AST node."""
    if isinstance(
        child,
        (ast.If, ast.While, ast.For, ast.AsyncFor, ast.ExceptHandler, ast.With, ast.AsyncWith, ast.Assert),
    ):
        return 1
    if isinstance(child, ast.BoolOp):
        return len(child.values) - 1
    return 0


def _compute_mccabe(node: ast.AST) -> int:
    """Compute McCabe cyclomatic complexity for an AST node hierarchy."""
    return 1 + sum(_node_decision_points(child) for child in ast.walk(node))


def _compute_depth(node: ast.AST, current: int = 0) -> int:
    """Recursively compute maximum block nesting depth."""
    max_d = current
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try)):
            max_d = max(max_d, _compute_depth(child, current + 1))
        else:
            max_d = max(max_d, _compute_depth(child, current))
    return max_d


def _extract_call_name(node: ast.AST) -> str:
    """Extract identifier name from Call func node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI parser for PR triage bot."""
    parser = argparse.ArgumentParser(
        description="Closed-Loop PR Triage & Invariant Review Bot."
    )
    parser.add_argument(
        "--paths",
        "-p",
        nargs="+",
        type=Path,
        help="List of file paths to triage and audit.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with non-zero status if any persona requests changes.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output instead of Markdown.",
    )
    parser.add_argument(
        "--out",
        "-o",
        type=Path,
        help="Write review output to specified file path instead of stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for PR triage bot."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not args.paths:
        parser.print_help(sys.stderr)
        return 2

    bot = PRTriageBot()
    report = bot.evaluate_files(args.paths)
    output_text = report.to_json() if args.json else report.render_markdown()

    if args.out:
        args.out.write_text(output_text, encoding="utf-8")
    else:
        sys.stdout.write(output_text)

    if args.strict and report.overall_verdict == ReviewVerdict.REQUEST_CHANGES:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
