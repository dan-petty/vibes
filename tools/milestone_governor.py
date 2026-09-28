#!/usr/bin/env python3
"""Autonomous Milestone Scope Governor & Release Air-Lock Oracle.

Detects and resolves the Milestone Horizon Inflation Trap and Scope Cascades
in autonomous AI agent workflows. Enforces milestone lifecycle phases:
- INTAKE: Unrestricted scope discovery and feature addition.
- AIR_LOCKED: Stabilization phase where only P0 release-blocking regressions may enter.
- FROZEN: Release candidate freeze where all non-essential items roll over to next milestone.
- CLOSED: Release complete.

Evaluates Scope Injection Velocity (V_scope), Burn-Down Velocity (V_burn),
and the Convergence Ratio (C_R) to prevent autonomous release live-locks.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

# Invariant thresholds
DEFAULT_MAX_MILESTONE_ISSUES: Final[int] = 50
CRITICAL_MEGA_MILESTONE_ISSUES: Final[int] = 100
MIN_CONVERGENCE_RATIO: Final[float] = 1.2
WINDOW_DAYS_DEFAULT: Final[float] = 7.0

SARIF_SCHEMA_URI: Final[str] = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
)


class MilestonePhase(StrEnum):
    """Lifecycle phase of a software release milestone."""

    INTAKE = "INTAKE"
    AIR_LOCKED = "AIR_LOCKED"
    FROZEN = "FROZEN"
    CLOSED = "CLOSED"


def _extract_label_item(item: Any) -> str:
    """Extract label name string from dictionary or string."""
    if isinstance(item, dict):
        return str(item.get("name", ""))
    if isinstance(item, str):
        return item
    return ""


def _extract_labels(raw_labels: Any) -> tuple[str, ...]:
    """Extract string labels from GitHub API representation."""
    if not isinstance(raw_labels, (list, tuple)):
        return ()
    return tuple(_extract_label_item(item) for item in raw_labels)


def _extract_milestone_title(raw_milestone: Any) -> str:
    """Extract milestone title from dict or string representation."""
    if isinstance(raw_milestone, dict):
        return str(raw_milestone.get("title", ""))
    if isinstance(raw_milestone, str):
        return raw_milestone
    return ""


@dataclass(frozen=True)
class MilestoneIssue:
    """Discrete GitHub issue or pull request tracked in a milestone."""

    number: int
    title: str
    state: str  # "open" or "closed"
    labels: tuple[str, ...] = ()
    created_at: str = ""
    closed_at: str | None = None
    milestone: str = ""

    @property
    def is_open(self) -> bool:
        """Return True if issue is open."""
        return self.state.lower() == "open"

    @property
    def is_p0_blocker(self) -> bool:
        """Return True if issue represents an absolute release blocker."""
        labels_lower = {label.lower() for label in self.labels}
        p0_markers = {"priority/p0-critical", "p0", "priority/p0", "blocker", "critical"}
        return bool(labels_lower & p0_markers)

    @property
    def is_feature(self) -> bool:
        """Return True if issue represents new feature scope."""
        labels_lower = {label.lower() for label in self.labels}
        return "type/feature" in labels_lower or "feature" in labels_lower

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MilestoneIssue:
        """Construct MilestoneIssue from GitHub API dictionary."""
        return cls(
            number=int(data.get("number", 0)),
            title=str(data.get("title", "")),
            state=str(data.get("state", "open")),
            labels=_extract_labels(data.get("labels", [])),
            created_at=str(data.get("created_at", "")),
            closed_at=data.get("closed_at"),
            milestone=_extract_milestone_title(data.get("milestone")),
        )


@dataclass(frozen=True)
class MilestoneMetrics:
    """Holistic mathematical convergence and scope velocity metrics."""

    milestone_title: str
    total_issues: int
    open_issues: int
    closed_issues: int
    completion_rate: float
    scope_velocity: float  # issues added per day over evaluation window
    burn_velocity: float  # issues closed per day over evaluation window
    convergence_ratio: float  # burn / max(scope, 0.01)
    estimated_days_to_convergence: float | None
    is_live_locked: bool

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class GovernorFinding:
    """Diagnostic violation or advisory emitted by milestone governor."""

    rule_id: str
    level: str  # "error", "warning", "note"
    message: str
    issue_number: int | None = None
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert finding to dictionary."""
        return asdict(self)


