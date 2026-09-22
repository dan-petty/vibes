"""Tests for the single definition of which paths count as repository source.

The rule these tests pin is not "node_modules is excluded" but "every instrument that
scans this repository scans the same files". Divergent exclusion lists let a vendored
toolchain open backlog items against READMEs the repository does not own.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from docs_validator import _discover_markdown_files
from resource_iteration_workbench import ResourceScanner
from source_tree_policy import is_repository_source

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_vendored_and_generated_paths_are_not_repository_source() -> None:
    """Dependencies, caches, build output, and hidden directories are all excluded."""
    excluded = [
        Path("node_modules/pkg/README.md"),
        Path("venv/lib/module.py"),
        Path(".venv/lib/module.py"),
        Path("pkg/__pycache__/module.pyc"),
        Path("vibes.egg-info/PKG-INFO"),
        Path(".github/workflows/ci.yml"),
        Path(".git/config"),
    ]
    assert [is_repository_source(path) for path in excluded] == [False] * len(excluded)


def test_repository_paths_are_source_regardless_of_how_the_scan_was_rooted() -> None:
    """Judging by components keeps the verdict stable across relative and absolute paths."""
    included = [
        Path("tools/docs_validator.py"),
        Path("./tools/docs_validator.py"),
        Path("/srv/checkout/tools/docs_validator.py"),
        Path("observations/systems/15-a-count-is-not-a-cost.md"),
    ]
    assert [is_repository_source(path) for path in included] == [True] * len(included)


def test_a_vendored_directory_is_invisible_to_every_scanner(tmp_path: Path) -> None:
    """A vendored dependency must not enter any instrument's corpus."""
    (tmp_path / "real.md").write_text("# Real\n", encoding="utf-8")
    (tmp_path / "real.py").write_text("VALUE = 1\n", encoding="utf-8")
    vendored = tmp_path / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "README.md").write_text("# Vendored\n", encoding="utf-8")
    (vendored / "index.py").write_text("VALUE = 2\n", encoding="utf-8")

    discovered = _discover_markdown_files(tmp_path, (".md", ".markdown"))
    scanned = ResourceScanner.scan_directory(tmp_path, include_docs=True)
    scanned_names = {Path(resource.file_path).name for resource in scanned}

    assert (discovered, "README.md" in scanned_names, "index.py" in scanned_names) == (
        [tmp_path / "real.md"],
        False,
        False,
    )


def test_both_instruments_agree_on_the_real_corpus() -> None:
    """The validator and the workbench must see the same documents in this repository.

    Compares discovery rather than a full scan: the disagreement this guards against is
    about which files are in the corpus, and parsing all of them to find that out would
    put this file over the fast-feedback ceiling it is meant to protect.
    """
    validated = {p.resolve() for p in _discover_markdown_files(REPO_ROOT, (".md", ".markdown"))}
    scanned = {p.resolve() for p in ResourceScanner.discover(REPO_ROOT, ".md")}
    assert (scanned - validated, validated - scanned) == (set(), set())
