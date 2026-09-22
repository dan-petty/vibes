#!/usr/bin/env python3
"""Mechanical facts about an upstream repository, and the maturity they imply.

Shared by every instrument that asks "can I depend on this": the landscape survey, which
compares this repository against its field, and the supply chain audit, which asks the
same question of the things this repository already depends on. One definition, because
two instruments that score maturity differently cannot be reconciled about risk.

Facts are cached in a committed snapshot and scored against their own `fetched_at`, never
the wall clock, so a report re-renders byte-identically and a diff means the world moved.
"""

from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

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
GH_TIMEOUT_SECONDS: Final[int] = 30


def gh_json(endpoint: str, jq: str | None = None) -> Any:
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
    output = result.stdout.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        # A jq expression selecting a scalar makes `gh` print the bare value, so `-q .sha`
        # yields `3d3c42e…` rather than `"3d3c42e…"`. Parsing that as JSON fails, and the
        # first caller to select a scalar met a JSONDecodeError instead of their string.
        return output


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


def _fetch_release_history(repo: str) -> tuple[int, str | None, str]:
    """Return (count, latest timestamp, source) for a project's published versions.

    Counting GitHub Release objects alone measures whether a project uses one optional
    GitHub feature, not whether it ships. `markdownlint` had 80 tags and zero Releases and
    was being docked a quarter of its maturity score for a publishing preference. Tags are
    the fallback, and the source is recorded so the report never implies more than it knows.
    """
    releases = gh_json(f"/repos/{repo}/releases?per_page=100") or []
    if releases:
        return len(releases), releases[0].get("published_at"), "releases"
    tags = gh_json(f"/repos/{repo}/tags?per_page=100") or []
    if not tags:
        return 0, None, "none"
    return len(tags), _tag_commit_date(repo, tags[0]), "tags"


def _tag_commit_date(repo: str, tag: dict[str, Any]) -> str | None:
    """Return the commit date behind a tag, which carries no timestamp of its own."""
    sha = (tag.get("commit") or {}).get("sha")
    if not sha:
        return None
    commit = gh_json(f"/repos/{repo}/commits/{sha}") or {}
    committer = (commit.get("commit") or {}).get("committer") or {}
    return committer.get("date")


def fetch_facts(repo: str, now: str) -> RepoFacts:
    """Fetch one repository's mechanical signals, recording failure rather than dropping it.

    A repository that cannot be fetched stays in the snapshot carrying its error. Silently
    omitting it would shrink the comparison without saying so, which is the shape of every
    gate in this repository that once reported success on input it never read.
    """
    try:
        data = gh_json(f"/repos/{repo}")
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
    items = gh_json(endpoint, ".items") or []
    return [
        {
            "repo": item["full_name"],
            "stars": item["stargazers_count"],
            "description": (item.get("description") or "")[:110],
        }
        for item in items
    ]
