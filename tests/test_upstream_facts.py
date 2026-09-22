"""Tests for upstream repository facts and the maturity they imply.

Shared by the landscape survey and the supply chain audit, so a regression here changes
what both instruments believe about risk.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import upstream_facts
from upstream_facts import RepoFacts, assess_maturity, load_snapshot, save_snapshot

REFERENCE = datetime(2026, 1, 1, tzinfo=UTC)


def _facts(**overrides: object) -> RepoFacts:
    base = {
        "requested": "owner/name",
        "full_name": "owner/name",
        "stars": 1000,
        "created_at": "2018-01-01T00:00:00Z",
        "pushed_at": "2025-12-25T00:00:00Z",
        "releases": 10,
        "latest_release_at": "2025-12-01T00:00:00Z",
        "release_source": "releases",
        "license": "MIT",
        "fetched_at": "2026-01-01T00:00:00Z",
    }
    return RepoFacts(**{**base, **overrides})


def test_maturity_is_scored_against_the_snapshot_not_the_wall_clock() -> None:
    """The same snapshot must produce the same score whenever it is rendered."""
    facts = _facts()
    first = assess_maturity(facts)
    second = assess_maturity(facts, reference=REFERENCE)
    assert (first.score, first.band) == (second.score, second.band)


def test_an_archived_repository_scores_zero_governance() -> None:
    """Archived is the one signal that should dominate: the project has stopped."""
    live = assess_maturity(_facts(), reference=REFERENCE)
    archived = assess_maturity(_facts(archived=True), reference=REFERENCE)
    assert (archived.signals["governance"], archived.score < live.score) == (0.0, True)


def test_a_dormant_repository_ranks_below_an_active_one() -> None:
    """Years without a push is what the score exists to surface."""
    active = assess_maturity(_facts(), reference=REFERENCE)
    dormant = assess_maturity(
        _facts(pushed_at="2023-01-01T00:00:00Z", latest_release_at="2023-01-01T00:00:00Z"),
        reference=REFERENCE,
    )
    assert (dormant.score < active.score, dormant.signals["recent_activity"] < 0.5) == (True, True)


def test_a_repository_that_tags_without_releases_is_not_penalised() -> None:
    """Using the GitHub Releases feature is a publishing preference, not a maturity signal."""
    released = assess_maturity(_facts(release_source="releases"), reference=REFERENCE)
    tagged = assess_maturity(_facts(release_source="tags"), reference=REFERENCE)
    assert released.score == tagged.score


def test_a_failed_fetch_scores_unknown_rather_than_zero_maturity() -> None:
    """An unreachable repository is unassessed, which is not the same as immature."""
    assessment = assess_maturity(_facts(error="404"), reference=REFERENCE)
    assert (assessment.band, assessment.signals) == ("Unknown", {})


def test_snapshot_round_trips_and_sorts_stably(tmp_path: Path) -> None:
    """A re-fetch must produce a readable diff, which means a stable order."""
    path = tmp_path / "snapshot.json"
    save_snapshot(path, [_facts(requested="z/z", full_name="z/z"), _facts(requested="a/a")])
    payload = json.loads(path.read_text(encoding="utf-8"))
    restored = load_snapshot(path)
    assert ([entry["requested"] for entry in payload["repositories"]], len(restored)) == (
        ["a/a", "z/z"],
        2,
    )


def test_a_release_history_falls_back_to_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """A project that tags without publishing Releases still has a release history."""
    responses = {
        "/repos/o/n/releases?per_page=100": [],
        "/repos/o/n/tags?per_page=100": [{"commit": {"sha": "abc"}}] * 80,
        "/repos/o/n/commits/abc": {"commit": {"committer": {"date": "2026-01-02T00:00:00Z"}}},
    }
    monkeypatch.setattr(upstream_facts, "gh_json", lambda ep, jq=None: responses[ep])
    assert upstream_facts._fetch_release_history("o/n") == (80, "2026-01-02T00:00:00Z", "tags")


def test_release_objects_win_over_tags_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Published Releases carry their own timestamp and need no commit lookup."""
    monkeypatch.setattr(
        upstream_facts,
        "gh_json",
        lambda ep, jq=None: [{"published_at": "2026-02-02T00:00:00Z"}],
    )
    assert upstream_facts._fetch_release_history("o/n") == (1, "2026-02-02T00:00:00Z", "releases")


def test_an_unreachable_repository_is_recorded_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silently omitting a repository would shrink the comparison without saying so."""
    def explode(endpoint: str, jq: str | None = None) -> object:
        raise RuntimeError("404 Not Found")

    monkeypatch.setattr(upstream_facts, "gh_json", explode)
    facts = upstream_facts.fetch_facts("gone/away", "2026-01-01T00:00:00Z")
    assert (facts.requested, facts.error, assess_maturity(facts).band) == (
        "gone/away",
        "404 Not Found",
        "Unknown",
    )


def test_a_redirected_repository_is_flagged_as_renamed() -> None:
    """A rename is a real signal about a dependency and must not pass unremarked."""
    moved = _facts(requested="old/name", full_name="new/name")
    same = _facts(requested="owner/name", full_name="owner/name")
    assert (moved.renamed, same.renamed) == (True, False)
