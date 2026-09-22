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