RULE_CATALOG: Final[dict[str, dict[str, str]]] = {
    "MLS001": {
        "name": "ScopeExpansionLiveLock",
        "description": "Milestone scope injection velocity meets or exceeds burn-down velocity, causing release live-lock.",
    },
    "MLS002": {
        "name": "AirLockBoundaryBreach",
        "description": "Non-P0 issue admitted to milestone during AIR_LOCKED or FROZEN lifecycle phases.",
    },
    "MLS003": {
        "name": "MegaMilestoneSizingAlert",
        "description": "Milestone issue count exceeds safe sizing boundaries, indicating uncurated scope sprawl.",
    },
    "MLS004": {
        "name": "StagnantHorizonRatchet",
        "description": "Milestone completion percentage trapped in stagnant band despite continuous issue closures.",
    },
}


def parse_iso_datetime(dt_str: str) -> datetime | None:
    """Parse ISO 8601 string to timezone-aware UTC datetime."""
    if not dt_str:
        return None
    clean = dt_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(clean)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _empty_metrics(title: str) -> MilestoneMetrics:
    """Construct default metrics for empty milestone."""
    return MilestoneMetrics(
        milestone_title=title,
        total_issues=0,
        open_issues=0,
        closed_issues=0,
        completion_rate=1.0,
        scope_velocity=0.0,
        burn_velocity=0.0,
        convergence_ratio=1.0,
        estimated_days_to_convergence=0.0,
        is_live_locked=False,
    )


def _compute_convergence_projection(open_count: int, net_velocity: float) -> tuple[float | None, bool]:
    """Calculate estimated days to convergence and live-lock status."""
    if open_count == 0:
        return 0.0, False
    if net_velocity > 0.05:
        return round(open_count / net_velocity, 1), False
    return None, True


def calculate_milestone_metrics(
    issues: Sequence[MilestoneIssue],
    title: str = "Active Milestone",
    window_days: float = WINDOW_DAYS_DEFAULT,
    as_of: datetime | None = None,
) -> MilestoneMetrics:
    """Compute mathematical velocity and convergence metrics over rolling window."""
    total = len(issues)
    if total == 0:
        return _empty_metrics(title)

    open_count = sum(1 for i in issues if i.is_open)
    closed_count = total - open_count
    completion_rate = closed_count / total

    reference_now = as_of if as_of is not None else datetime.now(UTC)
    scope_vel, burn_vel = _calculate_velocities(issues, reference_now, window_days)

    denom = max(scope_vel, 0.01)
    convergence_ratio = round(burn_vel / denom, 2)
    net_velocity = burn_vel - scope_vel
    days_to_conv, is_live_locked = _compute_convergence_projection(open_count, net_velocity)

    return MilestoneMetrics(
        milestone_title=title,
        total_issues=total,
        open_issues=open_count,
        closed_issues=closed_count,
        completion_rate=round(completion_rate, 4),
        scope_velocity=round(scope_vel, 2),
        burn_velocity=round(burn_vel, 2),
        convergence_ratio=convergence_ratio,
        estimated_days_to_convergence=days_to_conv,
        is_live_locked=is_live_locked,
    )


def _is_within_window(dt_str: str, reference_now: datetime, window_sec: float) -> bool:
    """Return True if ISO datetime string falls within rolling window."""
    if not dt_str:
        return False
    dt = parse_iso_datetime(dt_str)
    return dt is not None and (reference_now - dt).total_seconds() <= window_sec


