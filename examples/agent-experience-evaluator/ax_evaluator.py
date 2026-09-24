#!/usr/bin/env python3
"""Agent Experience (AX) Evaluator: Static analysis for agentic usability & low impedance.

Evaluates codebases across three core Agent Experience dimensions:
1. Diagnostic Actionability (DAI): Structured CEGIS error vectors vs. unstructured prose.
2. Interface Friction (IFI): Strict negative schemas vs. permissive kwargs/dictionaries.
3. Cognitive Impedance (CIM): Headroom preservation (M <= 6, depth <= 3).

Computes composite S_AX score, generates SARIF 2.1.0 telemetry, and synthesizes
closed-loop CEGIS remediation guidance.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

DEFAULT_AX_THRESHOLD: Final[float] = 0.75

# Diagnostic rule codes and metadata
RULE_METADATA: Final[dict[str, dict[str, str]]] = {
    "AX001": {
        "name": "PermissiveSchemaHazard",
        "severity": "error",
        "description": "Interface or tool parameter schema lacks negative bounds (additionalProperties: false or extra='forbid'), or accepts unconstrained **kwargs.",
        "remediation": "Enforce negative constraints (additionalProperties: false) or typed Pydantic models with extra='forbid'.",
    },
    "AX002": {
        "name": "UnstructuredDiagnosticHazard",
        "severity": "warning",
        "description": "Exception or diagnostic assertion passes raw unstructured prose rather than structured machine-actionable error vectors.",
        "remediation": "Wrap error details in structured dataclasses, dictionaries, or expected/actual counterexample tuples.",
    },
    "AX003": {
        "name": "HighCognitiveImpedance",
        "severity": "warning",
        "description": "Function exceeds recommended agent cognitive headroom (McCabe M > 6 or nesting depth > 3).",
        "remediation": "Decompose deep branching into dictionary dispatch tables or dedicated single-responsibility helpers.",
    },
}

_IGNORED_PARTS: Final[frozenset[str]] = frozenset({"venv", "build", "dist", "__pycache__", ".git", ".data"})


@dataclass(frozen=True)
class AXFinding:
    """A single Agent Experience violation or friction hotspot."""

    rule_id: str
    file_path: str
    line_number: int
    symbol_name: str
    message: str
    remediation_hint: str


@dataclass
class ModuleTelemetry:
    """Raw telemetry metrics gathered from an AST traversal."""

    total_diagnostics: int = 0
    structured_diagnostics: int = 0
    total_schemas: int = 0
    permissive_schemas: int = 0
    total_functions: int = 0
    high_impedance_functions: int = 0
    total_complexity: int = 0
    max_depth: int = 0
    findings: list[AXFinding] = field(default_factory=list)


@dataclass(frozen=True)
class AXScoreReport:
    """Composite evaluation report for a codebase or file set."""

    files_analyzed: int
    dai: float  # Diagnostic Actionability Index [0, 1]
    ifi: float  # Interface Friction Index [0, 1]
    cim: float  # Cognitive Impedance Metric [0, 1]
    composite_score: float
    threshold: float
    passed: bool
    findings_count: int
    findings: list[AXFinding]


class ComplexityAndDepthVisitor(ast.NodeVisitor):
    """Calculates McCabe cyclomatic complexity and maximum block nesting depth."""

    __slots__ = ("complexity", "current_depth", "max_depth")

    def __init__(self) -> None:
        self.complexity: int = 1
        self.current_depth: int = 0
        self.max_depth: int = 0

    def _enter_block(self, node: ast.AST) -> None:
        self.current_depth += 1
        if self.current_depth > self.max_depth:
            self.max_depth = self.current_depth
        self.generic_visit(node)
        self.current_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        self.complexity += 1
        self._enter_block(node)

    def visit_For(self, node: ast.For) -> None:
        self.complexity += 1
        self._enter_block(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.complexity += 1
        self._enter_block(node)

    def visit_While(self, node: ast.While) -> None:
        self.complexity += 1
        self._enter_block(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.complexity += 1
        self._enter_block(node)

    def visit_With(self, node: ast.With) -> None:
        self._enter_block(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._enter_block(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.complexity += len(node.values) - 1
        self.generic_visit(node)


def calculate_function_metrics(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
    """Calculate cyclomatic complexity and maximum block nesting for a function."""
    visitor = ComplexityAndDepthVisitor()
    for stmt in fn_node.body:
        visitor.visit(stmt)
    return visitor.complexity, visitor.max_depth


def _extract_dict_str_keys(node: ast.Dict) -> set[str]:
    """Extract string literal keys from an AST dict node."""
    found: set[str] = set()
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            found.add(k.value)
    return found


def _is_schema_dict(keys: set[str], node: ast.Dict) -> bool:
    """Check if a dictionary AST node represents a tool or JSON object schema."""
    if "properties" in keys:
        return True
    if "type" not in keys:
        return False
    return any(isinstance(v, ast.Constant) and v.value == "object" for v in node.values)


def _is_unstructured_raise(exc: ast.expr) -> bool:
    """Check if exception instantiation passes raw unstructured text."""
    if not isinstance(exc, ast.Call) or not exc.args or exc.keywords:
        return False
    if len(exc.args) != 1:
        return False
    return isinstance(exc.args[0], (ast.Constant, ast.JoinedStr))


class AgentExperienceAuditor(ast.NodeVisitor):
    """Audits Python AST for schema permissiveness, unstructured errors, and cognitive impedance."""

    def __init__(self, file_path: str) -> None:
        self.file_path: str = file_path
        self.telemetry = ModuleTelemetry()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._audit_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._audit_function(node)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        self._audit_raise(node)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._audit_assert(node)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        self._audit_dict_schema(node)
        self.generic_visit(node)

    def _check_kwarg_boundaries(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Check for untyped keyword-only arguments and unconstrained **kwargs."""
        for arg in node.args.kwonlyargs:
            if arg.annotation is None:
                self._record_schema_violation(node.lineno, node.name, "Untyped keyword-only parameter")
        if node.args.kwarg and node.args.kwarg.annotation is None:
            self.telemetry.total_schemas += 1
            self.telemetry.permissive_schemas += 1
            self.telemetry.findings.append(
                AXFinding(
                    rule_id="AX001",
                    file_path=self.file_path,
                    line_number=node.lineno,
                    symbol_name=node.name,
                    message=f"Function '{node.name}' accepts untyped **kwargs without boundary constraints.",
                    remediation_hint=RULE_METADATA["AX001"]["remediation"],
                )
            )

    def _check_cognitive_headroom(self, name: str, lineno: int, complexity: int, depth: int) -> None:
        """Record finding if function exceeds recommended cognitive headroom."""
        if complexity <= 6 and depth <= 3:
            return
        self.telemetry.high_impedance_functions += 1
        self.telemetry.findings.append(
            AXFinding(
                rule_id="AX003",
                file_path=self.file_path,
                line_number=lineno,
                symbol_name=name,
                message=f"Function '{name}' has M={complexity} and depth={depth} (exceeds recommended M<=6, depth<=3).",
                remediation_hint=RULE_METADATA["AX003"]["remediation"],
            )
        )

    def _audit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.telemetry.total_functions += 1
        complexity, depth = calculate_function_metrics(node)
        self.telemetry.total_complexity += complexity
        if depth > self.telemetry.max_depth:
            self.telemetry.max_depth = depth
        self._check_kwarg_boundaries(node)
        self._check_cognitive_headroom(node.name, node.lineno, complexity, depth)

    def _audit_raise(self, node: ast.Raise) -> None:
        self.telemetry.total_diagnostics += 1
        if node.exc is not None and _is_unstructured_raise(node.exc):
            self.telemetry.findings.append(
                AXFinding(
                    rule_id="AX002",
                    file_path=self.file_path,
                    line_number=node.lineno,
                    symbol_name="<raise>",
                    message="Exception passes unstructured prose string without structured context attributes.",
                    remediation_hint=RULE_METADATA["AX002"]["remediation"],
                )
            )
            return
        self.telemetry.structured_diagnostics += 1

    def _audit_assert(self, node: ast.Assert) -> None:
        if node.msg is None:
            return
        self.telemetry.total_diagnostics += 1
        if isinstance(node.msg, (ast.Constant, ast.JoinedStr)):
            self.telemetry.findings.append(
                AXFinding(
                    rule_id="AX002",
                    file_path=self.file_path,
                    line_number=node.lineno,
                    symbol_name="<assert>",
                    message="Assertion diagnostic passes raw text string instead of structural expected/actual tuple.",
                    remediation_hint=RULE_METADATA["AX002"]["remediation"],
                )
            )
        else:
            self.telemetry.structured_diagnostics += 1

    def _audit_dict_schema(self, node: ast.Dict) -> None:
        """Detect schema dictionaries and check for negative constraints."""
        keys = _extract_dict_str_keys(node)
        if not _is_schema_dict(keys, node):
            return
        self.telemetry.total_schemas += 1
        if "additionalProperties" not in keys and "extra" not in keys:
            self.telemetry.permissive_schemas += 1
            self.telemetry.findings.append(
                AXFinding(
                    rule_id="AX001",
                    file_path=self.file_path,
                    line_number=node.lineno,
                    symbol_name="<schema_dict>",
                    message="JSON/Tool schema defines object properties but omits 'additionalProperties: false'.",
                    remediation_hint=RULE_METADATA["AX001"]["remediation"],
                )
            )

    def _record_schema_violation(self, line: int, name: str, reason: str) -> None:
        self.telemetry.total_schemas += 1
        self.telemetry.permissive_schemas += 1
        self.telemetry.findings.append(
            AXFinding(
                rule_id="AX001",
                file_path=self.file_path,
                line_number=line,
                symbol_name=name,
                message=f"Schema in '{name}' is ambiguous: {reason}.",
                remediation_hint=RULE_METADATA["AX001"]["remediation"],
            )
        )


