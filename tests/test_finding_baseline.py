"""Tests for accepting existing findings so a gate can be adopted before a codebase passes.

The property that matters most here is not that a baseline suppresses a finding — that is
the easy half — but that it stops suppressing one the moment the finding is fixed. A
suppression file that never shrinks goes on hiding a defect that was repaired and later
reintroduced, and the gate quietly stops covering code it used to cover.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from finding_baseline import (
    DEFAULT_BASELINE,
    SarifFinding,
    load_baseline,
    partition,
    prune,
    read_sarif,
    save_baseline,
)
from sarif_report import Result, Rule, Run, build_log


def _finding(digest: str = "aaaa", rule: str = "vibes/demo/rule") -> SarifFinding:
    """Build one finding with a chosen identity."""
    return SarifFinding(rule_id=rule, file_path="tools/demo.py", subject="demo", digest=digest)


def test_an_absent_baseline_accepts_nothing(tmp_path: Path) -> None:
    """A missing file means "report everything", which is the correct default for a new gate."""
    split = partition([_finding()], load_baseline(tmp_path / "nothing.json"))
    assert (len(split.new), len(split.known)) == (1, 0)


def test_a_recorded_finding_stops_being_reported(tmp_path: Path) -> None:
    """The adoption case: turn the gate on without fixing the whole codebase first."""
    path = tmp_path / "baseline.json"
    save_baseline(path, [_finding()])
    split = partition([_finding()], load_baseline(path))
    assert (len(split.new), len(split.known)) == (0, 1)


def test_a_finding_absent_from_the_baseline_is_still_reported(tmp_path: Path) -> None:
    """A baseline must accept what was there, never everything of that kind."""
    path = tmp_path / "baseline.json"
    save_baseline(path, [_finding(digest="old")])
    split = partition([_finding(digest="new")], load_baseline(path))
    assert (len(split.new), len(split.known)) == (1, 0)


def test_a_fixed_finding_is_reported_as_resolved(tmp_path: Path) -> None:
    """An entry nothing produces any more is the signal that the baseline can shrink."""
    path = tmp_path / "baseline.json"
    save_baseline(path, [_finding(digest="fixed")])
    split = partition([], load_baseline(path))
    assert (len(split.resolved), split.is_stale()) == (1, True)


def test_pruning_stops_a_reintroduced_defect_from_being_suppressed(tmp_path: Path) -> None:
    """The property the whole design turns on.

    A whitelist that keeps an entry after its finding was fixed goes on suppressing that
    finding if it comes back, so the gate silently stops covering code it once covered.
    Pruning is what makes a baseline a record of what was there rather than a permanent
    amnesty for a class of defect.
    """
    path = tmp_path / "baseline.json"
    save_baseline(path, [_finding(digest="comes-back")])
    prune(path, partition([], load_baseline(path)).resolved)
    reintroduced = partition([_finding(digest="comes-back")], load_baseline(path))
    assert (len(reintroduced.new), len(reintroduced.known)) == (1, 0)


def test_pruning_keeps_the_entries_that_are_still_real(tmp_path: Path) -> None:
    """Pruning must remove only what was fixed, or adoption starts over every run."""
    path = tmp_path / "baseline.json"
    save_baseline(path, [_finding(digest="gone"), _finding(digest="still-here")])
    split = partition([_finding(digest="still-here")], load_baseline(path))
    prune(path, split.resolved)
    assert sorted(load_baseline(path)) == ["still-here"]


def test_a_finding_that_moved_down_the_file_is_still_known(tmp_path: Path) -> None:
    """Identity excludes the line number, so an import added above is not a new defect."""
    path = tmp_path / "baseline.json"
    first = Result("vibes/demo/rule", "error", "message", "tools/demo.py", line=10, subject="demo")
    moved = Result("vibes/demo/rule", "error", "message", "tools/demo.py", line=99, subject="demo")
    save_baseline(path, [first])
    assert len(partition([moved], load_baseline(path)).known) == 1


def test_recording_the_same_findings_twice_produces_identical_bytes(tmp_path: Path) -> None:
    """A baseline is reviewed in a diff, so re-recording must not reorder it."""
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    findings = [_finding(digest="z", rule="vibes/z"), _finding(digest="a", rule="vibes/a")]
    save_baseline(first, findings, recorded_at="2026-01-01T00:00:00Z")
    save_baseline(second, list(reversed(findings)), recorded_at="2026-01-01T00:00:00Z")
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_a_duplicate_finding_is_recorded_once(tmp_path: Path) -> None:
    """Two oracles reporting one defect must not need two entries to suppress it."""
    path = tmp_path / "baseline.json"
    assert save_baseline(path, [_finding(), _finding()]) == 1


def test_a_malformed_baseline_fails_rather_than_accepting_nothing(tmp_path: Path) -> None:
    """Treating an unreadable baseline as empty would turn every accepted finding red."""
    path = tmp_path / "baseline.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_baseline(path)


def test_sarif_results_are_read_back_by_the_identity_their_producer_assigned(tmp_path: Path) -> None:
    """A fingerprint invented here would match nothing, and suppress nothing while saying it did."""
    run = Run("demo", [Rule("vibes/demo/rule", "rule", "d", "error")], [_result()])
    log = tmp_path / "findings.sarif"
    log.write_text(json.dumps(build_log([run], REPO_ROOT)), encoding="utf-8")
    parsed = read_sarif(log)
    assert (len(parsed), parsed[0].fingerprint()) == (1, _result().fingerprint())


def _result() -> Result:
    """Build one normalized finding."""
    return Result("vibes/demo/rule", "error", "something broke", "tools/demo.py", 12, "demo")


def test_a_result_without_a_fingerprint_is_skipped(tmp_path: Path) -> None:
    """Assigning it a computed identity would silently fail to match the producing tool."""
    log = tmp_path / "findings.sarif"
    log.write_text(
        json.dumps({"runs": [{"results": [{"ruleId": "x", "message": {"text": "y"}}]}]}),
        encoding="utf-8",
    )
    assert read_sarif(log) == []


def test_this_repository_accepts_no_findings_of_its_own() -> None:
    """The machinery exists so other codebases can adopt these gates, not so this one can defer.

    §10's architectural invariants are non-negotiable. A non-empty baseline here would mean
    this repository had started suppressing its own violations, which is precisely what the
    feature must never be used for — so the assertion is on the committed file, not on a
    fixture.
    """
    accepted = load_baseline(REPO_ROOT / DEFAULT_BASELINE)
    assert accepted == {}


def test_status_fails_only_on_a_stale_baseline_and_only_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whether new findings fail a build is the gate's decision, not the baseline's.

    Two commands answering the same question is two commands that disagree the moment one
    changes, so `status` reports new findings and exits non-zero only for a baseline that
    has gone stale. `sarif_report --fail-on-error` owns the gate.
    """
    from finding_baseline import main as baseline_main

    log = tmp_path / "findings.sarif"
    run = Run("demo", [Rule("vibes/demo/rule", "rule", "d", "error")], [_result()])
    log.write_text(json.dumps(build_log([run], REPO_ROOT)), encoding="utf-8")
    baseline = tmp_path / "baseline.json"

    new_findings = baseline_main(
        ["status", "--source", str(log), "--baseline", str(baseline), "--strict"]
    )

    save_baseline(baseline, [_finding(digest="nothing-produces-this")])
    stale_strict = baseline_main(
        ["status", "--source", str(log), "--baseline", str(baseline), "--strict"]
    )
    stale_lenient = baseline_main(["status", "--source", str(log), "--baseline", str(baseline)])
    capsys.readouterr()
    assert (new_findings, stale_strict, stale_lenient) == (0, 1, 0)
