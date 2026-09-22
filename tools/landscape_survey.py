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
import math
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import yaml

DEFAULT_MANIFEST: Final[Path] = Path("docs/landscape/capabilities.yaml")
DEFAULT_SNAPSHOT: Final[Path] = Path("docs/landscape/snapshot.json")
DEFAULT_ROADMAP: Final[Path] = Path("docs/ROADMAP.md")

# Weights sum to 1.0. Adoption is deliberately the smallest: stars measure how many people
# heard about a project, which correlates with maturity only loosely and lags it by years.
# Release discipline and recent activity are what actually predict whether a dependency
# will still be maintained when it breaks.
SIGNAL_WEIGHTS: Final[dict[str, float]] = {
    "recent_activity": 0.30,
    "release_discipline": 0.25,
    "governance": 0.20,
    "longevity": 0.15,
    "adoption": 0.10,
}

# Score floor for each band, highest first.
MATURITY_BANDS: Final[tuple[tuple[float, str], ...]] = (
    (75.0, "Mature"),
    (55.0, "Established"),
    (35.0, "Active"),
    (15.0, "Emerging"),
    (0.0, "Dormant"),
)

STALE_AFTER_DAYS: Final[float] = 730.0
RELEASE_STALE_AFTER_DAYS: Final[float] = 540.0
MATURE_AGE_YEARS: Final[float] = 5.0
ADOPTION_SATURATION_STARS: Final[int] = 20000


@dataclass(frozen=True)
class RepoFacts:
    """Mechanical signals about one repository, as the API reported them at `fetched_at`."""

    requested: str
    full_name: str
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    created_at: str = ""
    pushed_at: str = ""
    archived: bool = False
    license: str | None = None
    releases: int = 0
    latest_release_at: str | None = None
    release_source: str = "none"
    topics: tuple[str, ...] = ()
    fetched_at: str = ""
    error: str | None = None

    @property
    def renamed(self) -> bool:
        """Report whether the API redirected to a different name than the manifest asked for."""
        return self.full_name.lower() != self.requested.lower()


@dataclass(frozen=True)
class Maturity:
    """A maturity assessment, with the component signals that produced it."""

    repo: str
    score: float
    band: str
    signals: dict[str, float]


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


def load_snapshot(path: Path) -> dict[str, RepoFacts]:
    """Read cached repository facts, returning an empty snapshot when none exists yet."""
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(entry["requested"]): RepoFacts(
            **{**entry, "topics": tuple(entry.get("topics", []))}
        )
        for entry in payload.get("repositories", [])
    }


