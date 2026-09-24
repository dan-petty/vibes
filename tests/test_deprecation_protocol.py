"""Unit test suite for Enterprise Change Management & Version Deprecation Protocol.

Verifies feature flag contracts, version expiration audits, tool schema parameter
migration with deprecation warning emission, breaking change schema diffing,
and dead-code conditional branch detection.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from deprecation_protocol import (
    DefectKind,
    DefectSeverity,
    FeatureFlagDefinition,
    FeatureFlagRegistry,
    FlagState,
    ParameterMigrationRule,
    ProtocolFinding,
    ProtocolSummary,
    ToolSchemaDefinition,
    ToolSchemaMigrator,
    export_json,
    export_sarif_json,
    find_dead_flag_branches,
    format_markdown_report,
    main,
    parse_semver,
)


def test_parse_semver_valid_and_invalid() -> None:
    """Verify semantic version string parsing and error handling."""
    valid_parsed = parse_semver("1.2.3")
    assert valid_parsed == (1, 2, 3)

    with pytest.raises(ValueError):
        parse_semver("invalid-semver")


def test_feature_flag_registration_and_evaluation() -> None:
    """Verify feature flag registration, default values, and context overrides."""
    registry = FeatureFlagRegistry()
    flag = FeatureFlagDefinition(
        name="ai_streaming",
        state=FlagState.EXPERIMENTAL,
        introduced_in="1.0.0",
        expires_in="2.0.0",
        owner="ml-ops",
        default_value=False,
        description="Enable streaming reasoning tokens",
    )
    registry.register(flag)

    ret_default = registry.is_enabled("ai_streaming")
    ret_override = registry.is_enabled("ai_streaming", overrides={"ai_streaming": True})
    ret_missing = registry.is_enabled("unknown_flag")

    assert (ret_default, ret_override, ret_missing) == (False, True, False)


def test_feature_flag_audit_missing_contract() -> None:
    """Detect feature flags lacking required contract metadata."""
    registry = FeatureFlagRegistry()
    broken_flag = FeatureFlagDefinition(
        name="legacy_cache",
        state=FlagState.DEPRECATED,
        introduced_in="",
        expires_in="",
        owner="",
        default_value=True,
        description="Missing contract",
    )
    registry.register(broken_flag)

    findings = registry.audit_flags("1.0.0")
    assert len(findings) == 1
    assert (findings[0].defect, findings[0].severity) == (
        DefectKind.FLAG_MISSING_CONTRACT,
        DefectSeverity.HIGH,
    )


def test_feature_flag_audit_expired_flag() -> None:
    """Detect feature flags that have exceeded their expiration version."""
    registry = FeatureFlagRegistry()
    expired_flag = FeatureFlagDefinition(
        name="v1_auth",
        state=FlagState.DEPRECATED,
        introduced_in="1.0.0",
        expires_in="1.5.0",
        owner="security",
        default_value=True,
        description="Old auth protocol",
    )
    registry.register(expired_flag)

    findings = registry.audit_flags("1.5.0")
    assert len(findings) == 1
    assert (findings[0].defect, findings[0].severity, findings[0].metadata.get("expires_in")) == (
        DefectKind.FLAG_EXPIRED,
        DefectSeverity.CRITICAL,
        "1.5.0",
    )


def test_feature_flag_audit_removed_flag_not_expired() -> None:
    """Verify flags marked as REMOVED are considered clean and not reported as expired."""
    registry = FeatureFlagRegistry()
    removed_flag = FeatureFlagDefinition(
        name="v1_auth",
        state=FlagState.REMOVED,
        introduced_in="1.0.0",
        expires_in="1.5.0",
        owner="security",
        default_value=False,
        description="Removed auth",
    )
    registry.register(removed_flag)

    findings = registry.audit_flags("1.6.0")
    assert len(findings) == 0


def test_tool_schema_parameter_migration_and_warning() -> None:
    """Verify tool argument migration from deprecated to modern parameter names."""
    rule = ParameterMigrationRule(
        tool_name="search_code",
        old_param="query_text",
        new_param="query",
        since_version="1.1.0",
        remove_in_version="2.0.0",
        transform_type="identity",
    )
    schema = ToolSchemaDefinition(
        name="search_code",
        version="1.1.0",
        parameters={"query": {"type": "string"}},
        required=("query",),
        migrations=(rule,),
    )
    migrator = ToolSchemaMigrator([schema])

    legacy_call = {"query_text": "ast sentinel", "limit": 10}
    adapted, warnings = migrator.adapt_arguments("search_code", legacy_call)

    assert (
        adapted.get("query"),
        "query_text" in adapted,
        len(warnings),
        "deprecated since v1.1.0" in warnings[0],
    ) == (
        "ast sentinel",
        False,
        1,
        True,
    )


def test_tool_schema_transform_to_list() -> None:
    """Verify parameter migration value transformation to list."""
    rule = ParameterMigrationRule(
        tool_name="run_tests",
        old_param="test_target",
        new_param="test_targets",
        since_version="1.2.0",
        remove_in_version="2.0.0",
        transform_type="to_list",
    )
    schema = ToolSchemaDefinition(
        name="run_tests",
        version="1.2.0",
        parameters={"test_targets": {"type": "array"}},
        required=("test_targets",),
        migrations=(rule,),
    )
    migrator = ToolSchemaMigrator([schema])

    adapted, _ = migrator.adapt_arguments("run_tests", {"test_target": "test_unit.py"})
    assert adapted.get("test_targets") == ["test_unit.py"]


def test_schema_diff_clean_evolution() -> None:
    """Verify adding optional parameters passes backward compatibility diff."""
    migrator = ToolSchemaMigrator()
    v1 = ToolSchemaDefinition(
        name="fetch",
        version="1.0.0",
        parameters={"url": {"type": "string"}},
        required=("url",),
    )
    v2 = ToolSchemaDefinition(
        name="fetch",
        version="1.1.0",
        parameters={"url": {"type": "string"}, "timeout": {"type": "number"}},
        required=("url",),
    )
    findings = migrator.diff_schemas(v1, v2)
    assert len(findings) == 0


def test_schema_diff_breaking_parameter_drop() -> None:
    """Detect dropping a tool parameter without migration rule."""
    migrator = ToolSchemaMigrator()
    v1 = ToolSchemaDefinition(
        name="fetch",
        version="1.0.0",
        parameters={"url": {"type": "string"}, "headers": {"type": "object"}},
        required=("url",),
    )
    v2 = ToolSchemaDefinition(
        name="fetch",
        version="2.0.0",
        parameters={"url": {"type": "string"}},
        required=("url",),
    )
    findings = migrator.diff_schemas(v1, v2)
    assert len(findings) == 1
    assert (findings[0].defect, findings[0].severity) == (
        DefectKind.SCHEMA_BREAKING_CHANGE,
        DefectSeverity.CRITICAL,
    )


def test_schema_diff_new_required_parameter() -> None:
    """Detect adding a required parameter without backward compatibility."""
    migrator = ToolSchemaMigrator()
    v1 = ToolSchemaDefinition(
        name="fetch",
        version="1.0.0",
        parameters={"url": {"type": "string"}},
        required=("url",),
    )
    v2 = ToolSchemaDefinition(
        name="fetch",
        version="1.1.0",
        parameters={"url": {"type": "string"}, "auth_token": {"type": "string"}},
        required=("url", "auth_token"),
    )
    findings = migrator.diff_schemas(v1, v2)
    assert len(findings) == 1
    assert (findings[0].defect, findings[0].severity) == (
        DefectKind.SCHEMA_BREAKING_CHANGE,
        DefectSeverity.HIGH,
    )


def test_ast_dead_flag_branch_detection() -> None:
    """Detect dead code conditionals guarded by retired feature flags in Python AST."""
    code = """
