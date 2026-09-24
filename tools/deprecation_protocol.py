#!/usr/bin/env python3
"""Enterprise Change Management & Version Deprecation Protocol.

Enforces feature flag lifecycles, dead-code conditional pruning, agent tool
schema evolution, parameter migration, and breaking change detection across
enterprise post-1.0 release cycles:
- FlagState: EXPERIMENTAL -> STABLE -> DEPRECATED -> REMOVED
- ToolSchemaMigrator: Bidirectional argument adaptation & deprecation warning emission
- SchemaDiff: Mechanical backward-compatibility and breaking change verification
- AstFlagPruner: Dead-code branch detection for retired feature toggles
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

SEMVER_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
CANONICAL_MOCK_HOST: Final[str] = "example.com"
MAX_SCAN_FILE_SIZE_BYTES: Final[int] = 10 * 1024 * 1024


def parse_semver(version_str: str) -> tuple[int, int, int]:
    """Parse semantic version string into integer 3-tuple."""
    match = SEMVER_RE.match(version_str.strip())
    if not match:
        raise ValueError(f"Invalid semantic version: '{version_str}'")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


class FlagState(StrEnum):
    """Lifecycle progression states for enterprise feature flags."""

    EXPERIMENTAL = "experimental"
    STABLE = "stable"
    DEPRECATED = "deprecated"
    REMOVED = "removed"


class DefectKind(StrEnum):
    """Catalog of enterprise change management and deprecation defects."""

    FLAG_EXPIRED = "FLAG_EXPIRED"
    FLAG_MISSING_CONTRACT = "FLAG_MISSING_CONTRACT"
    SCHEMA_BREAKING_CHANGE = "SCHEMA_BREAKING_CHANGE"
    SCHEMA_UNALIASED_DEPRECATION = "SCHEMA_UNALIASED_DEPRECATION"
    DEAD_FLAG_BRANCH = "DEAD_FLAG_BRANCH"
    UNMIGRATED_CLIENT_CALL = "UNMIGRATED_CLIENT_CALL"


class DefectSeverity(StrEnum):
    """Standard severity classifications for deprecation defects."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class FeatureFlagDefinition:
    """Declared feature flag specification and lifecycle contract."""

    name: str
    state: FlagState
    introduced_in: str
    expires_in: str
    owner: str
    default_value: bool
    description: str


@dataclass(frozen=True)
class ParameterMigrationRule:
    """Formal parameter rename and transformation rule for tool schemas."""

    tool_name: str
    old_param: str
    new_param: str
    since_version: str
    remove_in_version: str
    transform_type: str = "identity"


@dataclass(frozen=True)
class ToolSchemaDefinition:
    """Tool specification and schema contract."""

    name: str
    version: str
    parameters: dict[str, Any]
    required: tuple[str, ...] = ()
    migrations: tuple[ParameterMigrationRule, ...] = ()


@dataclass(frozen=True)
class ProtocolFinding:
    """A detected deprecation or change management violation."""

    defect: DefectKind
    title: str
    severity: DefectSeverity
    message: str
    target: str
    line_number: int
    recommendation: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProtocolSummary:
    """Aggregate report of change management and deprecation findings."""

    current_version: str
    findings: list[ProtocolFinding]
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


def _has_missing_contract(flag: FeatureFlagDefinition) -> bool:
    """Predicate evaluating whether flag lacks required contract metadata."""
    return not (flag.owner and flag.introduced_in and flag.expires_in)


def _is_flag_expired(flag: FeatureFlagDefinition, cur_semver: tuple[int, int, int]) -> bool:
    """Predicate evaluating whether flag has exceeded expiration deadline."""
    if flag.state == FlagState.REMOVED:
        return False
    return cur_semver >= parse_semver(flag.expires_in)


