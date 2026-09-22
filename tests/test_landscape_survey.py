"""Tests for the landscape survey: maturity scoring, gap detection, and roadmap emission.

The property worth defending is that the survey cannot invent work. A feature nobody
recorded evidence for must never become a roadmap item, and an item already tracked must
never be added twice.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from landscape_survey import (
    DEFAULT_MILESTONE,
    Manifest,
    RepoFacts,
    assess_maturity,
    find_gaps,
    gap_marker,
    insert_gap_items,
    load_manifest,
    load_snapshot,
    render_roadmap_entry,
    save_snapshot,
)
from landscape_survey import (
    main as landscape_main,
)
from roadmap_ingest import _CONTEXT_RE, _OPEN_ITEM_RE, _build_item

REFERENCE = datetime(2026, 1, 1, tzinfo=UTC)

MANIFEST_YAML = """
feature_catalog:
  trend_over_time: Tracks metric movement across git history
  sarif_output: Emits SARIF for code scanning
  nesting_depth: Flags deep block nesting

capabilities:
  - key: gating
    title: Mechanical invariant gate
    short: Gate
    path: tools/landscape_survey.py
    ours: [nesting_depth]
    alternatives:
      - repo: someone/tracker
        has:
          trend_over_time: "wily-style index across revisions"
          nesting_depth: "documented depth ceiling"
        lacks: [sarif_output]
      - repo: someone/other
        has:
          trend_over_time: "second independent citation"