def _calculate_velocities(
    issues: Sequence[MilestoneIssue], reference_now: datetime, window_days: float
) -> tuple[float, float]:
    """Calculate creation and closure velocity per day inside window."""
    window_sec = max(window_days * 86400.0, 1.0)
    created_in_window = sum(
        1 for issue in issues if _is_within_window(issue.created_at, reference_now, window_sec)
    )
    closed_in_window = sum(
        1
        for issue in issues
        if issue.closed_at and _is_within_window(issue.closed_at, reference_now, window_sec)
    )

    norm_days = max(window_days, 0.1)
    scope_vel = created_in_window / norm_days
    burn_vel = closed_in_window / norm_days
    return scope_vel, burn_vel


def audit_milestone(
    issues: Sequence[MilestoneIssue],
    phase: MilestonePhase = MilestonePhase.INTAKE,
    metrics: MilestoneMetrics | None = None,
    max_issues: int = DEFAULT_MAX_MILESTONE_ISSUES,
) -> list[GovernorFinding]:
    """Audit milestone issues against scope governor invariants."""
    computed_metrics = metrics if metrics is not None else calculate_milestone_metrics(issues)
    findings: list[GovernorFinding] = []

    _check_sizing_rules(computed_metrics, max_issues, findings)
    _check_convergence_rules(computed_metrics, phase, findings)
    _check_airlock_rules(issues, phase, findings)

    return findings


def _check_sizing_rules(
    metrics: MilestoneMetrics, max_issues: int, findings: list[GovernorFinding]
) -> None:
    """Enforce milestone sizing bounds to prevent unmanageable releases."""
    if metrics.total_issues > CRITICAL_MEGA_MILESTONE_ISSUES:
        findings.append(
            GovernorFinding(
                rule_id="MLS003",
                level="error",
                message=(
                    f"Milestone '{metrics.milestone_title}' contains {metrics.total_issues} issues "
                    f"(exceeds critical ceiling of {CRITICAL_MEGA_MILESTONE_ISSUES})."
                ),
                recommendation="Slice milestone immediately into decoupled releases or roll over non-blockers.",
            )
        )
    elif metrics.total_issues > max_issues:
        findings.append(
            GovernorFinding(
                rule_id="MLS003",
                level="warning",
                message=(
                    f"Milestone '{metrics.milestone_title}' contains {metrics.total_issues} issues "
                    f"(exceeds recommended threshold of {max_issues})."
                ),
                recommendation="Audit active issues and apply milestone air-lock to halt secondary scope additions.",
            )
        )


def _check_stagnant_horizon(metrics: MilestoneMetrics, findings: list[GovernorFinding]) -> None:
    """Detect stubborn stagnant completion band despite ongoing closures."""
    is_in_stagnant_band = 0.70 <= metrics.completion_rate <= 0.90
    if is_in_stagnant_band and metrics.open_issues >= 15:
        findings.append(
            GovernorFinding(
                rule_id="MLS004",
                level="warning",
                message=(
                    f"Milestone completion trapped at {metrics.completion_rate * 100:.1f}% with "
                    f"{metrics.open_issues} open items remaining despite {metrics.closed_issues} closed."
                ),
                recommendation="Execute scope rollover: non-essential open items should be rolled over to next release.",
            )
        )


