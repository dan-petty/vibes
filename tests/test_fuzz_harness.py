"""Tests for the instrument fuzzer, most of which exist to prove it can fail.

A fuzzer that reports nothing looks exactly like a repository with no defects, and this
one reported nothing on its first 320 cases. §8.3 is the rule that applies: a finding
nobody looks for is not a finding, and a detector that has never fired is not known to
work. So every property here is driven by a target that is deliberately broken in that
one way, and the real instruments are separately asserted to survive a real input.
"""

from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import fuzz_harness
from fuzz_core import SUFFIXES, Case, generate
from fuzz_harness import (
    TARGETS,
    Campaign,
    Failure,
    Plan,
    Target,
    crosscheck,
    evaluate,
    explore,
    gating_failures,
    main,
    observe,
    replay,
)

CASE = Case("python", "x = 1\n", 0, "unit")


def _probe(invoke: object, **overrides: object) -> Target:
    """Build a deliberately broken instrument to point the properties at."""
    fields: dict[str, object] = {
        "name": "probe",
        "corpus": "python",
        "module": "tests/test_fuzz_harness.py",
        "summary": "Deliberately broken instrument used to prove a property fires",
        "invoke": invoke,
    }
    return Target(**{**fields, **overrides})  # type: ignore[arg-type]


def _boom(text: str, workspace: Path) -> object:
    """Raise an exception the target has not declared."""
    raise RuntimeError("unhandled")


def _clean(text: str, workspace: Path) -> object:
    """Return the same answer for the same input, forever."""
    return len(text)


def test_a_target_that_raises_is_reported_as_a_crash(tmp_path: Path) -> None:
    """The property the whole tool exists for must be shown to fire."""
    failure = evaluate(_probe(_boom), CASE, tmp_path)
    assert failure is not None
    assert (failure.prop, failure.severity, "RuntimeError" in failure.detail) == (
        "crash",
        "error",
        True,
    )


def test_a_declared_tolerance_is_not_a_crash(tmp_path: Path) -> None:
    """Tolerance is a claim about the contract; widening it must be a deliberate edit."""
    assert evaluate(_probe(_boom, tolerated=(RuntimeError,)), CASE, tmp_path) is None


def test_a_target_that_answers_differently_is_reported(tmp_path: Path) -> None:
    """§10a.2: an instrument that answers differently for identical input is broken."""
    counter = itertools.count()

    def drifting(text: str, workspace: Path) -> object:
        return next(counter)

    failure = evaluate(_probe(drifting), CASE, tmp_path)
    assert failure is not None and failure.prop == "nondeterministic"


def test_a_repair_that_does_not_converge_is_reported(tmp_path: Path) -> None:
    """This is the property that found the real defect in the documentation auto-fixer."""

    def shortening(text: str, workspace: Path) -> str:
        return text[:-1] if text else text

    failure = evaluate(_probe(_clean, fixer=shortening), CASE, tmp_path)
    assert failure is not None and failure.prop == "non_idempotent"


def test_a_slow_target_is_a_warning_and_never_gates(tmp_path: Path) -> None:
    """A time budget measures the host; gating on it would block the slowest machine."""
    failure = evaluate(_probe(_clean), CASE, tmp_path, budget=0.0)
    assert failure is not None
    assert (failure.prop, failure.severity, gating_failures(_campaign(failure))) == (
        "slow",
        "warning",
        [],
    )


def _campaign(*failures: Failure) -> Campaign:
    """Wrap failures in a campaign, which is what the severity filter consumes."""
    return Campaign(executed=len(failures), failures=list(failures))


def test_a_clean_target_produces_no_finding(tmp_path: Path) -> None:
    """A property that fires on a healthy instrument reports noise, not defects."""
    assert evaluate(_probe(_clean), CASE, tmp_path) is None


def test_a_crash_preempts_the_other_properties(tmp_path: Path) -> None:
    """An instrument that raised cannot meaningfully be asked whether it is deterministic."""
    failure = evaluate(_probe(_boom, fixer=lambda text, ws: text + "!"), CASE, tmp_path)
    assert failure is not None and failure.prop == "crash"


