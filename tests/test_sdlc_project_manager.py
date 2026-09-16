"""Unit tests for the Autonomous SDLC Project Management & Prioritization Engine."""

import json
from pathlib import Path
import sys
import pytest

# Add tools/ to sys.path for direct imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from sdlc_project_manager import (
    DependencyGraph,
    LifecycleState,
    PrioritizationScorer,
    PriorityLevel,
    SDLCProjectManager,
    SDLCResource,
    SDLCResourceKind,
    load_resources_from_json,
    main,
)


def test_sdlc_resource_creation_and_dict() -> None:
    """Ensure SDLCResource instantiates and serializes to dictionary cleanly."""
    res = SDLCResource(
        resource_id="issue-101",
        kind=SDLCResourceKind.ISSUE,
        number=101,
        title="Implement AST complexity cap",
        labels=["type/feature", "component/ast"],
        lifecycle_state=LifecycleState.READY,
        priority=PriorityLevel.P1_HIGH,
        effort_points=3,
        business_value=8,
    )
    data = res.to_dict()
    assert data["number"] == 101
    assert data["kind"] == "issue"
    assert data["lifecycle_state"] == "Ready"
    assert data["priority"] == "P1_HIGH"


def test_dependency_graph_blocking_logic() -> None:
    """Ensure DependencyGraph accurately detects when an item is blocked."""
    res1 = SDLCResource("1", SDLCResourceKind.ISSUE, 1, "Base Schema", lifecycle_state=LifecycleState.IN_PROGRESS)
    res2 = SDLCResource("2", SDLCResourceKind.ISSUE, 2, "API Client", depends_on=[1])
    res3 = SDLCResource("3", SDLCResourceKind.ISSUE, 3, "CLI Wrapper", depends_on=[2])

    graph = DependencyGraph([res1, res2, res3])
    assert graph.is_blocked(2) is True
    assert graph.is_blocked(1) is False

    # Mark base schema as DONE
    res1.lifecycle_state = LifecycleState.DONE
    assert graph.is_blocked(2) is False
    assert graph.is_blocked(3) is True


def test_dependency_graph_circular_dependency_detection() -> None:
    """Ensure circular blocker loops are detected."""
    res1 = SDLCResource("1", SDLCResourceKind.ISSUE, 1, "Task A", depends_on=[2])
    res2 = SDLCResource("2", SDLCResourceKind.ISSUE, 2, "Task B", depends_on=[1])

    graph = DependencyGraph([res1, res2])
    assert graph.has_circular_dependency() is True

    # Break cycle
    res1.depends_on = []
    graph_clean = DependencyGraph([res1, res2])
    assert graph_clean.has_circular_dependency() is False


def test_prioritization_scoring_hierarchy() -> None:
    """Ensure security/failing PRs score higher than routine low-priority backlog items."""
    res_normal = SDLCResource(
        "1", SDLCResourceKind.ISSUE, 1, "Update README typo",
        priority=PriorityLevel.P3_LOW, effort_points=1, business_value=2
    )
    res_sec = SDLCResource(
        "2", SDLCResourceKind.ISSUE, 2, "Fix SSRF Vulnerability",
        labels=["security", "bug"], priority=PriorityLevel.P0_CRITICAL, effort_points=2, business_value=10
    )
    res_pr = SDLCResource(
        "3", SDLCResourceKind.PULL_REQUEST, 3, "Auth Refactor PR",
        checks_passing=False, priority=PriorityLevel.P1_HIGH
    )

    graph = DependencyGraph([res_normal, res_sec, res_pr])
    score_normal = PrioritizationScorer.score_resource(res_normal, graph)
    score_sec = PrioritizationScorer.score_resource(res_sec, graph)
    score_pr = PrioritizationScorer.score_resource(res_pr, graph)

    assert score_sec > score_normal
    assert score_pr > score_normal
    assert score_sec > 1000.0  # P0 + security + bug modifiers