def evaluate_file(file_path: Path) -> ModuleTelemetry:
    """Parse and audit a single Python source file."""
    telemetry = ModuleTelemetry()
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return telemetry

    auditor = AgentExperienceAuditor(str(file_path))
    auditor.visit(tree)
    return auditor.telemetry


@dataclass
class _AggregateTotals:
    """Aggregated raw counts across telemetry modules."""

    total_diag: int = 0
    struct_diag: int = 0
    total_schemas: int = 0
    perm_schemas: int = 0
    total_fns: int = 0
    high_imp_fns: int = 0
    findings: list[AXFinding] = field(default_factory=list)


def _accumulate_telemetry(telemetry_list: Sequence[ModuleTelemetry]) -> _AggregateTotals:
    """Accumulate raw totals across all module telemetry records."""
    agg = _AggregateTotals()
    for t in telemetry_list:
        agg.total_diag += t.total_diagnostics
        agg.struct_diag += t.structured_diagnostics
        agg.total_schemas += t.total_schemas
        agg.perm_schemas += t.permissive_schemas
        agg.total_fns += t.total_functions
        agg.high_imp_fns += t.high_impedance_functions
        agg.findings.extend(t.findings)
    return agg


def _safe_ratio(numerator: int, denominator: int, default: float) -> float:
    """Compute ratio safely handling zero denominator."""
    if denominator == 0:
        return default
    return numerator / denominator