def test_fingerprints_ignore_memory_addresses_and_the_scratch_directory(tmp_path: Path) -> None:
    """Both varied per process and made every instrument look non-deterministic."""

    def addressed(text: str, workspace: Path) -> object:
        return [object(), str(workspace / "case.py")]

    first = observe(_probe(addressed), "x", tmp_path / "a")
    second = observe(_probe(addressed), "x", tmp_path / "b")
    assert (first.fingerprint, first.error) == (second.fingerprint, None)


def test_a_scratch_name_containing_an_address_pattern_is_still_normalized() -> None:
    """`mkdtemp` draws from a pool that includes `0`, `x` and the hex digits.

    Roughly one scratch directory in three hundred is named something the address pattern
    rewrites, and normalizing addresses before paths then left the directory name inside
    the hash. It presented as a 0.3% flake that no serial re-run could reproduce, because
    contention raises the number of directories drawn and not the odds for any one.
    """
    hostile = Path("/tmp/vibes-fuzz-8t0x1im")
    benign = Path("/tmp/vibes-fuzz-qwrtyzzz")
    assert fuzz_harness._fingerprint([f"{hostile}/case.py"], hostile) == fuzz_harness._fingerprint(
        [f"{benign}/case.py"], benign
    )


def test_a_divergence_is_confirmed_before_it_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One anomalous sample in 726 subprocess calls is a nightly red build, not a defect."""
    answers = iter(["a", "b", "c", "same", "same", "same"])
    monkeypatch.setattr(fuzz_harness, "_seed_fingerprint", lambda t, p, s: next(answers))
    plan = Plan(cases=1, seed=1, corpus_dir=tmp_path / "corpus")
    campaign = crosscheck([_probe(_clean)], tmp_path / "ws", plan)
    assert (campaign.executed, campaign.failures) == (1, [])


def test_a_divergence_that_reproduces_is_still_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real hash-order dependence is deterministic per seed and survives the second pass."""
    monkeypatch.setattr(fuzz_harness, "_seed_fingerprint", lambda t, p, s: s)
    plan = Plan(cases=1, seed=1, corpus_dir=tmp_path / "corpus")
    campaign = crosscheck([_probe(_clean)], tmp_path / "ws", plan)
    assert (campaign.executed, len(gating_failures(campaign))) == (1, 1)


def test_failures_are_grouped_by_defect_not_by_input(tmp_path: Path) -> None:
    """Two hundred mutations of one module find one defect two hundred times."""
    campaign = explore([_probe(_boom)], tmp_path, Plan(cases=12, seed=5, save=False))
    assert (campaign.executed, len(campaign.failures)) == (12, 1)


def test_explore_stores_the_minimized_input_not_the_original(tmp_path: Path) -> None:
    """An unshrunk failure is a four-hundred-line file nobody reads."""

    def hates_functions(text: str, workspace: Path) -> object:
        if "def " in text:
            raise ValueError("found one")
        return len(text)

    corpus = tmp_path / "corpus"
    plan = Plan(cases=6, seed=3, corpus_dir=corpus)
    campaign = explore([_probe(hates_functions)], tmp_path / "ws", plan)
    stored = sorted(corpus.glob("probe/*.case"))
    text = stored[0].read_text(encoding="utf-8")
    assert (len(campaign.saved), "def " in text, len(text) < 200) == (1, True, True)


def test_replay_re_reports_a_stored_failure(tmp_path: Path) -> None:
    """The corpus is the gate; an entry that stops failing the build is not a regression test."""
    corpus = tmp_path / "corpus" / "probe"
    corpus.mkdir(parents=True)
    (corpus / "deadbeefdeadbeef.case").write_text("anything\n", encoding="utf-8")
    campaign = replay([_probe(_boom)], tmp_path / "ws", Plan(corpus_dir=tmp_path / "corpus"))
    assert (campaign.executed, len(gating_failures(campaign))) == (1, 1)


