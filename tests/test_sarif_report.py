"""Tests for SARIF emission across every oracle.

The schema is the oracle here: a log that validates against the OASIS document is one
GitHub will accept, and the properties worth testing beyond it are the ones the schema
cannot express — that fingerprints survive a finding moving, and that a tool which found
nothing still reports, because that is the only way a fixed alert ever closes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from sarif_report import (
    ADAPTERS,
    DEFAULT_SCHEMA_PATH,
    SARIF_VERSION,
    Result,
    Rule,
    Run,
    build_log,
    collect_runs,
    docs_run,
    fuzz_run,
    sentinel_run,
    smell_run,
    supply_run,
    validate_log,
)
from sarif_report import (
    main as sarif_main,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = REPO_ROOT / DEFAULT_SCHEMA_PATH


# A deliberately defective repository. Scanning the real corpus cost 4.5 seconds and, when
# the repository is clean, exercised no mapping at all: zero findings prove nothing about
# how a finding is rendered. A fixture that actually trips each oracle is both faster and a
# stronger test, because every adapter then has real oracle output to translate.
DEEP_NESTING = """def tangled(a, b, c, d, e, f):
    if a:
        if b:
            if c:
                if d:
                    if e:
                        if f:
                            return 1
    return 0
"""

BROKEN_DOC = """# Broken

[missing target](./nowhere-at-all.md)
"""

DRIFTED_PYPROJECT = """[project]
name = "fixture"
dependencies = ["pytest>=8.0"]
"""

TAGGED_WORKFLOW = """jobs:
  build:
    steps:
      - uses: actions/checkout@v7
"""


@pytest.fixture(scope="session")
def defective_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A repository built to trip every oracle, so every adapter has something to map."""
    root = tmp_path_factory.mktemp("defective")
    (root / "tangled.py").write_text(DEEP_NESTING, encoding="utf-8")
    (root / "BROKEN.md").write_text(BROKEN_DOC, encoding="utf-8")
    (root / "pyproject.toml").write_text(DRIFTED_PYPROJECT, encoding="utf-8")
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(TAGGED_WORKFLOW, encoding="utf-8")
    return root


@pytest.fixture(scope="session")
def defective_runs(defective_root: Path) -> list[Run]:
    """Every adapter run once against the defective repository."""
    paths = [defective_root / "tangled.py"]
    return [
        sentinel_run(paths, defective_root),
        smell_run(paths, defective_root),
        docs_run(paths, defective_root),
        supply_run(paths, defective_root),
    ]


@pytest.fixture
def bare_root(tmp_path: Path) -> Path:
    """An empty repository, for exercising CLI plumbing without scanning a real corpus."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    return tmp_path


def _result(**overrides: object) -> Result:
    base = {
        "rule_id": "vibes/demo/rule",
        "level": "error",
        "message": "something is wrong",
        "file_path": "tools/demo.py",
        "line": 12,
        "subject": "demo",
    }
    return Result(**{**base, **overrides})


def _run(results: list[Result]) -> Run:
    return Run(
        tool_name="vibes-demo",
        rules=[Rule("vibes/demo/rule", "rule", "A demonstration rule", "error")],
        results=results,
    )


def test_a_log_validates_against_the_oasis_schema() -> None:
    """The published schema is what GitHub validates against, so it is what we test against."""
    log = build_log([_run([_result()])], REPO_ROOT)
    assert validate_log(log, SCHEMA) == []


def test_an_invalid_level_is_rejected_by_the_schema() -> None:
    """SARIF defines exactly four levels; the schema is what catches a fifth."""
    log = build_log([_run([_result(level="catastrophic")])], REPO_ROOT)
    assert validate_log(log, SCHEMA) != []


def test_a_tool_that_found_nothing_still_reports() -> None:
    """Code scanning closes an alert only when the tool reports again without it.

    Dropping empty runs as noise would leave every fixed defect showing red forever.
    """
    log = build_log([_run([]), _run([_result()])], REPO_ROOT)
    assert (len(log["runs"]), log["version"]) == (2, SARIF_VERSION)


def test_only_rules_that_fired_are_declared() -> None:
    """Declaring dormant rules renders them as alerts nobody can act on."""
    run = Run(
        tool_name="vibes-demo",
        rules=[
            Rule("vibes/demo/rule", "fired", "Fires", "error"),
            Rule("vibes/demo/quiet", "quiet", "Never fires", "note"),
        ],
        results=[_result()],
    )
    declared = {r["id"] for r in build_log([run], REPO_ROOT)["runs"][0]["tool"]["driver"]["rules"]}
    assert declared == {"vibes/demo/rule"}


def test_a_fingerprint_survives_the_finding_moving_down_the_file() -> None:
    """An import added above a defect is not a new defect, and must not re-alert."""
    assert _result(line=12).fingerprint() == _result(line=400).fingerprint()


def test_a_fingerprint_distinguishes_different_subjects_in_one_file() -> None:
    """Two findings of the same rule in one file are two alerts, not one."""
    assert _result(subject="alpha").fingerprint() != _result(subject="beta").fingerprint()


def test_locations_are_repository_relative(tmp_path: Path) -> None:
    """Code scanning resolves URIs against the repository root; absolute paths resolve nowhere."""
    absolute = str(REPO_ROOT / "tools" / "demo.py")
    log = build_log([_run([_result(file_path=absolute)])], REPO_ROOT)
    uri = log["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "tools/demo.py"


def test_a_line_number_is_never_below_one() -> None:
    """SARIF requires startLine >= 1, and a finding about a whole file reports line zero."""
    log = build_log([_run([_result(line=0)])], REPO_ROOT)
    region = log["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
    assert (region["startLine"], validate_log(log, SCHEMA)) == (1, [])


def test_advisory_smells_are_excluded_unless_asked_for() -> None:
    """Advisory findings need judgement, and as alerts they bury the ones that gate."""
    target = [REPO_ROOT / "tools" / "resource_iteration_workbench.py"]
    default = smell_run(target, REPO_ROOT)
    with_advisory = smell_run(target, REPO_ROOT, include_advisory=True)
    assert len(default.results) < len(with_advisory.results)


def test_every_adapter_produces_a_schema_valid_run(
    defective_runs: list[Run], defective_root: Path
) -> None:
    """Each oracle's mapping must hold on real oracle output, not a hand-built Result."""
    assert validate_log(build_log(defective_runs, defective_root), SCHEMA) == []


