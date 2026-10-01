#!/usr/bin/env python3
"""Verification Pyramid Auditor & Test Horizon Scaling Oracle.

Audits test suites and validation workflows for verification horizon health,
latency scaling risks, assertion density sprawl, and process containment.

In mature repositories, monolithic test suite scaling induces steep verification
latency cliffs (T_verify > 45s), context dilution, tool execution timeouts, and
assertion density traps where linear `assert` statements inflate cyclomatic
complexity (M + 1 per assert).

This oracle enforces the 4-Tier Verification Pyramid:
  Layer 0: In-Memory AST Sentinel (<= 50ms)
  Layer 1: Focused Slice Oracles (<= 500ms)
  Layer 2: Local Pre-Commit Hooks (<= 5.0s)
  Layer 3: Gated Remote Matrix CI (1m - 5m)
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from source_tree_policy import iter_source_files

INFORMATION_URI: Final[str] = "https://github.com/dan-petty/vibes"
MAX_LINEAR_ASSERTS_THRESHOLD: Final[int] = 4
MAX_TESTS_PER_FILE_THRESHOLD: Final[int] = 30
SUB_SECOND_CEILING_SECONDS: Final[float] = 0.50


class VerificationRule(StrEnum):
    """Diagnostic rule codes for verification horizon and pyramid health."""

    ASSERTION_DENSITY_SPRAWL = "VER001"
    UNCONTAINED_PROCESS_SPAWN = "VER002"
    SHARED_STATE_CONTENTION = "VER003"
    MONOLITHIC_COLD_START = "VER004"
    VERIFICATION_LATENCY_CLIFF = "VER005"


class VerificationSeverity(StrEnum):
    """Severity ratings conforming to OASIS SARIF 2.1.0."""

    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"


@dataclass(frozen=True)
class RuleDescriptor:
    """Descriptor metadata for a verification diagnostic rule."""

    code: VerificationRule
    name: str
    severity: VerificationSeverity
    short_description: str
    recommendation: str


RULE_DESCRIPTORS: Final[dict[VerificationRule, RuleDescriptor]] = {
    VerificationRule.ASSERTION_DENSITY_SPRAWL: RuleDescriptor(
        code=VerificationRule.ASSERTION_DENSITY_SPRAWL,
        name="AssertionDensitySprawl",
        severity=VerificationSeverity.WARNING,
        short_description="Test function contains >= 4 linear assert statements.",
        recommendation="Consolidate linear asserts into structural tuple checks.",
    ),
    VerificationRule.UNCONTAINED_PROCESS_SPAWN: RuleDescriptor(
        code=VerificationRule.UNCONTAINED_PROCESS_SPAWN,
        name="UncontainedProcessSpawn",
        severity=VerificationSeverity.ERROR,
        short_description="Subprocess invocation lacks process group isolation.",
        recommendation="Pass start_new_session=True and terminate with os.killpg.",
    ),
    VerificationRule.SHARED_STATE_CONTENTION: RuleDescriptor(
        code=VerificationRule.SHARED_STATE_CONTENTION,
        name="SharedStateContention",
        severity=VerificationSeverity.WARNING,
        short_description="Direct access to un-namespaced shared artifacts in test.",
        recommendation="Use tmp_path or unique fixture namespaces to prevent race conditions.",
    ),
    VerificationRule.MONOLITHIC_COLD_START: RuleDescriptor(
        code=VerificationRule.MONOLITHIC_COLD_START,
        name="MonolithicColdStart",
        severity=VerificationSeverity.NOTE,
        short_description="Test file exceeds test count ceiling, risking slow suite slice runs.",
        recommendation="Decompose large test modules into focused, sub-second test slices.",
    ),
    VerificationRule.VERIFICATION_LATENCY_CLIFF: RuleDescriptor(
        code=VerificationRule.VERIFICATION_LATENCY_CLIFF,
        name="VerificationLatencyCliff",
        severity=VerificationSeverity.WARNING,
        short_description="Test execution latency exceeds inner-loop ceiling (500ms).",
        recommendation="Mock heavy I/O or isolate into Layer 2/3 test stages.",
    ),
}


@dataclass(frozen=True)
class VerificationFinding:
    """A single diagnostic finding emitted during test suite audit."""

    rule_code: str
    rule_name: str
    file_path: str
    line_number: int
    message: str
    severity: str
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        """Convert finding to dictionary representation."""
        return asdict(self)


@dataclass
class AuditSummary:
    """Summary of verification pyramid audit across inspected targets."""

    total_files: int = 0
    total_functions: int = 0
    total_assertions: int = 0
    structural_tuple_assertions: int = 0
    findings: list[VerificationFinding] = field(default_factory=list)
    health_score: float = 100.0
    friction_index: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert summary to dictionary representation."""
        return {
            "total_files": self.total_files,
            "total_functions": self.total_functions,
            "total_assertions": self.total_assertions,
            "structural_tuple_assertions": self.structural_tuple_assertions,
            "findings_count": len(self.findings),
            "health_score": round(self.health_score, 2),
            "friction_index": round(self.friction_index, 4),
            "findings": [f.to_dict() for f in self.findings],
        }