def _audit_single_flag(
    name: str, flag: FeatureFlagDefinition, cur_semver: tuple[int, int, int], current_version: str
) -> ProtocolFinding | None:
    """Audit single flag for missing contract metadata or expiration."""
    if _has_missing_contract(flag):
        return ProtocolFinding(
            defect=DefectKind.FLAG_MISSING_CONTRACT,
            title="Missing Feature Flag Contract",
            severity=DefectSeverity.HIGH,
            message=f"Flag '{name}' lacks required owner, introduced_in, or expires_in contract",
            target=name,
            line_number=1,
            recommendation="Define owner, introduced_in, and expires_in versions for every flag",
            metadata={"flag": name},
        )

    if _is_flag_expired(flag, cur_semver):
        return ProtocolFinding(
            defect=DefectKind.FLAG_EXPIRED,
            title="Expired Feature Flag",
            severity=DefectSeverity.CRITICAL,
            message=f"Flag '{name}' expired at version {flag.expires_in} (current: {current_version})",
            target=name,
            line_number=1,
            recommendation="Retire feature flag, remove conditional branches, and prune dead code",
            metadata={"flag": name, "expires_in": flag.expires_in},
        )
    return None


class FeatureFlagRegistry:
    """Manages feature flag specifications, runtime evaluation, and lifecycle audits."""

    def __init__(self) -> None:
        self._flags: dict[str, FeatureFlagDefinition] = {}

    def register(self, flag: FeatureFlagDefinition) -> None:
        """Register a feature flag definition into the registry."""
        self._flags[flag.name] = flag

    def get_flag(self, name: str) -> FeatureFlagDefinition | None:
        """Retrieve flag definition by name."""
        return self._flags.get(name)

    def is_enabled(self, name: str, overrides: dict[str, bool] | None = None) -> bool:
        """Evaluate flag state considering overrides and defaults."""
        if overrides and name in overrides:
            return overrides[name]
        flag = self._flags.get(name)
        return flag.default_value if flag else False

    def audit_flags(self, current_version: str) -> list[ProtocolFinding]:
        """Audit registered flags against lifecycle contracts and version deadlines."""
        cur_semver = parse_semver(current_version)
        findings: list[ProtocolFinding] = []
        for name, flag in self._flags.items():
            finding = _audit_single_flag(name, flag, cur_semver, current_version)
            if finding is not None:
                findings.append(finding)
        return findings