def _has_permissive_schema(findings: Sequence[AXFinding]) -> bool:
    """Check if any finding represents an unconstrained schema violation."""
    return any(f.rule_id == "AX001" for f in findings)


def compute_aggregate_scores(telemetry_list: Sequence[ModuleTelemetry], threshold: float = DEFAULT_AX_THRESHOLD) -> AXScoreReport:
    """Aggregate telemetry across multiple files and compute the composite S_AX score."""
    agg = _accumulate_telemetry(telemetry_list)
    dai = _safe_ratio(agg.struct_diag, agg.total_diag, 1.0)
    ifi = _safe_ratio(agg.perm_schemas, agg.total_schemas, 0.0)
    cim = 1.0 - _safe_ratio(agg.high_imp_fns, agg.total_fns, 0.0)

    composite = 0.40 * dai + 0.30 * (1.0 - ifi) + 0.30 * cim
    passed = composite >= threshold and not _has_permissive_schema(agg.findings)

    return AXScoreReport(
        files_analyzed=len(telemetry_list),
        dai=round(dai, 3),
        ifi=round(ifi, 3),
        cim=round(cim, 3),
        composite_score=round(composite, 3),
        threshold=threshold,
        passed=passed,
        findings_count=len(agg.findings),
        findings=agg.findings,
    )


