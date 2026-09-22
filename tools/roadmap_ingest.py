#!/usr/bin/env python3
"""Roadmap ingestion: make declared intent visible to the prioritizer.

Every work generator in this repository is defect-shaped. The workbench reports
complexity, decay, missing tests and documentation drift; the sentinel reports invariant
breaches; the quantifier reports smells. All of them answer "what is wrong with what
exists". None answers "what should exist next".

The consequence is measurable and was observed directly: with the repository certified at
100.0/100 the backlog held **0 items** while `docs/ROADMAP.md` held **11 open ones**. An
agentic project can exhaust its mechanically-derivable work while every feature it set out
to build remains untouched, and the loop will report that state as success.

`SDLCResource` already carries `business_value`, `effort_points`, `milestone` and
`depends_on`, and `PrioritizationScorer` already computes a value-over-effort ratio. The
prioritizer was built for features and was never given one. This module parses the roadmap
into that shape so declared intent and measured defects are ranked against each other by
the same arithmetic, rather than competing for attention in different documents.

Rejected work is never ingested. The roadmap's anti-pattern rows record decisions *not* to
build things, and a loop that schedules them has inverted the decision it was told.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Sequence

DEFAULT_ROADMAP = Path("docs/ROADMAP.md")
DEFAULT_BACKLOG = Path(".data/sdlc_backlog.json")

# `- [ ] **Title (P0 - Critical)**:` — the roadmap's own item grammar.
# Trailing annotations after the bold title — "(partially delivered)", "(blocked: why)" —
# are common and must not cause the item to be dropped. An ingester that silently skips
# an entry reports a shorter backlog than the project actually has. The note group is
# non-greedy rather than "everything up to a colon" so that an annotation may contain
# one; under the old pattern "(blocked: reason)" failed to match the line at all, which
# discarded exactly the items someone had taken the trouble to annotate.
_OPEN_ITEM_RE: Final[re.Pattern[str]] = re.compile(
    r"^- \[ \] \*\*(?P<title>.+?)(?:\s*\((?P<priority>P[0-3])\s*-\s*[A-Za-z]+\))?\*\*(?P<note>.*?):?\s*$"
)
_MILESTONE_RE: Final[re.Pattern[str]] = re.compile(r"^### (?P<name>.+?)\s*\((?P<version>v[\d.]+)")
_CONTEXT_RE: Final[re.Pattern[str]] = re.compile(r"^\s+- \*(?P<label>[^*]+)\*:\s*(?P<text>.+)$")
# Rows of the Value vs Effort matrix: | category | feature | tech | value | effort | ... |
_MATRIX_ROW_RE: Final[re.Pattern[str]] = re.compile(
    r"^\|[^|]*\|\s*(?P<feature>[^|]+?)\s*\|[^|]*\|\s*(?P<value>High|Medium|Low)\s*\|"
    r"\s*(?P<effort>High|Medium|Low)\s*\|"
)

# T-shirt sizes to points. Value and effort use inverted scales on purpose: the scorer
# divides one by the other, so "High value, Low effort" must produce the largest ratio.
VALUE_POINTS: Final[dict[str, int]] = {"High": 8, "Medium": 5, "Low": 2}
EFFORT_POINTS: Final[dict[str, int]] = {"Low": 2, "Medium": 5, "High": 8}
DEFAULT_PRIORITY: Final[str] = "P2_MEDIUM"
PRIORITY_NAMES: Final[dict[str, str]] = {
    "P0": "P0_CRITICAL", "P1": "P1_HIGH", "P2": "P2_MEDIUM", "P3": "P3_LOW",
}
# Sections recording decisions not to build something.
REJECTED_MARKERS: Final[tuple[str, ...]] = ("Anti-Patterns", "Rejected")

# `(blocked: reason)` in an item's trailing annotation. The roadmap grammar already
# tolerated a "(blocked)" note but nothing acted on it, so a deliberate deferral was
# discarded and the prioritizer proposed the same item on the next pass. A reason is
# mandatory: a blocker nobody can read is indistinguishable from an excuse.
_BLOCKED_RE: Final[re.Pattern[str]] = re.compile(
    r"\(\s*blocked\s*:\s*(?P<reason>[^)]+?)\s*\)", re.IGNORECASE
)


@dataclass
class RoadmapItem:
    """One open roadmap deliverable, in the shape the prioritizer consumes."""

    title: str
    milestone: str
    priority: str = DEFAULT_PRIORITY
    business_value: int = VALUE_POINTS["Medium"]
    effort_points: int = EFFORT_POINTS["Medium"]
    context: str = ""
    line_number: int = 0
    # False when value and effort fell back to defaults because the prioritization matrix
    # has no row for this item. The ranking is then a guess, and says so rather than
    # presenting an assumed number with the same confidence as a recorded one.
    sized_from_matrix: bool = True
    note: str = ""
    blocked_reason: str = ""

    def _guidance(self) -> str:
        """Describe the deliverable and how confidently it was sized."""
        if self.blocked_reason:
            return (
                f"Blocked: {self.blocked_reason}. Recorded in docs/ROADMAP.md; the item "
                "stays ranked but is not schedulable until the annotation is removed."
            )
        base = self.context or f"Deliverable scheduled for {self.milestone}."
        annotation = f" Roadmap note: {self.note}." if self.note else ""
        if self.sized_from_matrix:
            return f"{base}{annotation}"
        return (
            f"{base}{annotation} Value and effort defaulted to Medium: the prioritization "
            "matrix has no row for this item, so its rank is an assumption rather than a "
            "recorded judgement. Add a matrix row to rank it deliberately."
        )

    def to_backlog_task(self, number: int) -> dict[str, object]:
        """Render as an SDLC backlog task, matching the workbench's export schema."""
        labels = ["enhancement", "roadmap", self.milestone]
        if self.blocked_reason:
            labels.append("blocked")
        return {
            "resource_id": f"roadmap-{number}",
            "kind": "issue",
            "number": number,
            "title": f"[ROADMAP] {self.title}",
            "labels": labels,
            "lifecycle_state": "Backlog",
            "priority": self.priority,
            "milestone": self.milestone,
            "effort_points": self.effort_points,
            "business_value": self.business_value,
            "blocked_reason": self.blocked_reason,
            "prescriptive_guidance": self._guidance(),
            "suggested_action": "Design, implement and verify the deliverable, then check it off in docs/ROADMAP.md.",
        }


