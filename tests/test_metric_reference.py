"""Tests for the registry that says who computes each metric.

A conformance audit found cyclomatic complexity implemented seven times in this repository —
four under `examples/`, two under `tools/`, one under `benchmarks/` — while radon, its
reference implementation, sat in `pyproject.toml` with a comment naming it. This registry
exists so the next metric has one obvious place to be looked up before it is written again.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from metric_reference import (
    METRICS,
    Metric,
    MetricsUnavailable,
    Provenance,
    cyclomatic_complexity,
    lint_findings,
    maintainability_index,
    nesting_depth,
)

NESTING_TYPES = (
    ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith,
    ast.Try, ast.TryStar, ast.ExceptHandler, ast.Match, ast.match_case,
)


# --- The registry refuses rows that do not justify themselves -----------------------------


def test_an_extended_metric_must_say_why_and_show_its_working() -> None:
    """A reason nobody measured is an opinion, and opinions are how seven implementations happened."""
    with pytest.raises(ValueError, match="at least"):
        Metric(metric_id="m", summary="s", provenance=Provenance.EXTENDED, why_not_delegated="short")
    with pytest.raises(ValueError, match="witness"):
        Metric(
            metric_id="m", summary="s", provenance=Provenance.EXTENDED,
            why_not_delegated="a" * 60,
        )


def test_a_delegated_metric_must_name_its_reference() -> None:
    """"Somebody else computes this" is not a row until it says who."""
    with pytest.raises(ValueError, match="name its reference"):
        Metric(metric_id="m", summary="s", provenance=Provenance.DELEGATED)


def test_every_row_in_the_registry_is_valid() -> None:
    """Constructed at import, so this asserts the registry loaded rather than that it exists."""
    assert METRICS
    assert all(isinstance(metric, Metric) for metric in METRICS.values())


# --- The witnesses are executed, not asserted ----------------------------------------------


def _ruff(source: str, tmp_path: Path, rule: str, setting: str) -> str:
    """Run one ruff rule over a snippet and return its first line of output."""
    module = tmp_path / "w.py"
    module.write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache", "--preview",
         "--select", rule, "--config", setting, str(module)],
        capture_output=True, text=True, check=False, timeout=120,
    )
    return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""


def test_the_nesting_witness_still_holds(tmp_path: Path) -> None:
    """The rejection of ruff's PLR1702, re-run rather than remembered.

    It does not count `match`/`case` as nesting, so a four-level `for > match > case > for`
    passes at `max-nested-blocks = 2` while this repository's walk reports 5. A reference
    laxer than the gate cannot replace the gate — and a witness that is never re-run is a
    claim about the past.
    """
    source = METRICS["nesting_depth"].witness.source
    assert "All checks passed" in _ruff(source, tmp_path, "PLR1702", "lint.pylint.max-nested-blocks = 2")
    ours = nesting_depth(ast.parse(source).body[0], NESTING_TYPES)
    assert ours == 5


def test_complexity_is_radons_because_ruffs_misses_comprehensions(tmp_path: Path) -> None:
    """Why the reference for complexity is radon and not ruff's C901.

    `[r for r in rows if r.a if r.b]` is three branches. radon scores the function 4; C901
    passes it at `max-complexity = 1`, so delegating complexity to ruff would have been a
    laxer gate wearing a reference implementation's name.
    """
    source = "def f(rows):\n    return [r for r in rows if r.a if r.b]\n"
    assert "All checks passed" in _ruff(source, tmp_path, "C901", "lint.mccabe.max-complexity = 1")
    assert cyclomatic_complexity(ast.parse(source).body[0]) == 4


# --- The adapters ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def p(a):\n    return a\n", 1),
        ("def f(rows):\n    return [r for r in rows if r.a if r.b]\n", 4),
        ('def d(c):\n    match c:\n        case "a": return 1\n        case _: return 0\n', 2),
        ("def w(a):\n    while a:\n        a -= 1\n    else:\n        pass\n", 3),
    ],
)
def test_complexity_matches_radon_because_it_is_radon(source: str, expected: int) -> None:
    """Not an agreement test any more. There is one implementation, and it is upstream's."""
    assert cyclomatic_complexity(ast.parse(source).body[0]) == expected


def test_a_closure_is_its_own_unit_of_control_flow() -> None:
    """A behaviour change, recorded rather than smuggled.

    The visitor this replaces charged a nested function's decision points to the enclosing
    one, scoring this `outer` at 5. radon reports the closure separately and gives 2. Some
    functions therefore score lower than they used to — a *laxer* result, on the reference's
    authority, which is the one direction that needs saying out loud.
    """
    source = ("def outer(a):\n    def inner(b):\n        return [x for x in b if x if x.y]\n"
              "    if a:\n        return inner(a)\n    return None\n")
    assert cyclomatic_complexity(ast.parse(source).body[0]) == 2


def test_the_maintainability_index_is_radons_number() -> None:
    """§1a: prefer the tool's own metric to a recomputation of it."""
    assert 0.0 <= maintainability_index("x = 1\n") <= 100.0


# --- A reference that could not measure must not report a clean run -------------------------


def test_a_path_that_cannot_be_linted_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """`ruff --select X --output-format json` on an unreadable path prints `[]` and exits 0.

    Reading the findings alone turns "not linted" into "clean" — the `TargetIntegrity`
    defect this repository already guards against, re-entering through the delegation seam.
    """
    with pytest.raises(MetricsUnavailable, match="do not exist"):
        lint_findings([tmp_path / "nosuch.py"], ["PLR0913"], {})


def test_a_real_finding_is_reported(tmp_path: Path) -> None:
    """The adapter has to be able to find something, or the check above proves nothing."""
    module = tmp_path / "wide.py"
    module.write_text("def wide(a, b, c, d, e, f):\n    return a\n", encoding="utf-8")
    findings = lint_findings([module], ["PLR0913"], {"lint.pylint.max-args": 2})
    assert [f["code"] for f in findings] == ["PLR0913"]


def test_a_clean_file_reports_nothing(tmp_path: Path) -> None:
    """And it has to distinguish that from the unreadable case above."""
    module = tmp_path / "ok.py"
    module.write_text("def narrow(a):\n    return a\n", encoding="utf-8")
    assert lint_findings([module], ["PLR0913"], {"lint.pylint.max-args": 2}) == []
