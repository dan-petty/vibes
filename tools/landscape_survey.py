#!/usr/bin/env python3
"""Survey comparable open-source projects, score their maturity, and turn gaps into roadmap items.

Every other work generator here looks inward. The workbench reports decay, the sentinel
reports invariant breaches, the quantifier reports smells — all of them answer "what is
wrong with what exists", and the roadmap ingester answers "what did we say we would
build". None of them can see that somebody else already solved a problem better, or that
a capability this repository treats as finished is two features behind the field.

Three separations keep that from becoming fabrication:

* **Facts** come from the GitHub API and are cached in a committed snapshot. Maturity is
  scored against the snapshot's own fetch time rather than the wall clock, so re-running a
  report produces a byte-identical answer and a diff means the world changed, not the day.
* **Feature claims** are human-curated in the manifest and each one carries its evidence.
  Inferring a feature from README keywords is how a survey invents a matrix nobody can
  defend, and a loop downstream of it schedules the invention.
* **A feature nobody has assessed is `unknown`, never `no`.** Gap detection reads cited
  claims alone, so an unknown can never manufacture work. This is the same discipline the
  backlog uses for assumption-ranked items: say which numbers were measured.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import yaml
from roadmap_emit import DEFAULT_MILESTONE, DEFAULT_PRIORITY, Proposal, insert_proposals
from upstream_facts import (
    RepoFacts,
    assess_maturity,
    discover_candidates,
    fetch_facts,
    load_snapshot,
    save_snapshot,
)

DEFAULT_MANIFEST: Final[Path] = Path("docs/landscape/capabilities.yaml")
DEFAULT_SNAPSHOT: Final[Path] = Path("docs/landscape/snapshot.json")
DEFAULT_ROADMAP: Final[Path] = Path("docs/ROADMAP.md")







@dataclass(frozen=True)
class Gap:
    """A feature an alternative demonstrably has and this repository's capability does not."""

    capability: str
    capability_title: str
    capability_short: str
    feature: str
    feature_summary: str
    holders: tuple[str, ...]
    evidence: tuple[str, ...]

    def roadmap_title(self) -> str:
        """Render the deliverable title this gap becomes on the roadmap."""
        return f"{self.capability_short}: {self.feature_summary.lower()}"


@dataclass
class Manifest:
    """The curated survey input: what we build and who else builds something comparable."""

    feature_catalog: dict[str, str] = field(default_factory=dict)
    capabilities: list[dict[str, Any]] = field(default_factory=list)

    def repos(self) -> list[str]:
        """List every alternative repository named anywhere in the manifest, deduplicated."""
        seen: dict[str, None] = {}
        for capability in self.capabilities:
            for alternative in capability.get("alternatives", []):
                seen.setdefault(str(alternative["repo"]), None)
        return list(seen)