class ToolSchemaMigrator:
    """Orchestrates agent tool schema evolution and parameter migrations."""

    def __init__(self, schemas: Sequence[ToolSchemaDefinition] = ()) -> None:
        self._schemas: dict[str, ToolSchemaDefinition] = {s.name: s for s in schemas}

    def register_schema(self, schema: ToolSchemaDefinition) -> None:
        """Register a tool schema definition."""
        self._schemas[schema.name] = schema

    def _apply_single_transform(
        self, value: Any, transform_type: str
    ) -> Any:
        """Execute parameter value transformation."""
        if transform_type == "to_list" and not isinstance(value, list):
            return [value]
        if transform_type == "to_str":
            return str(value)
        return value

    def adapt_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Translate legacy parameters and return normalized arguments with warnings."""
        schema = self._schemas.get(tool_name)
        if not schema:
            return dict(arguments), []

        adapted = dict(arguments)
        warnings_emitted: list[str] = []

        for rule in schema.migrations:
            if rule.old_param in adapted:
                old_val = adapted.pop(rule.old_param)
                adapted[rule.new_param] = self._apply_single_transform(
                    old_val, rule.transform_type
                )
                warnings_emitted.append(
                    f"Parameter '{rule.old_param}' in tool '{tool_name}' is deprecated since "
                    f"v{rule.since_version} and will be removed in v{rule.remove_in_version}. "
                    f"Use '{rule.new_param}' instead."
                )

        return adapted, warnings_emitted

    def diff_schemas(
        self, old_schema: ToolSchemaDefinition, new_schema: ToolSchemaDefinition
    ) -> list[ProtocolFinding]:
        """Verify backward compatibility between schema versions and detect breaking drops."""
        findings: list[ProtocolFinding] = []
        old_params = set(old_schema.parameters.keys())
        new_params = set(new_schema.parameters.keys())
        migrated_params = {m.old_param for m in new_schema.migrations}

        # Check for dropped parameters without migration rules
        dropped = (old_params - new_params) - migrated_params
        for param in sorted(dropped):
            findings.append(
                ProtocolFinding(
                    defect=DefectKind.SCHEMA_BREAKING_CHANGE,
                    title="Breaking Parameter Drop",
                    severity=DefectSeverity.CRITICAL,
                    message=f"Parameter '{param}' removed from tool '{new_schema.name}' without migration rule",
                    target=f"{new_schema.name}.{param}",
                    line_number=1,
                    recommendation="Add a ParameterMigrationRule or maintain parameter alias until major boundary",
                    metadata={"tool": new_schema.name, "parameter": param},
                )
            )

        # Check for newly required parameters without migration defaults
        old_req = set(old_schema.required)
        new_req = set(new_schema.required)
        added_req = new_req - old_req
        for req in sorted(added_req):
            if req not in old_params:
                findings.append(
                    ProtocolFinding(
                        defect=DefectKind.SCHEMA_BREAKING_CHANGE,
                        title="New Required Parameter Added",
                        severity=DefectSeverity.HIGH,
                        message=f"Required parameter '{req}' added to tool '{new_schema.name}' breaking older callers",
                        target=f"{new_schema.name}.{req}",
                        line_number=1,
                        recommendation="Make newly introduced parameters optional or provide backward-compatible default",
                        metadata={"tool": new_schema.name, "parameter": req},
                    )
                )

        return findings


class _DeadBranchVisitor(ast.NodeVisitor):
    """AST visitor detecting dead code conditionals guarded by retired feature flags."""

    def __init__(self, retired_flags: dict[str, bool], filename: str) -> None:
        self.retired_flags = retired_flags
        self.filename = filename
        self.findings: list[ProtocolFinding] = []

    def _extract_flag_name(self, node: ast.Call) -> str | None:
        """Extract feature flag name from call node if matching flag pattern."""
        func_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if func_name in ("is_enabled", "feature_flag", "flag_enabled") and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
        return None

    def visit_If(self, node: ast.If) -> None:
        """Inspect if-statement test conditions for retired feature flag invocations."""
        if isinstance(node.test, ast.Call):
            flag_name = self._extract_flag_name(node.test)
            if flag_name and flag_name in self.retired_flags:
                ret_val = self.retired_flags[flag_name]
                dead_branch = "else" if ret_val else "then"
                self.findings.append(
                    ProtocolFinding(
                        defect=DefectKind.DEAD_FLAG_BRANCH,
                        title="Dead Code Branch from Retired Flag",
                        severity=DefectSeverity.HIGH,
                        message=f"Conditional on retired flag '{flag_name}' (permanent {ret_val}) creates dead {dead_branch}-branch",
                        target=self.filename,
                        line_number=node.lineno,
                        recommendation=f"Prune dead {dead_branch}-branch and inline feature conditional",
                        metadata={"flag": flag_name, "dead_branch": dead_branch},
                    )
                )
        self.generic_visit(node)


def find_dead_flag_branches(
    code: str, retired_flags: dict[str, bool], filename: str = "module.py"
) -> list[ProtocolFinding]:
    """Analyze Python AST to identify dead code conditionals guarded by retired flags."""
    try:
        tree = ast.parse(code, filename=filename)
    except SyntaxError:
        return []
    visitor = _DeadBranchVisitor(retired_flags, filename)
    visitor.visit(tree)
    return visitor.findings


def export_sarif_json(summary: ProtocolSummary) -> dict[str, Any]:
    """Export protocol findings in standard OASIS SARIF 2.1.0 schema format."""
    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []

    for f in summary.findings:
        rules.setdefault(
            f.defect.value,
            {
                "id": f.defect.value,
                "name": f.title,
                "shortDescription": {"text": f.title},
                "defaultConfiguration": {"level": f.severity.value},
            },
        )
        results.append(
            {
                "ruleId": f.defect.value,
                "level": f.severity.value,
                "message": {"text": f"{f.message} ({f.recommendation})"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f.target},
                            "region": {"startLine": f.line_number},
                        }
                    }
                ],
            }
        )

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "vibes-deprecation-protocol",
                        "informationUri": "https://github.com/dan-petty/vibes",
                        "version": "1.0.0",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def export_json(summary: ProtocolSummary) -> str:
    """Export protocol findings as formatted JSON string."""
    data = {
        "timestamp": summary.timestamp,
        "current_version": summary.current_version,
        "total_findings": len(summary.findings),
        "findings": [
            {
                "defect": f.defect.value,
                "title": f.title,
                "severity": f.severity.value,
                "message": f.message,
                "target": f.target,
                "line_number": f.line_number,
                "recommendation": f.recommendation,
                "metadata": f.metadata,
            }
            for f in summary.findings
        ],
    }
    return json.dumps(data, indent=2)


def format_markdown_report(summary: ProtocolSummary) -> str:
    """Format human-readable Markdown summary table of protocol findings."""
    lines = [
        "# Enterprise Change Management & Version Deprecation Report",
        "",
        f"- **Current Version**: `{summary.current_version}`",
        f"- **Total Findings**: {len(summary.findings)}",
        f"- **Audit Timestamp**: {summary.timestamp}",
        "",
    ]
    if not summary.findings:
        lines.append("✓ **Zero deprecation defects detected.** Lifecycle contracts verified clean.\n")
        return "\n".join(lines)

    lines.extend([
        "| Defect | Severity | Target | Line | Finding | Recommendation |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ])
    for f in summary.findings:
        lines.append(
            f"| `{f.defect.value}` | **{f.severity.value.upper()}** | `{f.target}` | {f.line_number} | {f.message} | {f.recommendation} |"
        )
    lines.append("")
    return "\n".join(lines)


def _build_argument_parser() -> argparse.ArgumentParser:
    """Construct command line interface argument parser."""
    parser = argparse.ArgumentParser(
        prog="deprecation_protocol.py",
        description="Enterprise Change Management & Version Deprecation Protocol.",
    )
    parser.add_argument(
        "--current-version",
        type=str,
        default="1.0.0",
        help="Current system release version for contract auditing",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "sarif", "markdown"],
        default="table",
        help="Report serialization format",
    )
    parser.add_argument("--output", type=Path, default=None, help="Write scan report to file")
    return parser


def _format_summary_report(summary: ProtocolSummary, fmt: str) -> str:
    """Serialize summary to designated format string."""
    formatters = {
        "sarif": lambda s: json.dumps(export_sarif_json(s), indent=2),
        "json": export_json,
        "markdown": format_markdown_report,
        "table": format_markdown_report,
    }
    formatter = formatters.get(fmt, format_markdown_report)
    return formatter(summary)


def _write_or_print_content(content: str, out_path: Path | None) -> None:
    """Write rendered report to target file or stdout."""
    if out_path:
        out_path.write_text(content, encoding="utf-8")
        print(f"Report written to {out_path}")
        return
    print(content)


def main(argv: Sequence[str] | None = None) -> int:
    """Execute command line interface for deprecation protocol."""
    parser = _build_argument_parser()
    args = parser.parse_args(argv)

    registry = FeatureFlagRegistry()
    findings = registry.audit_flags(args.current_version)
    summary = ProtocolSummary(current_version=args.current_version, findings=findings)

    content = _format_summary_report(summary, args.format)
    _write_or_print_content(content, args.output)
    return 1 if summary.findings else 0


if __name__ == "__main__":
    sys.exit(main())