def test_replay_of_a_healthy_instrument_is_silent(tmp_path: Path) -> None:
    """A corpus entry whose defect was fixed must stop failing, or the gate never goes green."""
    corpus = tmp_path / "corpus" / "probe"
    corpus.mkdir(parents=True)
    (corpus / "deadbeefdeadbeef.case").write_text("anything\n", encoding="utf-8")
    campaign = replay([_probe(_clean)], tmp_path / "ws", Plan(corpus_dir=tmp_path / "corpus"))
    assert (campaign.executed, campaign.failures) == (1, [])


def test_crosscheck_reports_hash_seed_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subprocess plumbing is mocked; what is asserted is that disagreement is caught."""
    monkeypatch.setattr(fuzz_harness, "_seed_fingerprint", lambda target, path, seed: seed)
    plan = Plan(cases=2, seed=1, corpus_dir=tmp_path / "corpus")
    campaign = crosscheck([_probe(_clean)], tmp_path / "ws", plan)
    assert (campaign.executed, len(gating_failures(campaign))) == (2, 2)


def test_crosscheck_agrees_when_every_seed_agrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that fires on agreement would make the corpus unusable."""
    monkeypatch.setattr(fuzz_harness, "_seed_fingerprint", lambda target, path, seed: "same")
    plan = Plan(cases=3, seed=1, corpus_dir=tmp_path / "corpus")
    campaign = crosscheck([_probe(_clean)], tmp_path / "ws", plan)
    assert (campaign.executed, campaign.failures) == (3, [])


