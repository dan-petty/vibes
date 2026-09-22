"""Tests for the single definition of which paths count as repository source.

The rule these tests pin is not "node_modules is excluded" but "every instrument that
scans this repository scans the same files". Divergent exclusion lists let a vendored
toolchain open backlog items against READMEs the repository does not own.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
# The two sample applications are standalone by design and cannot import the policy, so
# their agreement with it is asserted here instead of assumed. Both were wrong: the
# sentinel used an unpruned `rglob` and the quantifier excluded `__pycache__` alone.
sys.path.insert(0, str(REPO_ROOT / "examples" / "ast-invariant-sentinel"))
sys.path.insert(0, str(REPO_ROOT / "examples" / "code-smell-quantifier"))

from docs_validator import _discover_markdown_files
from resource_iteration_workbench import ResourceScanner
from sentinel import _collect_py_targets
from smell_quantifier import _python_files
from source_tree_policy import is_repository_source, iter_source_files


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


def test_every_python_scanner_agrees_with_the_policy_on_the_real_tree() -> None:
    """Four instruments scan this repository; all four must see the same modules.

    The existing agreement test compared the two that live in `tools/` and import the
    policy directly — the two that were already right. The two sample applications
    implement discovery themselves, were never compared to anything, and both diverged:
    `sentinel.py` with no arguments audited `node_modules` and exited non-zero on a
    vendored package, which no CI step or hook ever invoked it in a position to reveal.
    """
    policy = {p.resolve() for p in iter_source_files(REPO_ROOT, (".py",))}
    sentinel_seen = {p.resolve() for p in _collect_py_targets(REPO_ROOT)}
    quantifier_seen = {p.resolve() for p in _python_files([REPO_ROOT])}
    assert (sentinel_seen - policy, policy - sentinel_seen) == (set(), set())
    assert (quantifier_seen - policy, policy - quantifier_seen) == (set(), set())


def test_a_vendored_package_is_invisible_to_both_sample_applications(tmp_path: Path) -> None:
    """Asserted on a fixture as well as the real tree, so the guard holds in a clean clone.

    CI installs a Node toolchain only in a later step; a test that relies on `node_modules`
    happening to be on disk passes vacuously everywhere else, which is how this class of
    defect survives.
    """
    (tmp_path / "real.py").write_text("VALUE = 1\n", encoding="utf-8")
    for vendored in ("node_modules/pkg", "venv/lib", ".venv/lib", "thing.egg-info"):
        directory = tmp_path / vendored
        directory.mkdir(parents=True)
        (directory / "index.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert _collect_py_targets(tmp_path) == [tmp_path / "real.py"]
    assert _python_files([tmp_path]) == [tmp_path / "real.py"]


def test_a_file_target_is_audited_even_inside_a_vendored_directory(tmp_path: Path) -> None:
    """Pruning governs discovery, not an explicit argument.

    A pre-commit hook hands the sentinel exact paths. If pruning also filtered those, a
    staged file under any excluded directory would be silently certified unaudited, which
    is the failure mode the pruning was added to prevent, inverted.
    """
    vendored = tmp_path / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    named = vendored / "index.py"
    named.write_text("VALUE = 2\n", encoding="utf-8")
    assert (_collect_py_targets(named), _python_files([named])) == ([named], [named])
