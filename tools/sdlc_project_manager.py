#!/usr/bin/env python3
"""Autonomous SDLC Project Management & Prioritization Engine for AI Agents.

Organizes, models, and deterministically prioritizes GitHub SDLC resources:
- Issues (Features, Bugs, Technical Debt)
- Pull Requests (WIP, Review Threads, CI Checks, FIFO Aging)
- Milestones & Releases
- GitHub Projects v2 Kanban Board States & Custom Fields

Enables autonomous agents to self-steer, avoid PR starvation, unblock critical
paths, and maintain continuous alignment with human project managers.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from enum import Enum
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

# Canonical priority weights
PRIORITY_BASE_WEIGHTS = {
    "P0_CRITICAL": 1000.0,
    "P1_HIGH": 500.0,
    "P2_MEDIUM": 200.0,
    "P3_LOW": 50.0,
}

# Label severity modifiers
LABEL_WEIGHT_MODIFIERS = {
    "security": 400.0,
    "bug": 250.0,
    "regression": 300.0,
    "performance": 150.0,
    "feature": 100.0,
    "docs": 50.0,
}


class SDLCResourceKind(str, Enum):
    """Classification of SDLC resource on GitHub."""
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"
    MILESTONE = "milestone"


class PriorityLevel(str, Enum):
    """Urgency and business impact priority level."""
    P0_CRITICAL = "P0_CRITICAL"
    P1_HIGH = "P1_HIGH"
    P2_MEDIUM = "P2_MEDIUM"
    P3_LOW = "P3_LOW"


class LifecycleState(str, Enum):
    """Canonical GitHub Projects v2 Kanban lifecycle state."""
    BACKLOG = "Backlog"
    READY = "Ready"
    IN_PROGRESS = "In Progress"
    IN_REVIEW = "In Review"
    DONE = "Done"
    BLOCKED = "Blocked"


@dataclass
class SDLCResource:
    """Represents a discrete SDLC artifact tracked on GitHub."""
    resource_id: str
    kind: SDLCResourceKind
    number: int
    title: str
    labels: list[str] = field(default_factory=list)
    milestone: str | None = None
    lifecycle_state: LifecycleState = LifecycleState.BACKLOG
    priority: PriorityLevel = PriorityLevel.P2_MEDIUM
    age_days: float = 0.0
    is_draft: bool = False
    checks_passing: bool = True
    unresolved_review_threads: int = 0
    depends_on: list[int] = field(default_factory=list)
    blocks: list[int] = field(default_factory=list)
    effort_points: int = 2
    business_value: int = 5
    calculated_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize SDLC resource state to JSON-compatible dictionary."""
        data = asdict(self)
        data["kind"] = self.kind.value
        data["lifecycle_state"] = self.lifecycle_state.value
        data["priority"] = self.priority.value
        return data


@dataclass
class AgentActionRecommendation:
    """Prescriptive next action for an autonomous agent or swarm."""
    action_type: str  # "REMEDIATE_PR", "REVIEW_PR", "IMPLEMENT_ISSUE", "RESOLVE_BLOCKER"
    target_resource: SDLCResource
    rationale: str
    priority_score: float


class DependencyGraph:
    """Analyzes blocker and dependency relationships between SDLC items."""

    def __init__(self, resources: list[SDLCResource]) -> None:
        self.resources_by_num = {r.number: r for r in resources}
        self.adj_list: dict[int, list[int]] = {r.number: list(r.depends_on) for r in resources}

    def has_circular_dependency(self) -> bool:
        """Detect whether circular blocker loops exist."""
        visited: set[int] = set()
        stack: set[int] = set()
        unvisited = (node for node in self.adj_list if node not in visited)
        return any(self._dfs_cycle(node, visited, stack) for node in unvisited)

    def _dfs_cycle(self, node: int, visited: set[int], stack: set[int]) -> bool:
        visited.add(node)
        stack.add(node)

        for neighbor in self.adj_list.get(node, []):
            if neighbor in stack:
                return True
            if neighbor not in visited and self._dfs_cycle(neighbor, visited, stack):
                return True

        stack.remove(node)
        return False

    def is_blocked(self, resource_number: int) -> bool:
        """Check if any dependencies are not in DONE state."""
        deps = self.adj_list.get(resource_number, [])
        for dep_num in deps:
            dep_res = self.resources_by_num.get(dep_num)
            if dep_res and dep_res.lifecycle_state != LifecycleState.DONE:
                return True
        return False