def test_every_target_names_a_corpus_that_can_be_generated() -> None:
    """A target whose corpus has no generator is a target that never runs."""
    assert all(target.corpus in SUFFIXES for target in TARGETS.values())


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_every_real_instrument_survives_a_well_formed_input(name: str, tmp_path: Path) -> None:
    """The instruments are the subject; a harness that cannot drive them proves nothing."""
    target = TARGETS[name]
    assert evaluate(target, generate(target.corpus, 11), tmp_path) is None


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_the_fingerprint_worker_answers_for_every_real_instrument(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`crosscheck` spawns this entry point per hash seed; a silent worker compares equal."""
    target = TARGETS[name]
    case = generate(target.corpus, 23)
    path = tmp_path / f"case{SUFFIXES[case.corpus]}"
    path.write_text(case.text, encoding="utf-8")
    code = main(["fingerprint", "--targets", name, "--input", str(path)])
    printed = capsys.readouterr().out.strip()
    assert (code, bool(re.fullmatch(r"[0-9a-f]{16}", printed))) == (0, True)


def test_the_committed_corpus_is_inert_on_disk() -> None:
    """A `.md` fixture would be swept up by the validator it is a fixture for."""
    corpus = Path(__file__).resolve().parent.parent / "artifacts" / "fuzz-corpus"
    strays = [p for p in corpus.rglob("*") if p.is_file() and p.suffix not in (".case", ".md")]
    assert strays == []


# --- CLI plumbing ---------------------------------------------------------------------
#
# `replay` is what `ci.yml` and the pre-push hook execute, and until these tests it was
# reachable only through those two — the untested-entry-point gap this repository has hit
# twice before, in `project_tooling.main()` and the sentinel's. A gate whose exit code is
# never asserted cannot be distinguished from one that always returns zero.


def _corpus_with(tmp_path: Path, target: str, text: str = "anything\n") -> Path:
    """Build a one-entry corpus directory for a named target."""
    directory = tmp_path / "corpus" / target
    directory.mkdir(parents=True)
    (directory / "deadbeefdeadbeef.case").write_text(text, encoding="utf-8")
    return tmp_path / "corpus"


def test_replay_exits_non_zero_when_a_stored_case_still_breaks_an_instrument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """This exit code is the gate. Nothing else makes CI go red on a regression."""
    monkeypatch.setitem(fuzz_harness.TARGETS, "probe", _probe(_boom))
    corpus = _corpus_with(tmp_path, "probe")
    code = main(["replay", "--targets", "probe", "--corpus-dir", str(corpus)])
    printed = capsys.readouterr().out
    assert (code, "probe/crash" in printed, "1 error(s)" in printed) == (1, True, True)


def test_replay_exits_zero_once_the_instrument_is_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corpus entry that can never go green would make the gate permanently red."""
    monkeypatch.setitem(fuzz_harness.TARGETS, "probe", _probe(_clean))
    code = main(["replay", "--targets", "probe", "--corpus-dir", str(_corpus_with(tmp_path, "probe"))])
    assert (code, "0 error(s)" in capsys.readouterr().out) == (0, True)


def test_a_timing_finding_alone_does_not_fail_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gating on a host-sensitive budget would fail the build on whichever runner was slow."""
    monkeypatch.setitem(fuzz_harness.TARGETS, "probe", _probe(_clean))
    corpus = _corpus_with(tmp_path, "probe")
    code = main(["replay", "--targets", "probe", "--corpus-dir", str(corpus), "--budget", "0"])
    assert (code, "probe/slow" in capsys.readouterr().out) == (0, True)


def test_run_reports_and_can_explore_without_growing_the_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--no-save` is how a campaign is investigated without committing what it found."""
    monkeypatch.setitem(fuzz_harness.TARGETS, "probe", _probe(_boom))
    corpus = tmp_path / "corpus"
    code = main(["run", "--targets", "probe", "--cases", "2", "--no-save", "--corpus-dir", str(corpus)])
    assert (code, corpus.exists(), "Explored" in capsys.readouterr().out) == (1, False, True)


def test_run_writes_the_minimized_case_into_the_corpus_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exploration that found something and kept nothing has to be run again to act on it."""
    monkeypatch.setitem(fuzz_harness.TARGETS, "probe", _probe(_boom))
    corpus = tmp_path / "corpus"
    main(["run", "--targets", "probe", "--cases", "2", "--corpus-dir", str(corpus)])
    capsys.readouterr()
    assert len(list((corpus / "probe").glob("*.case"))) == 1


def test_the_report_is_machine_readable_for_ci_and_the_sarif_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A finding that reaches only a terminal is the CI log this repository built SARIF to escape."""
    import json

    monkeypatch.setitem(fuzz_harness.TARGETS, "probe", _probe(_boom))
    report = tmp_path / "nested" / "fuzz.json"
    main(["replay", "--targets", "probe", "--corpus-dir", str(_corpus_with(tmp_path, "probe")),
          "--report", str(report)])
    capsys.readouterr()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert (payload["executed"], payload["failures"][0]["severity"]) == (1, "error")


def test_targets_lists_every_instrument_with_its_stored_case_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The listing is how an agent discovers what is under test and what it may raise."""
    code = main(["targets", "--corpus-dir", str(tmp_path)])
    printed = capsys.readouterr().out
    assert (code, all(name in printed for name in TARGETS)) == (0, True)


def test_fingerprint_without_an_input_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """The crosscheck worker is invoked by argv; a silent zero here would compare nothing."""
    code = main(["fingerprint", "--targets", "docs"])
    assert (code, "requires --input" in capsys.readouterr().err) == (2, True)


def test_an_unknown_target_is_rejected_rather_than_ignored() -> None:
    """Silently dropping an unrecognized name would run a smaller campaign than was asked for."""
    with pytest.raises(SystemExit):
        main(["replay", "--targets", "no-such-instrument"])


def test_crosscheck_exits_non_zero_when_the_seeds_disagree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`fuzz.yml` branches on this exit code; the subprocess layer is mocked, the wiring is not."""
    monkeypatch.setitem(fuzz_harness.TARGETS, "probe", _probe(_clean))
    monkeypatch.setattr(fuzz_harness, "_seed_fingerprint", lambda target, path, seed: seed)
    code = main(["crosscheck", "--targets", "probe", "--cases", "1", "--corpus-dir", str(tmp_path)])
    assert (code, "probe/nondeterministic" in capsys.readouterr().out) == (1, True)