def test_recommend_action_prioritizes_failing_pr_first() -> None:
    """Ensure active PR with failing checks is recommended before starting new issues."""
    issue_ready = SDLCResource("1", SDLCResourceKind.ISSUE, 1, "Implement Feature X", lifecycle_state=LifecycleState.READY)
    pr_failing = SDLCResource(
        "2", SDLCResourceKind.PULL_REQUEST, 2, "Bugfix PR",
        lifecycle_state=LifecycleState.IN_REVIEW, checks_passing=False
    )

    manager = SDLCProjectManager([issue_ready, pr_failing])
    rec = manager.recommend_next_agent_action()

    assert rec is not None
    assert rec.action_type == "REMEDIATE_PR_CHECKS"
    assert rec.target_resource.number == 2


def test_recommend_action_prioritizes_unresolved_review_threads() -> None:
    """Ensure PR with unresolved comments is handled before starting new issues."""
    issue_ready = SDLCResource("1", SDLCResourceKind.ISSUE, 1, "Feature Y", lifecycle_state=LifecycleState.READY)
    pr_review = SDLCResource(
        "2", SDLCResourceKind.PULL_REQUEST, 2, "Core PR",
        lifecycle_state=LifecycleState.IN_REVIEW, checks_passing=True, unresolved_review_threads=3
    )

    manager = SDLCProjectManager([issue_ready, pr_review])
    rec = manager.recommend_next_agent_action()

    assert rec is not None
    assert rec.action_type == "RESOLVE_REVIEW_THREADS"
    assert rec.target_resource.number == 2


def test_recommend_action_bypasses_blocked_issues() -> None:
    """Ensure blocked issues are skipped in favor of the highest unblocked issue."""
    issue_blocked = SDLCResource(
        "1", SDLCResourceKind.ISSUE, 1, "Blocked Subtask",
        priority=PriorityLevel.P0_CRITICAL, lifecycle_state=LifecycleState.READY, depends_on=[99]
    )
    dep_in_progress = SDLCResource("99", SDLCResourceKind.ISSUE, 99, "Blocker Task", lifecycle_state=LifecycleState.IN_PROGRESS)
    issue_free = SDLCResource(
        "2", SDLCResourceKind.ISSUE, 2, "Unblocked Work",
        priority=PriorityLevel.P1_HIGH, lifecycle_state=LifecycleState.READY
    )

    manager = SDLCProjectManager([issue_blocked, dep_in_progress, issue_free])
    rec = manager.recommend_next_agent_action()

    assert rec is not None
    assert rec.action_type == "IMPLEMENT_ISSUE"
    # Should recommend the blocker task (already in progress) or unblocked issue, never the blocked task!
    assert rec.target_resource.number in (99, 2)


def test_kanban_board_rendering() -> None:
    """Ensure Kanban board renders all lifecycle columns cleanly."""
    res1 = SDLCResource("1", SDLCResourceKind.ISSUE, 1, "Design Architecture", lifecycle_state=LifecycleState.IN_PROGRESS)
    res2 = SDLCResource("2", SDLCResourceKind.ISSUE, 2, "Write Documentation", lifecycle_state=LifecycleState.READY)

    manager = SDLCProjectManager([res1, res2])
    board = manager.render_kanban_board()

    assert "AUTONOMOUS SDLC PROJECT BOARD" in board
    assert "[ IN PROGRESS ]" in board
    assert "[ READY ]" in board
    assert "#1 [ISSUE] Design Architecture" in board


def test_cli_prioritize_and_next_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Ensure CLI correctly loads JSON, prioritizes, and outputs recommendations."""
    test_json = tmp_path / "project_state.json"
    data = [
        {"number": 10, "title": "Setup CI matrix", "kind": "issue", "priority": "P1_HIGH", "lifecycle_state": "Ready"},
        {"number": 11, "title": "Patch buffer leak", "kind": "pull_request", "checks_passing": False, "lifecycle_state": "In Review"}
    ]
    test_json.write_text(json.dumps(data), encoding="utf-8")

    # 1. Test 'next' command
    exit_code = main(["next", "--file", str(test_json)])
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "REMEDIATE_PR_CHECKS" in captured
    assert "#11" in captured

    # 2. Test 'prioritize' command with JSON
    exit_code_prio = main(["prioritize", "--file", str(test_json), "--json"])
    assert exit_code_prio == 0
    prio_output = json.loads(capsys.readouterr().out)
    assert len(prio_output) == 2
    assert prio_output[0]["number"] == 11  # Failing PR has highest priority score