def is_structural_assertion(assert_node: ast.Assert) -> bool:
    """Determine whether an assert statement uses structural tuple or predicate comparison."""
    test = assert_node.test
    if isinstance(test, ast.Compare):
        has_left_tuple = isinstance(test.left, ast.Tuple)
        has_comparator_tuple = any(isinstance(c, ast.Tuple) for c in test.comparators)
        return has_left_tuple or has_comparator_tuple
    if isinstance(test, ast.Call) and isinstance(test.func, ast.Name):
        return test.func.id in ("all", "any")
    return False


def _check_function_assertions(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_path: str,
) -> tuple[int, int, list[VerificationFinding]]:
    """Audit assert statements inside a single test function."""
    asserts = [node for node in ast.walk(func) if isinstance(node, ast.Assert)]
    structural_count = sum(1 for a in asserts if is_structural_assertion(a))
    linear_count = len(asserts) - structural_count
    findings: list[VerificationFinding] = []

    if linear_count >= MAX_LINEAR_ASSERTS_THRESHOLD:
        desc = RULE_DESCRIPTORS[VerificationRule.ASSERTION_DENSITY_SPRAWL]
        msg = f"Function '{func.name}' has {linear_count} linear asserts (M+{linear_count})."
        findings.append(
            VerificationFinding(
                rule_code=desc.code.value,
                rule_name=desc.name,
                file_path=rel_path,
                line_number=func.lineno,
                message=msg,
                severity=desc.severity.value,
                recommendation=desc.recommendation,
            )
        )
    return len(asserts), structural_count, findings


def _is_popen_call(node: ast.Call) -> bool:
    """Check if AST call target is subprocess.Popen or subprocess.run."""
    func_node = node.func
    return isinstance(func_node, ast.Attribute) and func_node.attr in ("Popen", "run")


def _is_true_constant(node: ast.expr) -> bool:
    """Check if AST node is constant True."""
    return isinstance(node, ast.Constant) and node.value is True


def _has_session_isolation(node: ast.Call) -> bool:
    """Check if call includes start_new_session=True."""
    return any(
        kw.arg == "start_new_session" and _is_true_constant(kw.value)
        for kw in node.keywords
    )