def _check_convergence_rules(
    metrics: MilestoneMetrics, phase: MilestonePhase, findings: list[GovernorFinding]
) -> None:
    """Enforce burn-down velocity over scope expansion."""
    if metrics.open_issues == 0:
        return

    if metrics.is_live_locked:
        level = "error" if phase in (MilestonePhase.AIR_LOCKED, MilestonePhase.FROZEN) else "warning"
        findings.append(
            GovernorFinding(
                rule_id="MLS001",
                level=level,
                message=(
                    f"Milestone '{metrics.milestone_title}' is in scope live-lock: scope velocity "
                    f"({metrics.scope_velocity}/day) >= burn velocity ({metrics.burn_velocity}/day). "
                    f"Convergence ratio: {metrics.convergence_ratio:.2f}."
                ),
                recommendation="Enforce Milestone Scope Air-Lock. Route new defect and feature intake to next milestone.",
            )
        )
    elif metrics.convergence_ratio < MIN_CONVERGENCE_RATIO:
        findings.append(
            GovernorFinding(
                rule_id="MLS001",
                level="warning",
                message=(
                    f"Milestone '{metrics.milestone_title}' convergence ratio ({metrics.convergence_ratio:.2f}) "
                    f"is below healthy threshold ({MIN_CONVERGENCE_RATIO:.1f})."
                ),
                recommendation="Prioritize closing active PRs and restrict non-critical issue creation.",
            )
        )

    _check_stagnant_horizon(metrics, findings)


def _audit_airlocked_issue(
    issue: MilestoneIssue, phase: MilestonePhase, findings: list[GovernorFinding]
) -> None:
    """Audit single open issue for admission conformance in restricted phases."""
    if phase == MilestonePhase.FROZEN:
        findings.append(
            GovernorFinding(
                rule_id="MLS002",
                level="error",
                issue_number=issue.number,
                message=f"Issue #{issue.number} ('{issue.title[:60]}') remains open during FROZEN release phase.",
                recommendation="Roll over issue to next release or close before tagging release.",
            )
        )
    elif phase == MilestonePhase.AIR_LOCKED and not issue.is_p0_blocker:
        findings.append(
            GovernorFinding(
                rule_id="MLS002",
                level="error",
                issue_number=issue.number,
                message=(
                    f"Non-P0 issue #{issue.number} ('{issue.title[:60]}') admitted to "
                    f"milestone during AIR_LOCKED phase."
                ),
                recommendation="Roll over non-blocker issue to next milestone.",
            )
        )


def _check_airlock_rules(
    issues: Sequence[MilestoneIssue],
    phase: MilestonePhase,
    findings: list[GovernorFinding],
) -> None:
    """Verify that issues in air-locked milestone satisfy blocker criteria."""
    if phase not in (MilestonePhase.AIR_LOCKED, MilestonePhase.FROZEN):
        return

    for issue in issues:
        if issue.is_open:
            _audit_airlocked_issue(issue, phase, findings)


def partition_milestone_rollover(
    issues: Sequence[MilestoneIssue],
    target_milestone: str,
) -> tuple[list[MilestoneIssue], list[MilestoneIssue]]:
    """Partition open issues into retained blockers vs rollover candidates.

    Returns:
        tuple(keep_list, rollover_list)
    """
    keep: list[MilestoneIssue] = []
    rollover: list[MilestoneIssue] = []

    for issue in issues:
        if not issue.is_open:
            keep.append(issue)
            continue

        is_retained = issue.is_p0_blocker or ("type/epic" in issue.labels)
        if is_retained:
            keep.append(issue)
        else:
            rollover.append(issue)

    return keep, rollover


def _format_header_lines(metrics: MilestoneMetrics, phase: MilestonePhase) -> list[str]:
    """Format metadata and velocity header lines."""
    lines: list[str] = [
        "=" * 78,
        f"🎯 MILESTONE SCOPE GOVERNOR — {metrics.milestone_title.upper()}",
        "=" * 78,
        f"Phase:           {phase.value}",
        f"Total Issues:    {metrics.total_issues} ({metrics.closed_issues} closed, {metrics.open_issues} open)",
        f"Completion:      {metrics.completion_rate * 100:.1f}%",
        f"Scope Velocity:  {metrics.scope_velocity:.2f} issues/day (intake)",
        f"Burn Velocity:   {metrics.burn_velocity:.2f} issues/day (closures)",
        f"Convergence:     {metrics.convergence_ratio:.2f}x ({'LIVE-LOCKED' if metrics.is_live_locked else 'CONVERGING'})",
    ]

    if metrics.estimated_days_to_convergence is not None:
        if metrics.estimated_days_to_convergence > 0:
            lines.append(f"Est. Completion: {metrics.estimated_days_to_convergence:.1f} days at current net velocity")
        else:
            lines.append("Est. Completion: 0.0 days (Complete)")
    else:
        lines.append("Est. Completion: INDEFINITE (Scope intake exceeds burn rate)")

    lines.append("-" * 78)
    bar_width = 40
    filled = round(metrics.completion_rate * bar_width)
    bar = "█" * filled + "░" * (bar_width - filled)
    lines.append(f"Progress: [{bar}] {metrics.completion_rate * 100:.1f}%\n")
    return lines