def load_manifest(path: Path) -> Manifest:
    """Read the curated capability manifest."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Manifest(
        feature_catalog=dict(raw.get("feature_catalog", {})),
        capabilities=list(raw.get("capabilities", [])),
    )






















def find_gaps(manifest: Manifest) -> list[Gap]:
    """Return every feature a cited alternative has that the matching capability lacks.

    Reads `has:` entries only. A feature nobody recorded evidence for is unknown, and an
    unknown is not a gap — it is a question, and scheduling questions as work is how a
    loop manufactures a backlog out of its own ignorance.
    """
    gaps = [
        gap
        for capability in manifest.capabilities
        for gap in _capability_gaps(manifest, capability)
    ]
    return sorted(gaps, key=lambda g: (-len(g.holders), g.capability, g.feature))


def _capability_gaps(manifest: Manifest, capability: dict[str, Any]) -> list[Gap]:
    """Return the cited features one capability is missing."""
    ours = set(capability.get("ours", []))
    return [
        _build_gap(manifest, capability, feature, holders)
        for feature, holders in _claimed_features(capability).items()
        if feature not in ours
    ]


def _claimed_features(capability: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """Map each cited feature to the (repo, evidence) pairs claiming it, in manifest order."""
    claims: dict[str, list[tuple[str, str]]] = {}
    for alternative in capability.get("alternatives", []):
        repo = str(alternative["repo"])
        for feature, evidence in (alternative.get("has") or {}).items():
            claims.setdefault(str(feature), []).append((repo, str(evidence)))
    return claims


def _build_gap(
    manifest: Manifest, capability: dict[str, Any], feature: str, holders: list[tuple[str, str]]
) -> Gap:
    """Assemble one gap with the citations that make it falsifiable."""
    return Gap(
        capability=str(capability["key"]),
        capability_title=str(capability["title"]),
        capability_short=str(capability.get("short") or capability["title"]),
        feature=feature,
        feature_summary=manifest.feature_catalog.get(feature, feature),
        holders=tuple(repo for repo, _ in holders),
        evidence=tuple(f"{repo}: {text}" for repo, text in holders),
    )


# --- Fetching -------------------------------------------------------------------------

# --- Reporting ------------------------------------------------------------------------


def _maturity_table(manifest: Manifest, snapshot: dict[str, RepoFacts]) -> list[str]:
    """Render the maturity ranking across every alternative in the manifest."""
    rows = ["", "## Maturity of comparable projects", ""]
    rows.append("| Project | Score | Band | Stars | Last push | Versions | License |")
    rows.append("|---|---:|---|---:|---|---:|---|")
    assessments = [
        (assess_maturity(snapshot[repo]), snapshot[repo])
        for repo in manifest.repos()
        if repo in snapshot
    ]
    for maturity, facts in sorted(assessments, key=lambda pair: -pair[0].score):
        rows.append(
            f"| `{facts.full_name}` | {maturity.score} | {maturity.band} | {facts.stars} "
            f"| {facts.pushed_at[:10] or '—'} | {facts.releases} ({facts.release_source}) "
            f"| {facts.license or '—'} |"
        )
    return rows


def _comparison_tables(manifest: Manifest, snapshot: dict[str, RepoFacts]) -> Iterator[str]:
    """Render one feature matrix per capability, marking unknowns as unknown."""
    for capability in manifest.capabilities:
        yield ""
        yield f"## {capability['title']}"
        yield ""
        yield f"Ours: [`{capability['path']}`](../../{capability['path']})"
        yield ""
        features = sorted(_matrix_features(capability))
        alternatives = capability.get("alternatives", [])
        header = " | ".join(f"`{a['repo'].split('/')[-1]}`" for a in alternatives)
        yield f"| Feature | vibes | {header} |"
        yield "|---|---|" + "---|" * len(alternatives)
        for feature in features:
            yield _comparison_row(manifest, capability, feature, alternatives)
        yield ""
        yield _snapshot_note(alternatives, snapshot)


def _matrix_features(capability: dict[str, Any]) -> set[str]:
    """Return every feature the matrix should show for one capability.

    Includes features recorded in `lacks:`. Somebody checked and found them absent, and a
    matrix that showed only what exists would collect that work and never display it.
    """
    features = set(capability.get("ours", [])) | set(_claimed_features(capability))
    for alternative in capability.get("alternatives", []):
        features |= set(alternative.get("lacks") or [])
    return features


def _comparison_row(
    manifest: Manifest,
    capability: dict[str, Any],
    feature: str,
    alternatives: Sequence[dict[str, Any]],
) -> str:
    """Render one feature row: ours, then each alternative's cited position."""
    ours = "✅" if feature in set(capability.get("ours", [])) else "—"
    cells = [_cell(alternative, feature) for alternative in alternatives]
    summary = manifest.feature_catalog.get(feature, feature)
    return f"| **{feature}** — {summary} | {ours} | " + " | ".join(cells) + " |"


def _cell(alternative: dict[str, Any], feature: str) -> str:
    """Render one comparison cell, distinguishing a cited yes from an unassessed unknown."""
    if feature in (alternative.get("has") or {}):
        return "✅"
    if feature in (alternative.get("lacks") or []):
        return "—"
    return "?"