def test_every_adapter_actually_finds_its_defect(defective_runs: list[Run]) -> None:
    """A green adapter on a deliberately broken repository is an adapter wired to nothing."""
    produced = {run.tool_name: len(run.results) for run in defective_runs}
    assert all(count > 0 for count in produced.values()), produced


def test_every_adapter_is_represented_even_when_it_finds_nothing(
    defective_runs: list[Run], defective_root: Path
) -> None:
    """Four oracles run, so four runs are published, whatever they found."""
    assert len(build_log(defective_runs, defective_root)["runs"]) == 4


# The four adapters above scan a tree, so a deliberately broken tree exercises all of them
# at once. The fuzz adapter does not: its oracle is the committed regression corpus, which
# is clean by design, and keeping an entry in it that still fails would mean keeping an
# instrument that is still broken. It is therefore covered separately, and this tripwire
# fails if a sixth adapter is registered without anyone deciding which group it belongs to.
TREE_SCANNING_ADAPTERS: tuple[str, ...] = ("docs", "sentinel", "smells", "supply")


def test_every_registered_adapter_is_exercised_by_a_test() -> None:
    """An adapter reaching code scanning untested is a mapping nobody has ever seen run."""
    assert sorted(ADAPTERS) == sorted([*TREE_SCANNING_ADAPTERS, "fuzz"])


def test_the_fuzz_adapter_reports_even_when_the_corpus_is_clean(tmp_path: Path) -> None:
    """A tool that stops appearing leaves every alert it ever raised open forever."""
    run = fuzz_run([], tmp_path, corpus_dir=tmp_path / "empty-corpus")
    assert (run.tool_name, run.results) == ("vibes-instrument-fuzzer", [])


def test_the_fuzz_adapter_locates_a_finding_at_the_instrument_not_the_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corpus entry is working as intended; the file that needs editing is the parser."""
    import fuzz_harness

    failure = fuzz_harness.Failure(
        target="docs_fix",
        prop="non_idempotent",
        corpus="markdown",
        seed=0,
        origin="corpus:abc.case",
        detail="repair did not converge",
        digest="abc",
    )
    monkeypatch.setattr(
        fuzz_harness,
        "replay",
        lambda targets, workspace, plan: fuzz_harness.Campaign(1, [failure]),
    )
    run = fuzz_run([], tmp_path, corpus_dir=tmp_path)
    result = run.results[0]
    assert (result.rule_id, result.level, result.file_path.endswith("docs_validator.py")) == (
        "vibes/fuzz/non_idempotent",
        "error",
        True,
    )


def test_the_fuzz_adapter_maps_a_timing_finding_below_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A time budget measures the host; an alert that gates on it blocks the slowest runner."""
    import fuzz_harness

    slow = fuzz_harness.Failure("docs", "slow", "markdown", 0, "corpus:abc.case", "took 12s", "abc")
    monkeypatch.setattr(
        fuzz_harness, "replay", lambda targets, workspace, plan: fuzz_harness.Campaign(1, [slow])
    )
    assert fuzz_run([], tmp_path, corpus_dir=tmp_path).results[0].level == "warning"


