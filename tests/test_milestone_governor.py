"""Automated unit tests for the Autonomous Milestone Scope Governor & Release Air-Lock Oracle."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from milestone_governor import (
    GovernorFinding,
    MilestoneIssue,
    MilestoneMetrics,
    MilestonePhase,
    audit_milestone,
    calculate_milestone_metrics,
    export_sarif,
    format_cli_report,
    main,
    parse_iso_datetime,
    partition_milestone_rollover,
)


def test_milestone_issue_from_dict_and_properties() -> None:
    """Verify MilestoneIssue instantiation, parsing, and predicate properties."""
    dict_issue = MilestoneIssue.from_dict({
        "number": 101,
        "title": "Fix critical memory leak in worker daemon",
        "state": "open",
        "labels": [{"name": "priority/p0-critical"}, {"name": "type/bug"}],
        "created_at": "2026-09-24T10:00:00Z",
        "milestone": {"title": "v0.2.23"},
    })

    string_issue = MilestoneIssue.from_dict({
        "number": 102,
        "title": "Implement shiny new dashboard widget",
        "state": "closed",
        "labels": ["type/feature", "priority/p2-low"],
        "created_at": "2026-09-20T08:00:00Z",
        "closed_at": "2026-09-25T12:00:00Z",
        "milestone": "v0.2.23",
    })

    actual_evaluations = (
        (dict_issue.number, dict_issue.is_open, dict_issue.is_p0_blocker, dict_issue.is_feature, dict_issue.milestone),
        (string_issue.number, string_issue.is_open, string_issue.is_p0_blocker, string_issue.is_feature, string_issue.milestone),
    )
    expected_evaluations = (
        (101, True, True, False, "v0.2.23"),
        (102, False, False, True, "v0.2.23"),
    )
    assert actual_evaluations == expected_evaluations


def test_parse_iso_datetime() -> None:
    """Verify ISO 8601 parsing handles Zulu, timezone offsets, and malformed strings."""
    dt_zulu = parse_iso_datetime("2026-09-24T10:00:00Z")
    dt_offset = parse_iso_datetime("2026-09-24T12:00:00+02:00")
    dt_empty = parse_iso_datetime("")
    dt_invalid = parse_iso_datetime("invalid-timestamp")

    actual_parsed = (
        dt_zulu == datetime(2026, 9, 24, 10, 0, 0, tzinfo=UTC),
        dt_offset == datetime(2026, 9, 24, 10, 0, 0, tzinfo=UTC),
        dt_empty is None,
        dt_invalid is None,
    )
    assert actual_parsed == (True, True, True, True)


def test_calculate_milestone_metrics_empty() -> None:
    """Verify metric calculations for an empty milestone."""
    metrics = calculate_milestone_metrics([], title="Empty Milestone")
    actual_tuple = (
        metrics.total_issues,
        metrics.open_issues,
        metrics.closed_issues,
        metrics.completion_rate,
        metrics.convergence_ratio,
        metrics.is_live_locked,
    )
    expected_tuple = (0, 0, 0, 1.0, 1.0, False)
    assert actual_tuple == expected_tuple


def test_calculate_milestone_metrics_converging_and_livelocked() -> None:
    """Verify velocity and convergence ratio calculations in converging vs live-locked states."""
    ref_time = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)

    # 4 issues closed in window (created prior), 1 created in window -> converging
    converging_issues = [
        MilestoneIssue(1, "Bug 1", "closed", created_at="2026-09-10T00:00:00Z", closed_at="2026-09-27T00:00:00Z"),
        MilestoneIssue(2, "Bug 2", "closed", created_at="2026-09-10T00:00:00Z", closed_at="2026-09-27T00:00:00Z"),
        MilestoneIssue(3, "Bug 3", "closed", created_at="2026-09-10T00:00:00Z", closed_at="2026-09-27T00:00:00Z"),
        MilestoneIssue(4, "Bug 4", "closed", created_at="2026-09-10T00:00:00Z", closed_at="2026-09-27T00:00:00Z"),
        MilestoneIssue(5, "Bug 5", "open", created_at="2026-09-26T00:00:00Z"),
    ]
    conv_metrics = calculate_milestone_metrics(converging_issues, as_of=ref_time, window_days=7.0)

    # 4 created in window, 1 closed in window -> live-locked
    livelocked_issues = [
        MilestoneIssue(10, "Bug A", "closed", created_at="2026-09-20T00:00:00Z", closed_at="2026-09-27T00:00:00Z"),
        MilestoneIssue(11, "Feature B", "open", created_at="2026-09-25T00:00:00Z"),
        MilestoneIssue(12, "Feature C", "open", created_at="2026-09-26T00:00:00Z"),
        MilestoneIssue(13, "Feature D", "open", created_at="2026-09-27T00:00:00Z"),
        MilestoneIssue(14, "Feature E", "open", created_at="2026-09-28T00:00:00Z"),
    ]
    lock_metrics = calculate_milestone_metrics(livelocked_issues, as_of=ref_time, window_days=7.0)

    actual_eval = (
        conv_metrics.is_live_locked,
        conv_metrics.convergence_ratio > 2.0,
        conv_metrics.estimated_days_to_convergence is not None,
        lock_metrics.is_live_locked,
        lock_metrics.convergence_ratio < 1.0,
        lock_metrics.estimated_days_to_convergence is None,
    )
    assert actual_eval == (False, True, True, True, True, True)


def _make_dummy_issues(count: int, state: str = "closed") -> list[MilestoneIssue]:
    """Helper to generate dummy issues for sizing tests."""
    return [MilestoneIssue(i, f"Issue {i}", state) for i in range(count)]


def test_audit_milestone_sizing_warning() -> None:
    """Verify MLS003 sizing warning when exceeding default threshold."""
    issues = _make_dummy_issues(55)
    findings = audit_milestone(issues, max_issues=50)
    sizing_levels = [f.level for f in findings if f.rule_id == "MLS003"]
    assert sizing_levels == ["warning"]


def test_audit_milestone_sizing_critical_ceiling() -> None:
    """Verify MLS003 sizing error when exceeding critical mega-milestone ceiling."""
    issues = _make_dummy_issues(105)
    findings = audit_milestone(issues, max_issues=50)
    sizing_levels = [f.level for f in findings if f.rule_id == "MLS003"]
    assert sizing_levels == ["error"]


def test_audit_milestone_stagnant_horizon_rule() -> None:
    """Verify MLS004 triggers when completion rate is high but stubborn open items remain."""
    issues = _make_dummy_issues(80, "closed") + _make_dummy_issues(20, "open")
    findings = audit_milestone(issues, phase=MilestonePhase.INTAKE)
    rule_ids = {f.rule_id for f in findings}
    assert "MLS004" in rule_ids


def test_audit_milestone_airlock_intake_and_active() -> None:
    """Verify MLS002 rule during INTAKE and AIR_LOCKED phases."""
    blocker = MilestoneIssue(1, "Critical crash", "open", labels=("priority/p0-critical",))
    feature = MilestoneIssue(2, "Non-essential UI polish", "open", labels=("type/feature",))
    closed_item = MilestoneIssue(3, "Old item", "closed")
    issues = [blocker, feature, closed_item]

    findings_intake = audit_milestone(issues, phase=MilestonePhase.INTAKE)
    airlock_intake = [f.issue_number for f in findings_intake if f.rule_id == "MLS002"]

    findings_airlocked = audit_milestone(issues, phase=MilestonePhase.AIR_LOCKED)
    airlock_airlocked = [f.issue_number for f in findings_airlocked if f.rule_id == "MLS002"]

    assert (airlock_intake, airlock_airlocked) == ([], [2])


def test_audit_milestone_airlock_frozen_phase() -> None:
    """Verify MLS002 rule during FROZEN phase where no open issues are permitted."""
    blocker = MilestoneIssue(1, "Critical crash", "open", labels=("priority/p0-critical",))
    feature = MilestoneIssue(2, "Non-essential UI polish", "open", labels=("type/feature",))
    issues = [blocker, feature]

    findings_frozen = audit_milestone(issues, phase=MilestonePhase.FROZEN)
    airlock_frozen = [f.issue_number for f in findings_frozen if f.rule_id == "MLS002"]

    assert airlock_frozen == [1, 2]


def test_partition_milestone_rollover() -> None:
    """Verify partitioning separates retained blockers and epics from rollover candidates."""
    p0_issue = MilestoneIssue(1, "Fix memory leak", "open", labels=("p0",))
    epic_issue = MilestoneIssue(2, "Architecture overhaul epic", "open", labels=("type/epic",))
    normal_issue = MilestoneIssue(3, "Enhance CLI help wording", "open", labels=("type/docs",))
    closed_issue = MilestoneIssue(4, "Existing merged PR", "closed")

    keep, rollover = partition_milestone_rollover([p0_issue, epic_issue, normal_issue, closed_issue], target_milestone="v0.2.24")

    keep_numbers = [i.number for i in keep]
    rollover_numbers = [i.number for i in rollover]

    assert (keep_numbers, rollover_numbers) == ([1, 2, 4], [3])


def test_export_sarif() -> None:
    """Verify schema-compliant OASIS SARIF 2.1.0 output structure."""
    findings = [
        GovernorFinding(rule_id="MLS001", level="error", message="Live-lock detected", recommendation="Apply air-lock"),
        GovernorFinding(rule_id="MLS002", level="error", message="Air-lock breach", issue_number=42, recommendation="Rollover"),
    ]
    sarif_data = export_sarif(findings, milestone_title="v0.2.23")

    runs = sarif_data.get("runs", [])
    assert len(runs) == 1
    run = runs[0]
    rules = run.get("tool", {}).get("driver", {}).get("rules", [])
    results = run.get("results", [])

    assert (sarif_data["version"], len(rules), len(results), results[1]["properties"]["issueNumber"]) == (
        "2.1.0",
        2,
        2,
        42,
    )


def test_format_cli_report() -> None:
    """Verify ASCII report format contains title, progress bar, findings, and rollover items."""
    metrics = MilestoneMetrics(
        milestone_title="v0.2.23",
        total_issues=10,
        open_issues=2,
        closed_issues=8,
        completion_rate=0.8,
        scope_velocity=1.0,
        burn_velocity=2.0,
        convergence_ratio=2.0,
        estimated_days_to_convergence=2.0,
        is_live_locked=False,
    )
    findings = [GovernorFinding(rule_id="MLS004", level="warning", message="Stagnant band", recommendation="Rollover")]
    rollover = [MilestoneIssue(99, "Rollover candidate", "open")]

    report = format_cli_report(metrics, MilestonePhase.AIR_LOCKED, findings, rollover)
    assert ("MILESTONE SCOPE GOVERNOR — V0.2.23" in report, "Progress: [" in report, "MLS004" in report, "#99" in report) == (
        True,
        True,
        True,
        True,
    )


def test_main_cli_execution(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify main CLI execution for exit codes, json, and sarif flags."""
    sample_data = [
        {"number": 1, "title": "P0 Bug", "state": "open", "labels": [{"name": "p0"}], "created_at": "2026-09-24T00:00:00Z"},
        {"number": 2, "title": "Regular task", "state": "open", "labels": [], "created_at": "2026-09-24T00:00:00Z"},
    ]
    json_file = tmp_path / "milestone.json"
    sarif_file = tmp_path / "findings.sarif"
    json_file.write_text(json.dumps(sample_data), encoding="utf-8")

    # In INTAKE phase with 2 issues: no errors -> exit code 0
    exit_intake = main([str(json_file), "--phase", "INTAKE"])

    # In AIR_LOCKED phase: issue 2 is non-P0 -> error MLS002 -> exit code 1
    exit_airlocked = main([str(json_file), "--phase", "AIR_LOCKED", "--sarif", str(sarif_file), "--json"])

    # File not found -> exit code 1
    exit_missing = main(["/path/does/not/exist.json"])

    assert (exit_intake, exit_airlocked, exit_missing, sarif_file.is_file()) == (0, 1, 1, True)