def parse_value_effort_matrix(lines: Sequence[str]) -> dict[str, tuple[int, int]]:
    """Map feature name to (business value, effort) from the prioritization matrix.

    The matrix is where a human already recorded the judgement calls. Re-deriving value
    and effort from the item text would be guesswork replacing a decision someone made
    deliberately.
    """
    sized: dict[str, tuple[int, int]] = {}
    for line in lines:
        match = _MATRIX_ROW_RE.match(line)
        if not match:
            continue
        feature = _normalize(match.group("feature"))
        if feature:
            sized[feature] = (
                VALUE_POINTS[match.group("value")], EFFORT_POINTS[match.group("effort")]
            )
    return sized


def _normalize(text: str) -> str:
    """Reduce a title to a comparable key, dropping emphasis, code ticks and commands."""
    stripped = re.sub(r"[*`]", "", text)
    stripped = re.sub(r"\([^)]*\)", "", stripped)
    return re.sub(r"[^a-z0-9 ]", "", stripped.lower()).strip()


def _match_sizing(title: str, matrix: dict[str, tuple[int, int]]) -> tuple[int, int] | None:
    """Find the matrix row describing an item, by exact key then by containment."""
    key = _normalize(title)
    if key in matrix:
        return matrix[key]
    candidates = [v for k, v in matrix.items() if k and (k in key or key in k)]
    return candidates[0] if len(candidates) == 1 else None


def _is_rejected_section(heading: str) -> bool:
    """Return True for sections recording decisions not to build something."""
    return any(marker.lower() in heading.lower() for marker in REJECTED_MARKERS)


def parse_roadmap(path: Path) -> list[RoadmapItem]:
    """Extract every open deliverable from a roadmap document."""
    lines = path.read_text(encoding="utf-8").splitlines()
    matrix = parse_value_effort_matrix(lines)
    scan = _RoadmapScan()
    for number, line in enumerate(lines, 1):
        _absorb_line(line, number, scan, matrix)
    return scan.items


@dataclass
class _RoadmapScan:
    """Parser state while walking the roadmap: current section and items found so far."""

    items: list[RoadmapItem] = field(default_factory=list)
    milestone: str = "unscheduled"
    rejected: bool = False