class PrioritizationScorer:
    """Calculates deterministic multi-dimensional priority scores."""

    @classmethod
    def score_resource(cls, res: SDLCResource, dep_graph: DependencyGraph) -> float:
        """Compute holistic SDLC priority score."""
        score = PRIORITY_BASE_WEIGHTS.get(res.priority.value, 200.0)

        # 1. Add label modifiers
        score += cls._label_modifiers(res.labels)

        # 2. Add unblocking multiplier: items that unblock others get boosted
        score += len(res.blocks) * 150.0

        # 3. Pull Request state adjustments
        if res.kind == SDLCResourceKind.PULL_REQUEST:
            score += cls._pr_modifiers(res)

        # 4. Value vs Effort ROI boost
        roi_boost = (res.business_value * 25.0) / max(res.effort_points, 1)
        score += roi_boost

        # 5. FIFO aging bonus: older items advance to prevent starvation
        score += min(res.age_days * 12.0, 200.0)

        # 6. Penalty if blocked by incomplete dependencies
        if dep_graph.is_blocked(res.number):
            score *= 0.2

        res.calculated_score = round(score, 2)
        return res.calculated_score

    @staticmethod
    def _label_modifiers(labels: list[str]) -> float:
        bonus = 0.0
        for lbl in labels:
            clean_lbl = lbl.lower().replace("type/", "").replace("priority/", "")
            bonus += LABEL_WEIGHT_MODIFIERS.get(clean_lbl, 0.0)
        return bonus

    @staticmethod
    def _pr_modifiers(res: SDLCResource) -> float:
        bonus = 300.0  # In-flight PRs generally take precedence to complete loops
        if not res.checks_passing:
            bonus += 250.0  # Failing checks MUST be fixed immediately
        if res.unresolved_review_threads > 0:
            bonus += res.unresolved_review_threads * 50.0
        return bonus


class SDLCProjectManager:
    """Orchestrates SDLC resource prioritization, boards, and agent actions."""

    def __init__(self, resources: list[SDLCResource]) -> None:
        self.resources = resources
        self.dep_graph = DependencyGraph(resources)
        self.recompute_scores()

    def recompute_scores(self) -> None:
        """Calculate priority scores for all resources."""
        for res in self.resources:
            PrioritizationScorer.score_resource(res, self.dep_graph)

    def get_ranked_resources(self) -> list[SDLCResource]:
        """Return resources ordered by calculated priority score descending."""
        return sorted(self.resources, key=lambda r: r.calculated_score, reverse=True)

    def _find_pr_remediation_action(self, ranked: list[SDLCResource]) -> AgentActionRecommendation | None:
        """Find active PRs requiring immediate remediation (checks or review threads)."""
        for res in ranked:
            if res.kind != SDLCResourceKind.PULL_REQUEST or res.lifecycle_state == LifecycleState.DONE:
                continue
            if not res.checks_passing:
                return AgentActionRecommendation(
                    action_type="REMEDIATE_PR_CHECKS",
                    target_resource=res,
                    rationale=f"PR #{res.number} has failing CI checks. Remediate to maintain release integrity.",
                    priority_score=res.calculated_score,
                )
            if res.unresolved_review_threads > 0:
                return AgentActionRecommendation(
                    action_type="RESOLVE_REVIEW_THREADS",
                    target_resource=res,
                    rationale=f"PR #{res.number} has {res.unresolved_review_threads} unresolved review threads.",
                    priority_score=res.calculated_score,
                )
        return None

    def _is_unblocked_candidate(self, res: SDLCResource) -> bool:
        """Predicate checking if a resource is an unblocked backlog or ready issue."""
        if res.kind != SDLCResourceKind.ISSUE:
            return False
        if res.lifecycle_state not in (LifecycleState.READY, LifecycleState.BACKLOG):
            return False
        return not self.dep_graph.is_blocked(res.number)

    def _find_unblocked_issue_action(self, ranked: list[SDLCResource]) -> AgentActionRecommendation | None:
        """Find top unblocked issue ready for implementation."""
        for res in ranked:
            if self._is_unblocked_candidate(res):
                return AgentActionRecommendation(
                    action_type="IMPLEMENT_ISSUE",
                    target_resource=res,
                    rationale=f"Top unblocked issue #{res.number} ('{res.title}') ready for TDD implementation.",
                    priority_score=res.calculated_score,
                )
        return None

    def recommend_next_agent_action(self) -> AgentActionRecommendation | None:
        """Determine the single highest-leverage action the agent should take next."""
        ranked = self.get_ranked_resources()
        if not ranked:
            return None

        # Rule 1: Remediate any active Pull Request with failing checks or review threads
        pr_action = self._find_pr_remediation_action(ranked)
        if pr_action:
            return pr_action

        # Rule 2: Implement highest-priority unblocked issue
        issue_action = self._find_unblocked_issue_action(ranked)
        if issue_action:
            return issue_action

        # Fallback: Progress top ranked item
        top = ranked[0]
        return AgentActionRecommendation(
            action_type="PROGRESS_ITEM",
            target_resource=top,
            rationale=f"Advance #{top.number} ('{top.title}') through lifecycle.",
            priority_score=top.calculated_score,
        )

    def render_kanban_board(self) -> str:
        """Render ASCII Kanban board representation of current project state."""
        columns: dict[LifecycleState, list[SDLCResource]] = {state: [] for state in LifecycleState}
        for res in self.resources:
            columns[res.lifecycle_state].append(res)

        lines = [
            "==========================================================================================",
            "📋 AUTONOMOUS SDLC PROJECT BOARD (GitHub Projects v2)",
            "==========================================================================================",
        ]

        states = (LifecycleState.IN_PROGRESS, LifecycleState.IN_REVIEW, LifecycleState.READY, LifecycleState.BACKLOG, LifecycleState.DONE)
        for state in states:
            lines.extend(_render_column(state, columns[state], self.dep_graph))

        lines.append("==========================================================================================")
        return "\n".join(lines)