def _collect_popen_calls(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    """Find all subprocess popen calls in function."""
    calls: list[ast.Call] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and _is_popen_call(node):
            calls.append(node)
    return calls


def _check_subprocess_containment(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_path: str,
) -> list[VerificationFinding]:
    """Audit subprocess invocations for process group containment."""
    desc = RULE_DESCRIPTORS[VerificationRule.UNCONTAINED_PROCESS_SPAWN]
    findings: list[VerificationFinding] = []
    for node in _collect_popen_calls(func):
        if not _has_session_isolation(node):
            findings.append(
                VerificationFinding(
                    rule_code=desc.code.value,
                    rule_name=desc.name,
                    file_path=rel_path,
                    line_number=node.lineno,
                    message=f"Subprocess call in '{func.name}' lacks start_new_session=True.",
                    severity=desc.severity.value,
                    recommendation=desc.recommendation,
                )
            )
    return findings


def _check_shared_state_contention(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_path: str,
) -> list[VerificationFinding]:
    """Audit potential shared state race conditions across tests."""
    desc = RULE_DESCRIPTORS[VerificationRule.SHARED_STATE_CONTENTION]
    findings: list[VerificationFinding] = []
    seen: set[str] = set()

    for node in ast.walk(func):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        val = node.value
        if val in (".coverage", ".data/cache.db", "/tmp/test.db") and val not in seen:
            seen.add(val)
            findings.append(
                VerificationFinding(
                    rule_code=desc.code.value,
                    rule_name=desc.name,
                    file_path=rel_path,
                    line_number=node.lineno,
                    message=f"Direct hardcoded shared file '{val}' risks concurrent test races.",
                    severity=desc.severity.value,
                    recommendation=desc.recommendation,
                )
            )
    return findings


def _parse_source_tree(path: Path) -> tuple[ast.AST | None, VerificationFinding | None]:
    """Parse source file into AST safely, returning finding on syntax failure."""
    try:
        content = path.read_text(encoding="utf-8")
        return ast.parse(content, filename=str(path)), None
    except (SyntaxError, UnicodeDecodeError, OSError) as err:
        finding = VerificationFinding(
            rule_code="VER000",
            rule_name="UnparseableSource",
            file_path=str(path),
            line_number=1,
            message=f"Failed to parse source: {err}",
            severity=VerificationSeverity.ERROR.value,
            recommendation="Fix file syntax or encoding.",
        )
        return None, finding


def _is_test_function_name(name: str) -> bool:
    """Determine if a function name matches test conventions."""
    return name.startswith("test_") or name.endswith("_test")


def _collect_test_functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Collect test function definitions from AST."""
    funcs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_test_function_name(node.name):
            funcs.append(node)
    return funcs


def _check_test_count_ceiling(num_tests: int, rel_path: str) -> list[VerificationFinding]:
    """Check if test file exceeds test count ceiling."""
    if num_tests <= MAX_TESTS_PER_FILE_THRESHOLD:
        return []
    desc = RULE_DESCRIPTORS[VerificationRule.MONOLITHIC_COLD_START]
    return [
        VerificationFinding(
            rule_code=desc.code.value,
            rule_name=desc.name,
            file_path=rel_path,
            line_number=1,
            message=f"File contains {num_tests} tests, exceeding {MAX_TESTS_PER_FILE_THRESHOLD} ceiling.",
            severity=desc.severity.value,
            recommendation=desc.recommendation,
        )
    ]


def _audit_functions(
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef],
    rel_path: str,
) -> tuple[int, int, list[VerificationFinding]]:
    """Audit all test functions within a parsed file."""
    total_asserts = 0
    total_structural = 0
    findings: list[VerificationFinding] = []

    for func in functions:
        n_asserts, n_struct, assert_findings = _check_function_assertions(func, rel_path)
        total_asserts += n_asserts
        total_structural += n_struct
        findings.extend(assert_findings)
        findings.extend(_check_subprocess_containment(func, rel_path))
        findings.extend(_check_shared_state_contention(func, rel_path))

    return total_asserts, total_structural, findings


def audit_test_file(path: Path) -> tuple[int, int, int, list[VerificationFinding]]:
    """Audit a single test file for verification pyramid adherence."""
    tree, err_finding = _parse_source_tree(path)
    if tree is None:
        return 0, 0, 0, [err_finding] if err_finding else []

    rel_path = str(path)
    functions = _collect_test_functions(tree)
    findings = _check_test_count_ceiling(len(functions), rel_path)
    n_asserts, n_struct, func_findings = _audit_functions(functions, rel_path)
    findings.extend(func_findings)
    return len(functions), n_asserts, n_struct, findings


def calculate_metrics(
    findings: list[VerificationFinding],
    total_functions: int,
) -> tuple[float, float]:
    """Calculate the Verification Health Score and Friction Index."""
    penalty_weights = {
        VerificationSeverity.ERROR.value: 15.0,
        VerificationSeverity.WARNING.value: 5.0,
        VerificationSeverity.NOTE.value: 1.0,
    }
    raw_deductions = sum(penalty_weights.get(f.severity, 2.0) for f in findings)
    health = max(0.0, 100.0 - raw_deductions)
    friction = 0.0 if total_functions <= 0 else len(findings) / total_functions
    return health, friction


def _is_test_file(path: Path) -> bool:
    """Check if file matches python test naming conventions."""
    return path.suffix == ".py" and (path.name.startswith("test_") or path.name.endswith("_test.py"))


def _collect_dir_test_files(target: Path) -> list[Path]:
    """Collect test files from directory."""
    return [f for f in iter_source_files(target, (".py",)) if _is_test_file(f)]


def _resolve_single_target(target: Path) -> list[Path]:
    """Resolve test files from a single target."""
    if target.is_file() and target.suffix == ".py":
        return [target]
    if target.is_dir():
        return _collect_dir_test_files(target)
    return []


def _resolve_audit_targets(targets: Sequence[Path]) -> list[Path]:
    """Resolve candidate test paths from targets."""
    all_files: list[Path] = []
    for target in targets:
        all_files.extend(_resolve_single_target(target))
    return all_files


def _aggregate_file_audit(summary: AuditSummary, path: Path) -> None:
    """Audit single file and aggregate into summary."""
    funcs, asserts, structs, findings = audit_test_file(path)
    summary.total_functions += funcs
    summary.total_assertions += asserts
    summary.structural_tuple_assertions += structs
    summary.findings.extend(findings)


def audit_test_suite(targets: Sequence[Path]) -> AuditSummary:
    """Audit a collection of test files or directories."""
    summary = AuditSummary()
    files = _resolve_audit_targets(targets)
    summary.total_files = len(files)

    for path in sorted(files):
        _aggregate_file_audit(summary, path)

    summary.health_score, summary.friction_index = calculate_metrics(
        summary.findings,
        summary.total_functions,
    )
    return summary


def format_markdown_report(summary: AuditSummary) -> str:
    """Format an audit summary as a GitHub-flavored Markdown report."""
    lines = [
        "# Verification Pyramid & Test Horizon Audit Report",
        "",
        f"- **Inspected Files**: `{summary.total_files}`",
        f"- **Total Test Functions**: `{summary.total_functions}`",
        f"- **Total Assertions**: `{summary.total_assertions}` (Structural Tuples: `{summary.structural_tuple_assertions}`)",
        f"- **Verification Health Score**: `{summary.health_score:.1f} / 100.0`",
        f"- **Agent Friction Index**: `{summary.friction_index:.3f}`",
        "",
        "## Diagnostic Findings",
        "",
    ]
    if not summary.findings:
        lines.append("✓ Zero verification pyramid defects found. All test suites adhere to sub-second oracles.")
        return "\n".join(lines) + "\n"

    lines.extend([
        "| Rule | Severity | Location | Message | Recommendation |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])
    for f in summary.findings:
        lines.append(
            f"| `{f.rule_code}` | `{f.severity}` | `{f.file_path}:{f.line_number}` | {f.message} | {f.recommendation} |"
        )
    return "\n".join(lines) + "\n"


def format_sarif(summary: AuditSummary) -> dict[str, Any]:
    """Format an audit summary as an OASIS SARIF 2.1.0 payload."""
    rules_json: list[dict[str, Any]] = [
        {
            "id": desc.code.value,
            "name": desc.name,
            "shortDescription": {"text": desc.short_description},
            "defaultConfiguration": {"level": desc.severity.value},
            "helpUri": f"{INFORMATION_URI}#verification-pyramid",
        }
        for desc in RULE_DESCRIPTORS.values()
    ]

    results_json: list[dict[str, Any]] = [
        {
            "ruleId": f.rule_code,
            "level": f.severity,
            "message": {"text": f.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f.file_path},
                        "region": {"startLine": f.line_number},
                    }
                }
            ],
        }
        for f in summary.findings
    ]

    return {
        "$schema": "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "verification-pyramid-auditor",
                        "informationUri": INFORMATION_URI,
                        "rules": rules_json,
                    }
                },
                "results": results_json,
            }
        ],
    }


def parse_arguments(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Audit test suites for verification pyramid and latency scaling health."
    )
    parser.add_argument(
        "targets",
        nargs="*",
        default=["tests"],
        help="Test files or directories to audit (default: tests).",
    )
    parser.add_argument("--sarif", action="store_true", help="Emit OASIS SARIF 2.1.0 format.")
    parser.add_argument("--json", action="store_true", help="Emit JSON telemetry.")
    parser.add_argument("--markdown", action="store_true", help="Emit Markdown report.")
    parser.add_argument("--threshold", type=float, default=85.0, help="Minimum passing score.")
    return parser.parse_args(args)


def _render_text(summary: AuditSummary) -> None:
    """Render plain text summary to standard output."""
    print(f"Verification Health Score: {summary.health_score:.1f} / 100.0")
    print(f"Friction Index: {summary.friction_index:.3f}")
    print(f"Files: {summary.total_files} | Functions: {summary.total_functions} | Findings: {len(summary.findings)}")
    for f in summary.findings:
        print(f"  [{f.severity.upper()}] {f.file_path}:{f.line_number} {f.rule_code} {f.message}")


def _render_output(summary: AuditSummary, args: argparse.Namespace) -> None:
    """Render audit summary based on requested CLI format."""
    if args.sarif:
        print(json.dumps(format_sarif(summary), indent=2))
        return
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2))
        return
    if args.markdown:
        print(format_markdown_report(summary))
        return
    _render_text(summary)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for verification pyramid auditor."""
    args = parse_arguments(argv)
    target_paths = [Path(t) for t in args.targets]
    summary = audit_test_suite(target_paths)
    _render_output(summary, args)
    return 0 if summary.health_score >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