def _snapshot_note(alternatives: Sequence[dict[str, Any]], snapshot: dict[str, RepoFacts]) -> str:
    """Note any alternative missing from the snapshot, rather than rendering it as absent."""
    missing = [a["repo"] for a in alternatives if a["repo"] not in snapshot]
    if not missing:
        return "Legend: ✅ cited capability · — assessed absent · ? not assessed."
    return (
        "Legend: ✅ cited capability · — assessed absent · ? not assessed. "
        f"No snapshot facts for: {', '.join(missing)} — run `refresh`."
    )


def render_report(manifest: Manifest, snapshot: dict[str, RepoFacts]) -> str:
    """Render the full survey: maturity ranking, feature matrices, and the gap list."""
    fetched = next((f.fetched_at for f in snapshot.values() if f.fetched_at), "never")
    lines = [
        "# Landscape Survey",
        "",
        "Generated by [`tools/landscape_survey.py`](../../tools/landscape_survey.py). "
        "Do not edit by hand — edit [`capabilities.yaml`](./capabilities.yaml) and regenerate.",
        "",
        f"Facts fetched: `{fetched}`. Maturity is scored against that moment, not today, "
        "so re-running this report without refreshing produces an identical file.",
    ]
    lines += _maturity_table(manifest, snapshot)
    lines += list(_comparison_tables(manifest, snapshot))
    lines += _gap_section(find_gaps(manifest))
    return "\n".join(lines) + "\n"


def _gap_section(gaps: Sequence[Gap]) -> list[str]:
    """Render the gaps, each carrying the citation that would falsify it."""
    lines = ["", "## Capability gaps", ""]
    if not gaps:
        lines.append("No cited capability is missing from this repository.")
        return lines
    lines.append(
        f"{len(gaps)} feature(s) that a cited alternative has and the matching capability "
        "here does not. Each carries its evidence so it can be checked rather than believed."
    )
    lines.append("")
    for gap in gaps:
        lines.append(f"- **{gap.roadmap_title()}**")
        lines.append(f"  - Held by: {', '.join(f'`{h}`' for h in gap.holders)}")
        for citation in gap.evidence:
            lines.append(f"  - Evidence: {citation}")
    return lines


# --- Roadmap emission -----------------------------------------------------------------

GAP_MARKER: Final[str] = "landscape-gap"


def gap_marker(gap: Gap) -> str:
    """Return the stable identity written into the roadmap entry for this gap.

    Idempotency keys off this marker rather than the title, so re-wording a deliverable by
    hand does not make the next survey add it a second time.
    """
    return f"{GAP_MARKER}:{gap.capability}/{gap.feature}"


def gap_proposal(gap: Gap, priority: str = DEFAULT_PRIORITY) -> Proposal:
    """Turn one gap into a roadmap proposal, carrying the citation that would falsify it."""
    holders = ", ".join(f"`{h}`" for h in gap.holders)
    return Proposal(
        marker=gap_marker(gap),
        title=gap.roadmap_title(),
        context=(
            f"{gap.feature_summary}. Present in {holders}; absent from this repository's "
            f"{gap.capability_title.lower()}."
        ),
        evidence=gap.evidence[0],
        priority=priority,
    )


def render_roadmap_entry(gap: Gap, priority: str = DEFAULT_PRIORITY) -> list[str]:
    """Render one gap as roadmap markdown."""
    return gap_proposal(gap, priority).render()


def insert_gap_items(
    roadmap_text: str, gaps: Sequence[Gap], milestone: str = DEFAULT_MILESTONE
) -> tuple[str, list[str], list[str]]:
    """Insert roadmap entries for gaps not already tracked, returning (text, added, skipped)."""
    return insert_proposals(roadmap_text, [gap_proposal(gap) for gap in gaps], milestone)


# --- CLI ------------------------------------------------------------------------------