def _format_finding_line(f: GovernorFinding) -> list[str]:
    """Format single governor finding with action recommendation."""
    prefix = "✗ [ERROR]" if f.level == "error" else "! [WARN]"
    issue_str = f" #{f.issue_number}" if f.issue_number else ""
    lines = [f"  {prefix} {f.rule_id}{issue_str}: {f.message}"]
    if f.recommendation:
        lines.append(f"    ↳ Action: {f.recommendation}")
    return lines


def _format_findings_section(findings: Sequence[GovernorFinding]) -> list[str]:
    """Format section detailing all governor findings."""
    lines = [f"Findings ({len(findings)}):"]
    if not findings:
        lines.append("  ✓ Clean: Milestone scope satisfies all architectural invariants.")
        return lines
    for f in findings:
        lines.extend(_format_finding_line(f))
    return lines


def _format_rollover_section(rollover_candidates: Sequence[MilestoneIssue]) -> list[str]:
    """Format section listing candidate issues for milestone rollover."""
    if not rollover_candidates:
        return []
    lines = ["\n" + "-" * 78, f"Recommended Rollover Candidates ({len(rollover_candidates)}):"]
    for cand in rollover_candidates[:15]:
        lines.append(f"  → #{cand.number}: {cand.title[:65]}")
    if len(rollover_candidates) > 15:
        lines.append(f"  ... and {len(rollover_candidates) - 15} more.")
    return lines


def format_cli_report(
    metrics: MilestoneMetrics,
    phase: MilestonePhase,
    findings: Sequence[GovernorFinding],
    rollover_candidates: Sequence[MilestoneIssue] | None = None,
) -> str:
    """Format human-readable CLI report with ASCII burndown status."""
    lines = _format_header_lines(metrics, phase)
    lines.extend(_format_findings_section(findings))
    if rollover_candidates:
        lines.extend(_format_rollover_section(rollover_candidates))
    lines.append("=" * 78)
    return "\n".join(lines)


def _build_rule_metadata(rule_id: str) -> dict[str, Any]:
    """Construct SARIF rule descriptor for given rule identifier."""
    meta = RULE_CATALOG.get(rule_id, {"name": rule_id, "description": rule_id})
    return {
        "id": rule_id,
        "name": meta["name"],
        "shortDescription": {"text": meta["description"]},
    }


def _build_sarif_result(f: GovernorFinding, milestone_title: str) -> dict[str, Any]:
    """Construct SARIF result entry for a single finding."""
    level_map = {"error": "error", "warning": "warning", "note": "note"}
    result_obj: dict[str, Any] = {
        "ruleId": f.rule_id,
        "level": level_map.get(f.level, "warning"),
        "message": {"text": f.message},
        "properties": {
            "milestone": milestone_title,
            "recommendation": f.recommendation,
        },
    }
    if f.issue_number:
        result_obj["properties"]["issueNumber"] = f.issue_number
    return result_obj