if is_enabled("legacy_mode"):
    run_legacy()
else:
    run_modern()
"""
    retired = {"legacy_mode": False}
    findings = find_dead_flag_branches(code, retired, "service.py")
    assert len(findings) == 1
    assert (
        findings[0].defect,
        findings[0].severity,
        findings[0].metadata.get("dead_branch"),
    ) == (
        DefectKind.DEAD_FLAG_BRANCH,
        DefectSeverity.HIGH,
        "then",
    )


def test_sarif_and_json_export() -> None:
    """Verify SARIF 2.1.0 and JSON report serialization."""
    finding = ProtocolFinding(
        defect=DefectKind.FLAG_EXPIRED,
        title="Expired Flag",
        severity=DefectSeverity.CRITICAL,
        message="Flag expired",
        target="feature_x",
        line_number=10,
        recommendation="Prune flag",
    )
    summary = ProtocolSummary(current_version="1.2.0", findings=[finding])
    sarif = export_sarif_json(summary)
    json_str = export_json(summary)
    parsed = json.loads(json_str)
    markdown = format_markdown_report(summary)

    assert (
        sarif.get("version"),
        sarif.get("runs", [{}])[0].get("tool", {}).get("driver", {}).get("name"),
        parsed.get("total_findings"),
        "FLAG_EXPIRED" in markdown,
    ) == (
        "2.1.0",
        "vibes-deprecation-protocol",
        1,
        True,
    )


def test_main_cli_execution(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI main entry point execution."""
    ret = main(["--current-version", "1.0.0", "--format", "json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert (ret, parsed.get("current_version")) == (0, "1.0.0")