def test_one_failing_adapter_does_not_silence_the_others(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], bare_root: Path
) -> None:
    """A broken oracle must not take the whole report down; it must say so and continue."""
    import sarif_report

    def explode(paths: object, root: object) -> Run:
        raise OSError("oracle unavailable")

    monkeypatch.setitem(sarif_report.ADAPTERS, "docs", explode)
    runs = list(collect_runs(["docs", "supply"], [], bare_root))
    assert (len(runs), "oracle unavailable" in capsys.readouterr().err) == (1, True)


def test_cli_writes_a_schema_valid_log(
    tmp_path: Path, bare_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The written artefact is what CI uploads, so it is validated before it is written."""
    out = tmp_path / "nested" / "findings.sarif"
    exit_code = sarif_main(
        ["--root", str(bare_root), "--out", str(out), "--sources", "supply", "--schema", str(SCHEMA)]
    )
    capsys.readouterr()
    log = json.loads(out.read_text(encoding="utf-8"))
    assert (exit_code, log["version"], validate_log(log, SCHEMA)) == (0, SARIF_VERSION, [])


def test_cli_can_fail_the_build_on_an_error_level_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A report is not a gate unless it can go red on request."""
    import sarif_report

    monkeypatch.setitem(
        sarif_report.ADAPTERS, "supply", lambda paths, root: _run([_result(level="error")])
    )
    args = ["--root", str(tmp_path), "--out", str(tmp_path / "f.sarif"), "--sources", "supply"]
    lenient = sarif_main([*args, "--schema", str(SCHEMA)])
    strict = sarif_main([*args, "--schema", str(SCHEMA), "--fail-on-error"])
    capsys.readouterr()
    assert (lenient, strict) == (0, 1)


def test_cli_refuses_to_write_a_log_that_fails_the_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An invalid upload is rejected by GitHub with a message naming neither field nor run."""
    import sarif_report

    monkeypatch.setitem(
        sarif_report.ADAPTERS, "supply", lambda paths, root: _run([_result(level="bogus")])
    )
    out = tmp_path / "f.sarif"
    exit_code = sarif_main(
        ["--root", str(tmp_path), "--out", str(out), "--sources", "supply", "--schema", str(SCHEMA)]
    )
    assert (exit_code, out.exists(), "schema validation" in capsys.readouterr().err) == (
        1,
        False,
        True,
    )


def test_a_baselined_finding_is_suppressed_but_its_run_still_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run emptied by the baseline must still appear, or its old alerts never resolve."""
    import sarif_report
    from finding_baseline import save_baseline

    result = _result(level="error")
    monkeypatch.setitem(sarif_report.ADAPTERS, "supply", lambda paths, root: _run([result]))
    baseline = tmp_path / "baseline.json"
    save_baseline(baseline, [result])
    out = tmp_path / "f.sarif"
    code = sarif_main(
        ["--root", str(tmp_path), "--out", str(out), "--sources", "supply",
         "--schema", str(SCHEMA), "--baseline", str(baseline), "--fail-on-error"]
    )
    printed = capsys.readouterr().out
    log = json.loads(out.read_text(encoding="utf-8"))
    assert (code, len(log["runs"]), log["runs"][0]["results"]) == (0, 1, [])
    assert "1 accepted by baseline" in printed


def test_a_finding_absent_from_the_baseline_still_fails_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Adoption must not become amnesty: anything new is reported exactly as before."""
    import sarif_report
    from finding_baseline import save_baseline

    monkeypatch.setitem(
        sarif_report.ADAPTERS, "supply", lambda paths, root: _run([_result(subject="fresh")])
    )
    baseline = tmp_path / "baseline.json"
    save_baseline(baseline, [_result(subject="old")])
    code = sarif_main(
        ["--root", str(tmp_path), "--out", str(tmp_path / "f.sarif"), "--sources", "supply",
         "--schema", str(SCHEMA), "--baseline", str(baseline), "--fail-on-error"]
    )
    capsys.readouterr()
    assert code == 1