def save_snapshot(path: Path, facts: Sequence[RepoFacts]) -> None:
    """Write repository facts in a stable order so a re-fetch produces a readable diff."""
    payload = {
        "schema": 1,
        "repositories": [
            {**asdict(fact), "topics": list(fact.topics)}
            for fact in sorted(facts, key=lambda f: f.requested.lower())
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _parse_time(value: str | None) -> datetime | None:
    """Parse a GitHub timestamp, tolerating the trailing Z and an absent value."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _decay(then: str | None, reference: datetime, horizon_days: float) -> float:
    """Return 1.0 for something that happened just now, falling linearly to 0 at the horizon."""
    moment = _parse_time(then)
    if moment is None:
        return 0.0
    age_days = (reference - moment).total_seconds() / 86400.0
    return max(0.0, min(1.0, 1.0 - age_days / horizon_days))


def _governance_signal(facts: RepoFacts) -> float:
    """Score the two governance facts a consumer can check before depending on something."""
    if facts.archived:
        return 0.0
    return 1.0 if facts.license else 0.4


def _longevity_signal(facts: RepoFacts, reference: datetime) -> float:
    """Score how long the project has existed, saturating at MATURE_AGE_YEARS."""
    created = _parse_time(facts.created_at)
    if created is None:
        return 0.0
    years = (reference - created).total_seconds() / (86400.0 * 365.25)
    return max(0.0, min(1.0, years / MATURE_AGE_YEARS))


def _adoption_signal(facts: RepoFacts) -> float:
    """Score adoption on a log scale: the first thousand stars say far more than the tenth."""
    if facts.stars <= 0:
        return 0.0
    return min(1.0, math.log10(facts.stars + 1) / math.log10(ADOPTION_SATURATION_STARS))


def _release_signal(facts: RepoFacts, reference: datetime) -> float:
    """Score release discipline: tagged releases at all, and one recently."""
    if facts.releases <= 0:
        return 0.0
    return 0.4 + 0.6 * _decay(facts.latest_release_at, reference, RELEASE_STALE_AFTER_DAYS)


def assess_maturity(facts: RepoFacts, reference: datetime | None = None) -> Maturity:
    """Score one repository from mechanical signals, relative to when the facts were fetched.

    Scoring against `fetched_at` rather than the wall clock is what makes a report
    reproducible: the same snapshot yields the same numbers tomorrow, so a changed report
    means the world moved rather than the calendar.
    """
    moment = reference or _parse_time(facts.fetched_at) or datetime.now(UTC)
    if facts.error:
        return Maturity(repo=facts.requested, score=0.0, band="Unknown", signals={})
    signals = {
        "recent_activity": _decay(facts.pushed_at, moment, STALE_AFTER_DAYS),
        "release_discipline": _release_signal(facts, moment),
        "governance": _governance_signal(facts),
        "longevity": _longevity_signal(facts, moment),
        "adoption": _adoption_signal(facts),
    }
    score = sum(SIGNAL_WEIGHTS[name] * value for name, value in signals.items()) * 100.0
    return Maturity(
        repo=facts.full_name or facts.requested,
        score=round(score, 1),
        band=_band_for(score),
        signals={name: round(value, 3) for name, value in signals.items()},
    )


def _band_for(score: float) -> str:
    """Map a score onto its maturity band."""
    for floor, name in MATURITY_BANDS:
        if score >= floor:
            return name
    return MATURITY_BANDS[-1][1]


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

GH_TIMEOUT_SECONDS: Final[int] = 30


def _gh_json(endpoint: str, jq: str | None = None) -> Any:
    """Call the GitHub API through the authenticated `gh` CLI, returning parsed JSON.

    Shelling out to `gh` rather than holding a token: the credential stays in the tool the
    user already authenticated, and this module never reads, stores or logs one.
    """
    command = ["gh", "api", endpoint] + (["-q", jq] if jq else [])
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:200] or f"gh api {endpoint} failed")
    return json.loads(result.stdout) if result.stdout.strip() else None


def _fetch_release_history(repo: str) -> tuple[int, str | None, str]:
    """Return (count, latest timestamp, source) for a project's published versions.

    Counting GitHub Release objects alone measures whether a project uses one optional
    GitHub feature, not whether it ships. `markdownlint` had 80 tags and zero Releases and
    was being docked a quarter of its maturity score for a publishing preference. Tags are
    the fallback, and the source is recorded so the report never implies more than it knows.
    """
    releases = _gh_json(f"/repos/{repo}/releases?per_page=100") or []
    if releases:
        return len(releases), releases[0].get("published_at"), "releases"
    tags = _gh_json(f"/repos/{repo}/tags?per_page=100") or []
    if not tags:
        return 0, None, "none"
    return len(tags), _tag_commit_date(repo, tags[0]), "tags"


def _tag_commit_date(repo: str, tag: dict[str, Any]) -> str | None:
    """Return the commit date behind a tag, which carries no timestamp of its own."""
    sha = (tag.get("commit") or {}).get("sha")
    if not sha:
        return None
    commit = _gh_json(f"/repos/{repo}/commits/{sha}") or {}
    committer = (commit.get("commit") or {}).get("committer") or {}
    return committer.get("date")


def fetch_facts(repo: str, now: str) -> RepoFacts:
    """Fetch one repository's mechanical signals, recording failure rather than dropping it.

    A repository that cannot be fetched stays in the snapshot carrying its error. Silently
    omitting it would shrink the comparison without saying so, which is the shape of every
    gate in this repository that once reported success on input it never read.
    """
    try:
        data = _gh_json(f"/repos/{repo}")
        release_count, latest_release, source = _fetch_release_history(repo)
    except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as err:
        return RepoFacts(requested=repo, full_name=repo, fetched_at=now, error=str(err)[:200])
    return RepoFacts(
        requested=repo,
        full_name=str(data.get("full_name", repo)),
        stars=int(data.get("stargazers_count", 0)),
        forks=int(data.get("forks_count", 0)),
        open_issues=int(data.get("open_issues_count", 0)),
        created_at=str(data.get("created_at") or ""),
        pushed_at=str(data.get("pushed_at") or ""),
        archived=bool(data.get("archived", False)),
        license=(data.get("license") or {}).get("spdx_id"),
        releases=release_count,
        latest_release_at=latest_release,
        release_source=source,
        topics=tuple(data.get("topics", [])),
        fetched_at=now,
    )


def discover_candidates(query: str, limit: int) -> list[dict[str, Any]]:
    """Search GitHub for projects that might belong in the manifest.

    Returns suggestions for a human to curate, never manifest entries. Search ranking is
    not evidence of comparability, and a survey that adopted its own search results would
    be citing itself.
    """
    endpoint = f"/search/repositories?q={query}&sort=stars&order=desc&per_page={limit}"
    items = _gh_json(endpoint, ".items") or []
    return [
        {
            "repo": item["full_name"],
            "stars": item["stargazers_count"],
            "description": (item.get("description") or "")[:110],
        }
        for item in items
    ]


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
DEFAULT_MILESTONE: Final[str] = "### Milestone 5:"
DEFAULT_PRIORITY: Final[str] = "P2 - Medium"


def gap_marker(gap: Gap) -> str:
    """Return the stable identity written into the roadmap entry for this gap.

    Idempotency keys off this marker rather than the title, so re-wording a deliverable by
    hand does not make the next survey add it a second time.
    """
    return f"{GAP_MARKER}:{gap.capability}/{gap.feature}"


def render_roadmap_entry(gap: Gap, priority: str = DEFAULT_PRIORITY) -> list[str]:
    """Render one gap as a roadmap deliverable in the grammar `roadmap_ingest.py` parses.

    Value and effort are deliberately not invented. An item with no prioritization matrix
    row is ranked on defaults and says so downstream, which is the honest signal: a survey
    can establish that a capability exists elsewhere, never what it is worth here.
    """
    holders = ", ".join(f"`{h}`" for h in gap.holders)
    lines = [
        f"- [ ] **{gap.roadmap_title()} ({priority})**:",
        f"  - *Gap*: {gap.feature_summary}. Present in {holders}; absent from this repository's "
        f"{gap.capability_title.lower()}.",
        f"  - *Evidence*: {gap.evidence[0]}",
        f"  - *Survey*: `{gap_marker(gap)}` — regenerated by `tools/landscape_survey.py`; "
        "delete this bullet only with the item.",
    ]
    return lines


def insert_gap_items(
    roadmap_text: str, gaps: Sequence[Gap], milestone: str = DEFAULT_MILESTONE
) -> tuple[str, list[str], list[str]]:
    """Insert roadmap entries for gaps not already tracked, returning (text, added, skipped).

    An item already present — whether open, checked off, or hand-edited — is skipped. The
    survey proposes work once; what happens to it afterwards is the roadmap's business.
    """
    added, skipped = [], []
    pending: list[str] = []
    for gap in gaps:
        if gap_marker(gap) in roadmap_text:
            skipped.append(gap.roadmap_title())
            continue
        pending.extend(render_roadmap_entry(gap))
        added.append(gap.roadmap_title())
    if not pending:
        return roadmap_text, added, skipped
    return _splice_into_milestone(roadmap_text, pending, milestone), added, skipped


def _splice_into_milestone(roadmap_text: str, block: Sequence[str], milestone: str) -> str:
    """Place a block at the end of the named milestone section."""
    lines = roadmap_text.splitlines()
    start = _milestone_index(lines, milestone)
    end = _section_end(lines, start)
    return "\n".join(lines[:end] + list(block) + lines[end:]) + "\n"


def _milestone_index(lines: Sequence[str], milestone: str) -> int:
    """Return the index of the milestone heading, or raise naming what was searched for."""
    for index, line in enumerate(lines):
        if line.startswith(milestone):
            return index
    raise ValueError(f"No milestone heading starting with {milestone!r} in the roadmap")


def _section_end(lines: Sequence[str], start: int) -> int:
    """Return the index just past the last content line of the section beginning at `start`."""
    end = start + 1
    for index in range(start + 1, len(lines)):
        if lines[index].startswith(("### ", "## ", "---")):
            break
        if lines[index].strip():
            end = index + 1
    return end


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
