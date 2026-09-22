"""Tests for measuring what this repository builds against what it judges.

The measurement exists because the drift it reports is invisible from inside any single
change: every commit looked reasonable, and the aggregate was 93% tooling. The test that
matters most is the one pinning *lines* rather than *touches*, because the first version
counted touches and reported a comfortable split for a window containing no application
work at all.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from portfolio_balance import (
    DEFAULT_MANIFEST,
    Balance,
    _classify_path,
    _numstat_row,
    classify,
    declared_kinds,
    investment,
    portfolio,
)


def _manifest(tmp_path: Path, kinds: list[str | None]) -> Path:
    """Write a manifest declaring the given kinds."""
    entries = [{"key": f"c{i}", **({"kind": k} if k else {})} for i, k in enumerate(kinds)]
    path = tmp_path / "capabilities.yaml"
    path.write_text(yaml.safe_dump({"capabilities": entries}), encoding="utf-8")
    return path


def test_portfolio_counts_the_kinds_a_manifest_declares(tmp_path: Path) -> None:
    """The portfolio is what the repository has, read from where it is recorded."""
    measured = portfolio(_manifest(tmp_path, ["capability", "capability", "quality"]))
    assert (measured.counts, measured.share("capability")) == ({"capability": 2, "quality": 1}, 0.667)


def test_an_undeclared_kind_is_counted_as_undeclared_not_guessed(tmp_path: Path) -> None:
    """A classifier that guesses reports whatever its word list caught; this one declines."""
    measured = portfolio(_manifest(tmp_path, ["capability", None]))
    assert measured.counts == {"capability": 1, "undeclared": 1}


def test_a_share_of_nothing_is_zero_rather_than_a_crash() -> None:
    """An empty window is the normal state of a new checkout, not an error."""
    assert Balance("empty").share("capability") == 0.0


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("examples/adaptive-web-crawler/crawler.py", "capability"),
        ("tools/docs_validator.py", "quality"),
        ("tests/test_thing.py", "other"),
        ("AGENTS.md", "other"),
    ],
)
def test_paths_are_classified_by_where_they_live(path: str, expected: str) -> None:
    """Location is the only mechanical signal available, and it is stated rather than inferred."""
    assert _classify_path(path) == expected


@pytest.mark.parametrize(
    ("row", "expected"),
    [("12\t3\tsrc/a.py", (12, "src/a.py")), ("-\t-\timg.png", None), ("nonsense", None)],
)
def test_numstat_rows_parse_and_binaries_are_skipped(row: str, expected: object) -> None:
    """A binary file reports `-` for its counts; treating that as zero would be a silent lie."""
    assert _numstat_row(row) == expected


def _git(root: Path, *args: str) -> None:
    """Run one git command in a fixture repository."""
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_investment_counts_lines_so_a_sweep_cannot_look_like_a_build(tmp_path: Path) -> None:
    """The regression that matters.

    Counting touched files reported a comfortable split for a window whose only
    `examples/` activity was a repository-wide lint sweep across 29 files. A sweep touches
    many locations and adds almost nothing; a build does the reverse. This fixture is that
    shape: one line added across three example files, eighty added to one tool.
    """
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    for name in ("a", "b", "c"):
        target = tmp_path / "examples" / name
        target.mkdir(parents=True)
        (target / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "gate.py").write_text("\n".join(f"line_{i} = {i}" for i in range(80)) + "\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")

    measured = investment(5, tmp_path)
    assert (measured.counts["capability"], measured.counts["quality"]) == (3, 80)
    assert measured.share("capability") < 0.05


def test_every_capability_in_the_real_manifest_declares_a_kind() -> None:
    """An undeclared capability makes the ratio quietly wrong rather than visibly incomplete."""
    document = yaml.safe_load((REPO_ROOT / DEFAULT_MANIFEST).read_text(encoding="utf-8"))
    undeclared = [c["key"] for c in document["capabilities"] if "kind" not in c]
    assert undeclared == []


# --- Classification by declaration ----------------------------------------------------
#
# Location alone is wrong in both directions: `tools/app_factory.py` builds applications and
# `examples/ast-invariant-sentinel/` judges them. The first version of this measurement
# reported building the factory as quality investment, moving the ratio the wrong way on the
# very change made to correct the imbalance.

_DECLARED = {
    "tools/app_factory.py": "capability",
    "examples/ast-invariant-sentinel/sentinel.py": "quality",
    "examples/adaptive-web-crawler/": "capability",
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tools/app_factory.py", "capability"),
        ("examples/ast-invariant-sentinel/sentinel.py", "quality"),
        ("examples/adaptive-web-crawler/crawler.py", "capability"),
        ("tools/sarif_report.py", "quality"),
        ("examples/polyglot-cst-parser/parser.py", "capability"),
        ("AGENTS.md", "other"),
    ],
)
def test_a_declaration_outranks_a_location(path: str, expected: str) -> None:
    """Undeclared paths still fall back to location, or the ratio measures a handful of files."""
    declared = {k.rstrip("/"): v for k, v in _DECLARED.items()}
    ordered = dict(sorted(declared.items(), key=lambda item: -len(item[0])))
    assert _classify_path(path, ordered) == expected


def test_the_most_specific_declaration_wins() -> None:
    """`examples/` alone would classify the sentinel and the crawler identically."""
    declared = declared_kinds(REPO_ROOT / DEFAULT_MANIFEST)
    lengths = [len(path) for path in declared]
    assert lengths == sorted(lengths, reverse=True)


def test_the_real_manifest_classifies_the_factory_as_a_capability() -> None:
    """The regression in one line: this is the case that made the metric disagree with itself."""
    declared = declared_kinds(REPO_ROOT / DEFAULT_MANIFEST)
    assert _classify_path("tools/app_factory.py", declared) == "capability"


def test_investment_reads_the_manifest_when_it_is_given_one(tmp_path: Path) -> None:
    """Without the manifest the fallback applies, which is the behaviour that was wrong."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "maker.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    manifest = tmp_path / "capabilities.yaml"
    manifest.write_text(
        yaml.safe_dump({"capabilities": [{"key": "m", "kind": "capability", "path": "tools/maker.py"}]}),
        encoding="utf-8",
    )
    without = investment(5, tmp_path)
    with_manifest = investment(5, tmp_path, manifest)
    assert (without.counts, with_manifest.counts) == ({"quality": 1}, {"capability": 1})


