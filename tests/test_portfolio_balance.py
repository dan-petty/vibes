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