def _absorb_line(
    line: str, number: int, scan: _RoadmapScan, matrix: dict[str, tuple[int, int]]
) -> None:
    """Fold one roadmap line into the scan."""
    heading = _MILESTONE_RE.match(line)
    if heading:
        scan.milestone, scan.rejected = heading.group("version"), False
        return
    if line.startswith("#"):
        scan.rejected = _is_rejected_section(line)
        return
    if _absorb_context(line, scan):
        return
    match = _OPEN_ITEM_RE.match(line)
    if match and not scan.rejected:
        scan.items.append(_build_item(match, scan.milestone, number, matrix))


def _absorb_context(line: str, scan: _RoadmapScan) -> bool:
    """Attach the first context bullet under an item, returning whether the line was one."""
    context = _CONTEXT_RE.match(line)
    if not context:
        return False
    if scan.items and not scan.items[-1].context:
        scan.items[-1].context = f"{context.group('label')}: {context.group('text')}"
    return True


def _build_item(
    match: re.Match[str], milestone: str, line_number: int, matrix: dict[str, tuple[int, int]]
) -> RoadmapItem:
    """Assemble one roadmap item, taking value and effort from the matrix when present."""
    title = match.group("title").strip()
    sizing = _match_sizing(title, matrix)
    value, effort = sizing or (VALUE_POINTS["Medium"], EFFORT_POINTS["Medium"])
    note = (match.group("note") or "").strip()
    blocked = _BLOCKED_RE.search(note)
    return RoadmapItem(
        title=re.sub(r"[*`]", "", title),
        milestone=milestone,
        priority=PRIORITY_NAMES.get(match.group("priority") or "", DEFAULT_PRIORITY),
        business_value=value,
        effort_points=effort,
        line_number=line_number,
        sized_from_matrix=sizing is not None,
        note=note,
        blocked_reason=blocked.group("reason").strip() if blocked else "",
    )


def merge_into_backlog(items: Sequence[RoadmapItem], backlog: Path) -> list[dict[str, object]]:
    """Add roadmap items to an existing backlog, replacing any previous roadmap entries.

    Defects and features land in one file so a single prioritizer ranks them together.
    Previous roadmap entries are replaced rather than appended, because an item checked
    off in the roadmap must disappear from the backlog on the next ingest.
    """
    existing: list[dict[str, object]] = []
    if backlog.exists():
        try:
            loaded = json.loads(backlog.read_text(encoding="utf-8"))
            existing = [t for t in loaded if not str(t.get("resource_id", "")).startswith("roadmap-")]
        except (OSError, json.JSONDecodeError):
            existing = []
    tasks = existing + [item.to_backlog_task(2000 + i) for i, item in enumerate(items, 1)]
    backlog.parent.mkdir(parents=True, exist_ok=True)
    backlog.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
    return tasks


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser for roadmap ingestion."""
    parser = argparse.ArgumentParser(description="Ingest roadmap deliverables into the SDLC backlog")
    parser.add_argument("--roadmap", type=Path, default=DEFAULT_ROADMAP, help="Roadmap document")
    parser.add_argument("--backlog", type=Path, default=DEFAULT_BACKLOG, help="Backlog to merge into")
    parser.add_argument("--dry-run", action="store_true", help="Print items without writing")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI runner for roadmap ingestion."""
    args = build_arg_parser().parse_args(list(argv[1:]) if argv is not None else None)
    if not args.roadmap.exists():
        print(f"MISSING {args.roadmap} — nothing to ingest.", file=sys.stderr)
        return 1

    items = parse_roadmap(args.roadmap)
    for item in items:
        ratio = item.business_value / max(item.effort_points, 1)
        sized = " " if item.sized_from_matrix else "~"
        print(f" {sized}{item.priority:<12} {item.milestone:<8} value/effort={ratio:.2f}  {item.title[:54]}")
    unsized = sum(1 for item in items if not item.sized_from_matrix)
    if unsized:
        print(f"\n  ~ {unsized} item(s) have no prioritization matrix row; ranked on defaults.")
    if args.dry_run:
        print(f"{len(items)} open deliverable(s); nothing written.")
        return 0

    tasks = merge_into_backlog(items, args.backlog)
    print(f"\nMerged {len(items)} roadmap item(s) into {args.backlog} ({len(tasks)} total tasks).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