# --- A capability that outgrows one file ------------------------------------------------


def test_a_capability_may_declare_more_than_one_path(tmp_path: Path) -> None:
    """One file per capability was the assumption, and it expired on the next change."""
    manifest = tmp_path / "capabilities.yaml"
    manifest.write_text(
        yaml.safe_dump({"capabilities": [
            {"key": "factory", "kind": "capability",
             "path": "tools/app_factory.py",
             "paths": ["tools/app_factory.py", "tools/contract_variables.py"]},
        ]}),
        encoding="utf-8",
    )
    declared = declared_kinds(manifest)
    assert _classify_path("tools/contract_variables.py", declared) == "capability"


def test_the_real_manifest_classifies_the_factorys_second_module_as_a_capability() -> None:
    """The regression in one line, and the second time this metric has needed one.

    `tools/contract_variables.py` exists to close a capability gap, it lives beside the
    instruments, and one commit after the classifier was repaired it was counted as
    quality again — the same defect arriving through the declaration rather than through
    the code, because the declaration named a file and the capability had grown a module.
    """
    declared = declared_kinds(REPO_ROOT / DEFAULT_MANIFEST)
    assert _classify_path("tools/contract_variables.py", declared) == "capability"


def test_every_declared_path_exists() -> None:
    """A declaration pointing at a moved file silently stops covering it.

    Nothing else would notice: the classifier falls back to location, which is the answer
    the declaration was added to override, and the ratio goes quietly wrong again.
    """
    declared = declared_kinds(REPO_ROOT / DEFAULT_MANIFEST)
    missing = sorted(path for path in declared if not (REPO_ROOT / path).exists())
    assert missing == []


# --- How the answer was reached ---------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tools/app_factory.py", ("capability", "declaration")),
        ("tools/fuzz_harness.py", ("quality", "location")),
        ("examples/fastmcp-token-bucket-gateway/gateway.py", ("capability", "location")),
        ("docs/ROADMAP.md", ("other", "location")),
    ],
)
def test_classification_says_whether_a_declaration_or_a_location_decided(
    path: str, expected: tuple[str, str]
) -> None:
    """The fallback is where this measurement went wrong, twice, and silently both times."""
    assert classify(path, declared_kinds(REPO_ROOT / DEFAULT_MANIFEST)) == expected


def test_the_declared_share_is_reported_beside_the_ratio() -> None:
    """A ratio computed mostly from locations is a ratio about the directory layout."""
    measure = Balance("m", {"capability": 10, "quality": 30}, {"declaration": 10, "location": 30})
    assert measure.declared_share == 0.25


def test_a_share_of_no_classifications_is_zero_rather_than_a_crash() -> None:
    """An empty window is the normal state of a new checkout, not an error."""
    assert Balance("m").declared_share == 0.0


def test_the_investment_measure_records_how_each_line_was_classified(tmp_path: Path) -> None:
    """Reported from the same walk that produces the counts, so the two cannot disagree."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "app_factory.py").write_text("A = 1\n", encoding="utf-8")
    (tmp_path / "tools" / "other.py").write_text("B = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "one")
    manifest = tmp_path / "capabilities.yaml"
    manifest.write_text(
        yaml.safe_dump({"capabilities": [
            {"key": "factory", "kind": "capability", "paths": ["tools/app_factory.py"]},
        ]}),
        encoding="utf-8",
    )
    measure = investment(5, tmp_path, manifest)
    assert measure.by_source == {"declaration": 1, "location": 1}