def _handle_refresh(args: argparse.Namespace) -> int:
    """Fetch fresh facts for every repository in the manifest and rewrite the snapshot."""
    manifest = load_manifest(args.manifest)
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    facts = [fetch_facts(repo, now) for repo in manifest.repos()]
    save_snapshot(args.snapshot, facts)
    failed = [f for f in facts if f.error]
    renamed = [f for f in facts if not f.error and f.renamed]
    print(f"Fetched {len(facts) - len(failed)}/{len(facts)} repositories into {args.snapshot}")
    for fact in renamed:
        print(f"  renamed: {fact.requested} -> {fact.full_name}")
    for fact in failed:
        print(f"  FAILED: {fact.requested}: {fact.error}", file=sys.stderr)
    return 1 if failed else 0


def _handle_report(args: argparse.Namespace) -> int:
    """Render the survey, to stdout or to a file."""
    manifest = load_manifest(args.manifest)
    report = render_report(manifest, load_snapshot(args.snapshot))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"Wrote {args.out}")
        return 0
    print(report, end="")
    return 0


def _handle_gaps(args: argparse.Namespace) -> int:
    """List capability gaps, as text or JSON."""
    gaps = find_gaps(load_manifest(args.manifest))
    if args.json:
        print(json.dumps([asdict(gap) for gap in gaps], indent=2))
        return 0
    print(f"{len(gaps)} capability gap(s):")
    for gap in gaps:
        print(f"  {gap.roadmap_title()}")
        print(f"      held by: {', '.join(gap.holders)}")
    return 0


def _handle_roadmap(args: argparse.Namespace) -> int:
    """Propose roadmap deliverables for the gaps, writing only when asked."""
    gaps = find_gaps(load_manifest(args.manifest))
    if args.top:
        gaps = gaps[: args.top]
    original = args.roadmap.read_text(encoding="utf-8")
    updated, added, skipped = insert_gap_items(original, gaps, args.milestone)
    print(f"{len(added)} new, {len(skipped)} already tracked.")
    for title in added:
        print(f"  + {title}")
    if not args.write:
        print("\nDry run. Pass --write to apply.")
        return 0
    args.roadmap.write_text(updated, encoding="utf-8")
    print(f"\nWrote {len(added)} item(s) to {args.roadmap}")
    return 0


def _handle_discover(args: argparse.Namespace) -> int:
    """Search GitHub for projects that might belong in the manifest."""
    try:
        candidates = discover_candidates(args.query, args.limit)
    except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as err:
        print(f"Search failed: {err}", file=sys.stderr)
        return 1
    known = set(load_manifest(args.manifest).repos())
    print(f"{len(candidates)} candidate(s) for manual curation:")
    for item in candidates:
        mark = "known" if item["repo"] in known else "  new"
        print(f"  [{mark}] {item['repo']:<40} ★{item['stars']:<7} {item['description']}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser for the landscape survey."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("refresh", help="Fetch repository facts from GitHub (requires network)")

    report = sub.add_parser("report", help="Render the survey from cached facts")
    report.add_argument("--out", type=Path, help="Write to this file instead of stdout")

    gaps = sub.add_parser("gaps", help="List capability gaps")
    gaps.add_argument("--json", action="store_true")

    roadmap = sub.add_parser("roadmap", help="Propose roadmap deliverables for the gaps")
    roadmap.add_argument("--roadmap", type=Path, default=DEFAULT_ROADMAP)
    roadmap.add_argument("--milestone", default=DEFAULT_MILESTONE)
    roadmap.add_argument("--top", type=int, help="Only the N best-evidenced gaps")
    roadmap.add_argument("--write", action="store_true", help="Apply; otherwise dry-run")

    discover = sub.add_parser("discover", help="Search GitHub for candidate projects")
    discover.add_argument("query", help="GitHub search query")
    discover.add_argument("--limit", type=int, default=10)
    return parser


_COMMANDS: Final[dict[str, Any]] = {
    "refresh": _handle_refresh,
    "report": _handle_report,
    "gaps": _handle_gaps,
    "roadmap": _handle_roadmap,
    "discover": _handle_discover,
}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the landscape survey."""
    args = build_arg_parser().parse_args(argv)
    if not args.manifest.is_file():
        print(f"Manifest not found: {args.manifest}", file=sys.stderr)
        return 1
    return int(_COMMANDS[args.command](args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