"""


def _manifest(tmp_path: Path) -> Manifest:
    path = tmp_path / "capabilities.yaml"
    path.write_text(MANIFEST_YAML, encoding="utf-8")
    return load_manifest(path)


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


def test_a_gap_needs_a_cited_alternative_and_our_absence(tmp_path: Path) -> None:
    """Only a feature somebody cited, and we do not list, becomes a gap."""
    gaps = {gap.feature: gap for gap in find_gaps(_manifest(tmp_path))}
    assert (sorted(gaps), gaps["trend_over_time"].holders) == (
        ["trend_over_time"],
        ("someone/tracker", "someone/other"),
    )


def test_an_unassessed_feature_never_becomes_work(tmp_path: Path) -> None:
    """`sarif_output` appears only in the catalog and a `lacks:` list, so it is not a gap.

    This is the property that stops a survey manufacturing a backlog out of its own
    ignorance: silence about a feature is a question, not a finding.
    """
    assert [gap.feature for gap in find_gaps(_manifest(tmp_path))] == ["trend_over_time"]


def test_a_gap_carries_every_citation_that_would_falsify_it(tmp_path: Path) -> None:
    """Two independent claims must both survive into the evidence list."""
    gap = find_gaps(_manifest(tmp_path))[0]
    assert (len(gap.evidence), gap.evidence[0].startswith("someone/tracker:")) == (2, True)


def test_an_emitted_roadmap_entry_parses_as_a_roadmap_deliverable(tmp_path: Path) -> None:
    """The survey writes the grammar `roadmap_ingest.py` reads, or the item is lost silently."""
    lines = render_roadmap_entry(find_gaps(_manifest(tmp_path))[0])
    item_match = _OPEN_ITEM_RE.match(lines[0])
    assert item_match is not None
    item = _build_item(item_match, "v0.5.0", 1, {})
    assert (item.priority, item.blocked_reason, _CONTEXT_RE.match(lines[1]) is not None) == (
        "P2_MEDIUM",
        "",
        True,
    )


def test_roadmap_insertion_is_idempotent(tmp_path: Path) -> None:
    """Running the survey twice must not propose the same deliverable twice."""
    gaps = find_gaps(_manifest(tmp_path))
    roadmap = f"# Roadmap\n\n{DEFAULT_MILESTONE} Something (v0.5.0 - Planned)\n- [ ] **Existing**:\n\n---\n"
    once, added_first, _ = insert_gap_items(roadmap, gaps)
    twice, added_second, skipped = insert_gap_items(once, gaps)

    assert (len(added_first), added_second, len(skipped), once == twice) == (1, [], 1, True)


def test_an_item_already_tracked_by_hand_is_recognised(tmp_path: Path) -> None:
    """Identity is the survey marker, so re-wording a deliverable does not duplicate it."""
    gap = find_gaps(_manifest(tmp_path))[0]
    roadmap = (
        f"# Roadmap\n\n{DEFAULT_MILESTONE} Something (v0.5.0 - Planned)\n"
        f"- [x] **A totally different title**:\n  - *Survey*: `{gap_marker(gap)}`\n\n---\n"
    )
    _, added, skipped = insert_gap_items(roadmap, [gap])
    assert (added, len(skipped)) == ([], 1)


def test_insertion_names_the_milestone_it_could_not_find(tmp_path: Path) -> None:
    """A silent no-op here would lose every proposed deliverable without a word."""
    gaps = find_gaps(_manifest(tmp_path))
    try:
        insert_gap_items("# Roadmap\n\nNo milestones here.\n", gaps, "### Milestone 9:")
    except ValueError as err:
        assert "Milestone 9:" in str(err)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("expected a ValueError naming the missing milestone")


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


def test_cli_reports_gaps_as_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The JSON surface is what a downstream loop consumes; it must stay parseable."""
    manifest = tmp_path / "capabilities.yaml"
    manifest.write_text(MANIFEST_YAML, encoding="utf-8")
    exit_code = landscape_main(["--manifest", str(manifest), "gaps", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert (exit_code, [entry["feature"] for entry in payload]) == (0, ["trend_over_time"])


def test_cli_roadmap_is_a_dry_run_unless_told_otherwise(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Writing to the roadmap is a side effect on a human artefact, so it is opt-in."""
    manifest = tmp_path / "capabilities.yaml"
    manifest.write_text(MANIFEST_YAML, encoding="utf-8")
    roadmap = tmp_path / "ROADMAP.md"
    original = f"# Roadmap\n\n{DEFAULT_MILESTONE} Something (v0.5.0 - Planned)\n- [ ] **Existing**:\n\n---\n"
    roadmap.write_text(original, encoding="utf-8")

    exit_code = landscape_main(
        ["--manifest", str(manifest), "roadmap", "--roadmap", str(roadmap)]
    )
    capsys.readouterr()
    assert (exit_code, roadmap.read_text(encoding="utf-8")) == (0, original)


def test_cli_roadmap_writes_when_asked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With --write the deliverable lands under the requested milestone."""
    manifest = tmp_path / "capabilities.yaml"
    manifest.write_text(MANIFEST_YAML, encoding="utf-8")
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(
        f"# Roadmap\n\n{DEFAULT_MILESTONE} Something (v0.5.0 - Planned)\n- [ ] **Existing**:\n\n---\n",
        encoding="utf-8",
    )

    exit_code = landscape_main(
        ["--manifest", str(manifest), "roadmap", "--roadmap", str(roadmap), "--write"]
    )
    capsys.readouterr()
    written = roadmap.read_text(encoding="utf-8")
    assert (exit_code, "landscape-gap:gating/trend_over_time" in written) == (0, True)


def test_cli_rejects_a_missing_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A survey with no manifest must fail loudly rather than report zero gaps."""
    exit_code = landscape_main(["--manifest", str(tmp_path / "nope.yaml"), "gaps"])
    assert (exit_code, "not found" in capsys.readouterr().err) == (1, True)


def test_a_release_history_falls_back_to_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """A project that tags without publishing Releases still has a release history."""
    import landscape_survey

    responses = {
        "/repos/o/n/releases?per_page=100": [],
        "/repos/o/n/tags?per_page=100": [{"commit": {"sha": "abc"}}] * 80,
        "/repos/o/n/commits/abc": {"commit": {"committer": {"date": "2026-01-02T00:00:00Z"}}},
    }
    monkeypatch.setattr(landscape_survey, "_gh_json", lambda ep, jq=None: responses[ep])
    assert landscape_survey._fetch_release_history("o/n") == (80, "2026-01-02T00:00:00Z", "tags")


def test_release_objects_win_over_tags_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Published Releases carry their own timestamp and need no commit lookup."""
    import landscape_survey

    monkeypatch.setattr(
        landscape_survey,
        "_gh_json",
        lambda ep, jq=None: [{"published_at": "2026-02-02T00:00:00Z"}],
    )
    assert landscape_survey._fetch_release_history("o/n") == (1, "2026-02-02T00:00:00Z", "releases")


def test_an_unreachable_repository_is_recorded_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silently omitting a repository would shrink the comparison without saying so."""
    import landscape_survey

    def explode(endpoint: str, jq: str | None = None) -> object:
        raise RuntimeError("404 Not Found")

    monkeypatch.setattr(landscape_survey, "_gh_json", explode)
    facts = landscape_survey.fetch_facts("gone/away", "2026-01-01T00:00:00Z")
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


def test_the_report_marks_unassessed_features_distinctly(tmp_path: Path) -> None:
    """A `?` and a `—` must not collapse: unknown and absent are different claims."""
    manifest = _manifest(tmp_path)
    report = __import__("landscape_survey").render_report(manifest, {})
    matrix_line = next(line for line in report.splitlines() if "sarif_output" in line)
    assert ("| ? |" in matrix_line, "| — |" in matrix_line) == (True, True)


def test_the_report_names_alternatives_missing_from_the_snapshot(tmp_path: Path) -> None:
    """Rendering a project with no facts as blank would read as absent rather than unfetched."""
    report = __import__("landscape_survey").render_report(_manifest(tmp_path), {})
    assert "No snapshot facts for: someone/tracker, someone/other" in report


def test_the_report_ranks_maturity_and_shows_the_version_source(tmp_path: Path) -> None:
    """The maturity table is the survey's answer to 'can I depend on this'."""
    snapshot = {
        "someone/tracker": _facts(requested="someone/tracker", full_name="someone/tracker"),
        "someone/other": _facts(
            requested="someone/other", full_name="someone/other", release_source="tags"
        ),
    }
    report = __import__("landscape_survey").render_report(_manifest(tmp_path), snapshot)
    assert ("(tags)" in report, "(releases)" in report, "Mature" in report) == (True, True, True)


def test_cli_report_writes_to_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The generated report is a committed artefact, so writing it is a first-class path."""
    manifest = tmp_path / "capabilities.yaml"
    manifest.write_text(MANIFEST_YAML, encoding="utf-8")
    out = tmp_path / "nested" / "SURVEY.md"
    exit_code = landscape_main(["--manifest", str(manifest), "report", "--out", str(out)])
    capsys.readouterr()
    assert (exit_code, out.is_file(), "Landscape Survey" in out.read_text(encoding="utf-8")) == (
        0,
        True,
        True,
    )


def test_cli_refresh_reports_failures_without_writing_a_short_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every requested repository stays in the snapshot, carrying its error if it failed."""
    import landscape_survey

    manifest = tmp_path / "capabilities.yaml"
    manifest.write_text(MANIFEST_YAML, encoding="utf-8")
    snapshot = tmp_path / "snapshot.json"
    monkeypatch.setattr(
        landscape_survey,
        "fetch_facts",
        lambda repo, now: RepoFacts(requested=repo, full_name=repo, fetched_at=now, error="boom"),
    )
    exit_code = landscape_main(
        ["--manifest", str(manifest), "--snapshot", str(snapshot), "refresh"]
    )
    capsys.readouterr()
    assert (exit_code, sorted(load_snapshot(snapshot))) == (1, ["someone/other", "someone/tracker"])


def test_cli_discover_marks_projects_already_curated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Search proposes candidates; the manifest decides. Known entries are labelled as such."""
    import landscape_survey

    manifest = tmp_path / "capabilities.yaml"
    manifest.write_text(MANIFEST_YAML, encoding="utf-8")
    monkeypatch.setattr(
        landscape_survey,
        "discover_candidates",
        lambda query, limit: [
            {"repo": "someone/tracker", "stars": 10, "description": "already curated"},
            {"repo": "fresh/find", "stars": 20, "description": "not yet curated"},
        ],
    )
    exit_code = landscape_main(["--manifest", str(manifest), "discover", "anything"])
    out = capsys.readouterr().out
    assert (exit_code, "[known] someone/tracker" in out, "[  new] fresh/find" in out) == (
        0,
        True,
        True,
    )