def generate_sarif_report(report: AXScoreReport) -> dict[str, Any]:
    """Format AX findings into standard OASIS SARIF 2.1.0 output."""
    rules = [
        {
            "id": rule_id,
            "name": meta["name"],
            "shortDescription": {"text": meta["name"]},
            "fullDescription": {"text": meta["description"]},
            "defaultConfiguration": {"level": meta["severity"]},
            "help": {"text": meta["remediation"]},
        }
        for rule_id, meta in RULE_METADATA.items()
    ]

    results = [
        {
            "ruleId": f.rule_id,
            "message": {"text": f"{f.message} (Remediation: {f.remediation_hint})"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f.file_path},
                        "region": {"startLine": f.line_number},
                    }
                }
            ],
        }
        for f in report.findings
    ]

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "ax-evaluator",
                        "version": "1.0.0",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def render_markdown_summary(report: AXScoreReport) -> str:
    """Render a human- and agent-readable Markdown summary report."""
    status_emoji = "✅ PASSED" if report.passed else "❌ FAILED"
    lines = [
        "# Agent Experience (AX) Evaluation Summary",
        "",
        f"**Verdict**: {status_emoji} (Score: **{report.composite_score:.2f}** / Target: **{report.threshold:.2f}**)",
        "",
        "| Evaluation Metric | Observed | Target / Best | Meaning |",
        "|---|---|---|---|",
        f"| **Diagnostic Actionability (DAI)** | `{report.dai:.3f}` | `>= 0.800` | Ratio of structured error vectors to unstructured prose |",
        f"| **Interface Friction Index (IFI)** | `{report.ifi:.3f}` | `<= 0.100` | Rate of permissive or unconstrained tool parameters |",
        f"| **Cognitive Impedance (CIM)** | `{report.cim:.3f}` | `>= 0.850` | Structural headroom ratio (functions with M <= 6, depth <= 3) |",
        f"| **Total Files Analyzed** | `{report.files_analyzed}` | — | Evaluated Python source modules |",
        f"| **Total AX Findings** | `{report.findings_count}` | `0` | Discovered friction hotspots and schema risks |",
        "",
    ]

    if report.findings:
        lines.extend([
            "## Discovered Friction Hotspots",
            "",
            "| Rule | File | Line | Symbol | Message & Remediation |",
            "|---|---|---|---|---|",
        ])
        for f in report.findings[:30]:
            lines.append(f"| `{f.rule_id}` | `{f.file_path}` | `{f.line_number}` | `{f.symbol_name}` | {f.message} |")

        if len(report.findings) > 30:
            lines.append(f"\n*(Truncated {len(report.findings) - 30} additional findings)*")

    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with total input coverage (nargs='*')."""
    parser = argparse.ArgumentParser(
        description="Static analyzer evaluating codebase Agent Experience (AX) and cognitive impedance."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Source files or directories to evaluate (default: current directory).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "sarif", "markdown"],
        default="text",
        help="Output report format (default: text).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional destination file path for output.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_AX_THRESHOLD,
        help=f"Minimum composite S_AX score required to pass (default: {DEFAULT_AX_THRESHOLD}).",
    )
    return parser


def _is_ignored_path(path: Path) -> bool:
    """Check if any path component starts with a dot or is in ignored directories."""
    return any(part.startswith(".") or part in _IGNORED_PARTS for part in path.parts)


def collect_target_files(paths: Sequence[str]) -> list[Path]:
    """Collect all Python source files from arguments, ignoring hidden directories."""
    targets: list[Path] = []
    for item in paths:
        path = Path(item)
        if path.is_file() and path.suffix == ".py":
            targets.append(path)
        elif path.is_dir():
            targets.extend(p for p in path.rglob("*.py") if not _is_ignored_path(p))
    return sorted(set(targets))


def _format_report(report: AXScoreReport, output_format: str) -> str:
    """Format report according to requested serialization format."""
    if output_format == "json":
        return json.dumps(asdict(report), indent=2)
    if output_format == "sarif":
        return json.dumps(generate_sarif_report(report), indent=2)
    if output_format == "markdown":
        return render_markdown_summary(report)
    status_str = "PASSED" if report.passed else "FAILED"
    return (
        f"Agent Experience (AX) Evaluation: {status_str}\n"
        f"Composite S_AX: {report.composite_score:.2f} (Threshold: {report.threshold:.2f})\n"
        f"  - DAI (Diagnostic Actionability): {report.dai:.3f}\n"
        f"  - IFI (Interface Friction):       {report.ifi:.3f}\n"
        f"  - CIM (Cognitive Impedance):      {report.cim:.3f}\n"
        f"Files Analyzed: {report.files_analyzed} | Findings: {report.findings_count}\n"
    )


def main() -> int:
    """CLI execution entry point."""
    parser = build_parser()
    args = parser.parse_args()

    files = collect_target_files(args.paths)
    if not files:
        sys.stderr.write("No Python files located for evaluation.\n")
        return 0

    telemetry_list = [evaluate_file(f) for f in files]
    report = compute_aggregate_scores(telemetry_list, threshold=args.threshold)

    output = _format_report(report, args.format)
    if args.out:
        args.out.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
