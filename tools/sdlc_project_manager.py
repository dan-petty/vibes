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
import time
from pathlib import Path
import re
import sys
from typing import Any, Sequence

# Canonical priority weights
# Passes are cheap; the interval exists so a watch does not spin.
WATCH_INTERVAL_SECONDS = 2.0

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


def load_resources_from_dicts(payload: Sequence[dict[str, Any]]) -> list[SDLCResource]:
    """Build resources from already-parsed task dictionaries."""
    return [_resource_from_dict(item) for item in payload]


def _resource_from_dict(item: dict[str, Any]) -> SDLCResource:
    """Build one resource from a task dictionary."""
    return SDLCResource(
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


def load_resources_from_json(path: Path) -> list[SDLCResource]:
    """Load SDLC resources from a JSON state file."""
    return load_resources_from_dicts(json.loads(path.read_text(encoding="utf-8")))


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
    cmd_sync = subparsers.add_parser(
        "sync", help="Reconcile the board against a fresh scan, closing resolved cards."
    )
    cmd_sync.add_argument("--file", "-f", required=True, type=Path, help="Path to resources JSON file.")
    cmd_sync.add_argument(
        "--scan", type=Path, help="Fresh backlog export to reconcile against (default: the same file)."
    )
    cmd_sync.add_argument(
        "--watch", action="store_true", help="Reconcile continuously until interrupted."
    )
    cmd_sync.add_argument(
        "--interval", type=float, default=WATCH_INTERVAL_SECONDS, help="Seconds between passes."
    )
    cmd_sync.add_argument("--max-passes", type=int, default=None, help="Stop after this many passes.")

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


def _sync_once(backlog: Path, scan: Path) -> ReconciliationResult:
    """Run one reconciliation pass and persist the result."""
    existing = json.loads(backlog.read_text(encoding="utf-8")) if backlog.is_file() else []
    current = json.loads(scan.read_text(encoding="utf-8")) if scan.is_file() else []
    merged, result = reconcile_backlog(existing, current)

    manager = SDLCProjectManager(load_resources_from_dicts(merged))
    merged, promoted = advance_ready_item(merged, manager)
    if promoted:
        result.advanced.append(promoted)

    backlog.parent.mkdir(parents=True, exist_ok=True)
    backlog.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return result


def _handle_sync(args: argparse.Namespace) -> int:
    """Reconcile the board once, or continuously until interrupted."""
    scan = args.scan or args.file
    passes = 0
    try:
        while True:
            result = _sync_once(args.file, scan)
            passes += 1
            print(f"[sync {passes}] {result.summary()}")
            for label, names in (("closed", result.closed), ("opened", result.opened), ("ready", result.advanced)):
                for name in names:
                    print(f"    {label}: {name[:88]}")
            if not args.watch or (args.max_passes is not None and passes >= args.max_passes):
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nStopped after {passes} pass(es).")
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
        "sync": lambda: _handle_sync(args),
    }
    handler = dispatch.get(args.command)
    return handler() if handler else 0

@dataclass
class ReconciliationResult:
    """What one reconciliation pass changed about the board."""

    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    advanced: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        """Return whether the pass moved anything."""
        return bool(self.opened or self.closed or self.advanced)

    def summary(self) -> str:
        """Render a one-line description of the pass."""
        return (
            f"opened {len(self.opened)}, closed {len(self.closed)}, "
            f"advanced {len(self.advanced)}, unchanged {self.unchanged}"
        )


def _card_identity(task: dict[str, Any]) -> str:
    """Return the stable identity of a card, which is its title rather than its number.

    Numbers are assigned by export order and shift whenever the finding set changes, so
    keying on them would close and reopen every card on each pass. The title carries the
    finding's subject and location, which is what makes two cards the same card.
    """
    return str(task.get("title", "")).strip()


def _is_roadmap_card(task: dict[str, Any]) -> bool:
    """Return True for cards sourced from the roadmap rather than from a scan."""
    return str(task.get("resource_id", "")).startswith("roadmap-")


def reconcile_backlog(
    existing: Sequence[dict[str, Any]], current: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], ReconciliationResult]:
    """Fold a fresh scan into the existing board, closing what the scan no longer reports.

    A defect card exists because a scan reported a finding. When the finding stops being
    reported the defect is fixed, and leaving the card open makes the board a record of
    what was once wrong rather than of what is wrong now.

    Roadmap cards are exempt: their absence from a scan means nothing, since no scan can
    observe an unbuilt feature. They close when the roadmap says so, which ingestion
    handles by replacing them wholesale.
    """
    result = ReconciliationResult()
    current_by_id = {_card_identity(task): task for task in current}
    merged: list[dict[str, Any]] = []

    for task in existing:
        identity = _card_identity(task)
        if _is_roadmap_card(task):
            merged.append(task)
            result.unchanged += 1
        elif identity in current_by_id:
            merged.append(task)
            result.unchanged += 1
        elif task.get("lifecycle_state") != LifecycleState.DONE.value:
            closed = {**task, "lifecycle_state": LifecycleState.DONE.value}
            merged.append(closed)
            result.closed.append(identity)

    known = {_card_identity(task) for task in merged}
    for identity, task in current_by_id.items():
        if identity not in known:
            merged.append(task)
            result.opened.append(identity)
    return merged, result


def advance_ready_item(
    tasks: Sequence[dict[str, Any]], manager: SDLCProjectManager
) -> tuple[list[dict[str, Any]], str | None]:
    """Promote the highest-ranked unblocked backlog card to Ready.

    Exactly one card is promoted per pass. A board where everything is Ready states no
    order, which is the same as stating no priority.
    """
    ranked = manager.get_ranked_resources()
    candidates = (
        res for res in ranked
        if res.lifecycle_state is LifecycleState.BACKLOG and manager._is_unblocked_candidate(res)
    )
    target = next(candidates, None)
    if target is None:
        return list(tasks), None
    promoted = [
        {**task, "lifecycle_state": LifecycleState.READY.value}
        if _card_identity(task) == target.title.strip()
        else task
        for task in tasks
    ]
    return promoted, target.title.strip()


if __name__ == "__main__":
    sys.exit(main())