def export_sarif(
    findings: Sequence[GovernorFinding],
    milestone_title: str,
) -> dict[str, Any]:
    """Export governor findings to schema-compliant OASIS SARIF 2.1.0."""
    rules_used: dict[str, dict[str, Any]] = {}
    sarif_results: list[dict[str, Any]] = []

    for f in findings:
        if f.rule_id not in rules_used:
            rules_used[f.rule_id] = _build_rule_metadata(f.rule_id)
        sarif_results.append(_build_sarif_result(f, milestone_title))

    return {
        "$schema": SARIF_SCHEMA_URI,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "vibes-milestone-governor",
                        "semanticVersion": "1.0.0",
                        "rules": list(rules_used.values()),
                    }
                },
                "results": sarif_results,
            }
        ],
    }


def _extract_dict_items(raw: Any) -> list[dict[str, Any]]:
    """Filter raw structure to only dictionary items."""
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _extract_raw_issue_list(data: Any) -> list[dict[str, Any]]:
    """Extract raw issue dictionaries from parsed JSON data structure."""
    if isinstance(data, list):
        return _extract_dict_items(data)
    if isinstance(data, dict):
        items = data.get("issues") or data.get("resources")
        return _extract_dict_items(items)
    return []


def load_issues_from_json(path: Path) -> list[MilestoneIssue]:
    """Load list of MilestoneIssue from JSON file."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    raw_list = _extract_raw_issue_list(data)
    return [MilestoneIssue.from_dict(item) for item in raw_list]


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Autonomous Milestone Scope Governor & Release Air-Lock Oracle",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("file", type=Path, help="Path to JSON file containing milestone issues")
    parser.add_argument(
        "--phase",
        type=str,
        choices=[p.value for p in MilestonePhase],
        default=MilestonePhase.INTAKE.value,
        help="Target milestone lifecycle phase",
    )
    parser.add_argument("--title", type=str, default="Release Milestone", help="Milestone title")
    parser.add_argument("--max-issues", type=int, default=DEFAULT_MAX_MILESTONE_ISSUES, help="Max issue ceiling")
    parser.add_argument("--window-days", type=float, default=WINDOW_DAYS_DEFAULT, help="Rolling evaluation window")
    parser.add_argument("--rollover-to", type=str, default="vNext", help="Target milestone for rollover plan")
    parser.add_argument("--sarif", type=Path, help="Path to export OASIS SARIF 2.1.0 output")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    return parser


def _export_outputs(
    args: argparse.Namespace,
    metrics: MilestoneMetrics,
    phase: MilestonePhase,
    findings: Sequence[GovernorFinding],
    rollover: Sequence[MilestoneIssue],
) -> None:
    """Handle CLI terminal or file output formatting."""
    if args.sarif:
        sarif_data = export_sarif(findings, milestone_title=args.title)
        with args.sarif.open("w", encoding="utf-8") as f:
            json.dump(sarif_data, f, indent=2)

    if args.json:
        payload = {
            "metrics": metrics.to_dict(),
            "phase": phase.value,
            "findings": [f.to_dict() for f in findings],
            "rollover_count": len(rollover),
            "rollover_candidate_numbers": [r.number for r in rollover],
        }
        print(json.dumps(payload, indent=2))
    else:
        report_str = format_cli_report(metrics, phase, findings, rollover)
        print(report_str)


def _has_critical_errors(findings: Sequence[GovernorFinding]) -> bool:
    """Return True if any finding is of level error."""
    return any(f.level == "error" for f in findings)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for milestone governor."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not args.file.is_file():
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        return 1

    issues = load_issues_from_json(args.file)
    phase = MilestonePhase(args.phase)

    metrics = calculate_milestone_metrics(issues, title=args.title, window_days=args.window_days)
    findings = audit_milestone(issues, phase=phase, metrics=metrics, max_issues=args.max_issues)
    _, rollover = partition_milestone_rollover(issues, target_milestone=args.rollover_to)

    _export_outputs(args, metrics, phase, findings, rollover)
    return 1 if _has_critical_errors(findings) else 0


if __name__ == "__main__":
    sys.exit(main())