def _render_column_item(item: SDLCResource, is_blocked: bool) -> str:
    """Format single resource line in Kanban column."""
    prefix = "PR" if item.kind == SDLCResourceKind.PULL_REQUEST else "ISSUE"
    blocked_tag = " [🚫 BLOCKED]" if is_blocked else ""
    return (
        f"  • #{item.number} [{prefix}] {item.title[:40]:<40} "
        f"| {item.priority.value:<11} | Score: {item.calculated_score:>6.1f}{blocked_tag}"
    )


def _render_column(state: LifecycleState, items: list[SDLCResource], dep_graph: DependencyGraph) -> list[str]:
    """Format single Kanban lifecycle state column."""
    lines = [f"\n[ {state.value.upper()} ] ({len(items)} items)"]
    if not items:
        lines.append("  (empty)")
        return lines
    sorted_items = sorted(items, key=lambda r: r.calculated_score, reverse=True)
    lines.extend(_render_column_item(item, dep_graph.is_blocked(item.number)) for item in sorted_items)
    return lines


def load_resources_from_json(path: Path) -> list[SDLCResource]:
    """Load SDLC resources from a JSON state file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    resources: list[SDLCResource] = []

    for item in raw:
        resources.append(
            SDLCResource(
                resource_id=str(item.get("resource_id", item.get("number", ""))),
                kind=SDLCResourceKind(item.get("kind", "issue")),
                number=int(item["number"]),
                title=str(item["title"]),
                labels=list(item.get("labels", [])),
                milestone=item.get("milestone"),
                lifecycle_state=LifecycleState(item.get("lifecycle_state", "Backlog")),
                priority=PriorityLevel(item.get("priority", "P2_MEDIUM")),
                age_days=float(item.get("age_days", 0.0)),
                is_draft=bool(item.get("is_draft", False)),
                checks_passing=bool(item.get("checks_passing", True)),
                unresolved_review_threads=int(item.get("unresolved_review_threads", 0)),
                depends_on=[int(n) for n in item.get("depends_on", [])],
                blocks=[int(n) for n in item.get("blocks", [])],
                effort_points=int(item.get("effort_points", 2)),
                business_value=int(item.get("business_value", 5)),
            )
        )
    return resources


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for SDLC project manager."""
    parser = argparse.ArgumentParser(description="Autonomous SDLC Project Management & Prioritization Engine.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Command: prioritize
    cmd_prio = subparsers.add_parser("prioritize", help="Rank all SDLC resources by calculated priority score.")
    cmd_prio.add_argument("--file", "-f", required=True, type=Path, help="Path to resources JSON file.")
    cmd_prio.add_argument("--json", action="store_true", help="Output results in JSON format.")

    # Command: next
    cmd_next = subparsers.add_parser("next", help="Recommend the single next action for the agent.")
    cmd_next.add_argument("--file", "-f", required=True, type=Path, help="Path to resources JSON file.")

    # Command: board
    cmd_board = subparsers.add_parser("board", help="Display ASCII Kanban board.")
    cmd_board.add_argument("--file", "-f", required=True, type=Path, help="Path to resources JSON file.")

    return parser


def _handle_prioritize(manager: SDLCProjectManager, is_json: bool) -> int:
    ranked = manager.get_ranked_resources()
    if is_json:
        print(json.dumps([r.to_dict() for r in ranked], indent=2))
        return 0
    print(manager.render_kanban_board())
    print("\n🎯 TOP RANKED PRIORITIES:")
    for i, r in enumerate(ranked[:5], 1):
        print(f"  {i}. #{r.number} ({r.kind.value}): {r.title} — Score: {r.calculated_score} ({r.priority.value})")
    return 0


def _handle_next(manager: SDLCProjectManager) -> int:
    rec = manager.recommend_next_agent_action()
    if not rec:
        print("No actionable tasks available.")
        return 0
    print("🤖 RECOMMENDED AGENT NEXT ACTION:")
    print(f"  Action:    {rec.action_type}")
    print(f"  Target:    #{rec.target_resource.number} ({rec.target_resource.kind.value}) — {rec.target_resource.title}")
    print(f"  Score:     {rec.priority_score}")
    print(f"  Rationale: {rec.rationale}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Execute SDLC project manager command line interface."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not args.file.is_file():
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        return 1

    resources = load_resources_from_json(args.file)
    manager = SDLCProjectManager(resources)

    dispatch = {
        "prioritize": lambda: _handle_prioritize(manager, getattr(args, "json", False)),
        "next": lambda: _handle_next(manager),
        "board": lambda: (print(manager.render_kanban_board()), 0)[1],
    }
    handler = dispatch.get(args.command)
    return handler() if handler else 0


if __name__ == "__main__":
    sys.exit(main())
