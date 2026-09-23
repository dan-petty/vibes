#!/usr/bin/env python3
"""One row per metric, saying who computes it and — where we do — why nobody upstream can.

A conformance audit of this repository produced 106 findings, 59 of them in components that
reimplemented a solved problem. Cyclomatic complexity alone had **seven** implementations:
four under `examples/`, two under `tools/` and one under `benchmarks/`. Only the first four
were ever covered by §1a's copy-pasteable-exhibit exemption; the rest simply re-derived a
metric whose reference implementation was already a declared dependency, with
`pyproject.toml` stating what it was for.

This module is the answer to "who computes this", and it is meant to be read before anything
new is written. A row whose `reference` names a tool means **we do not compute it** — call
the adapter. A row with no reference must say why in `why_not_delegated`, and must carry a
`Witness`: an executed comparison showing what the candidate reference got wrong. A reason
nobody measured is an opinion, and opinions are how seven implementations happened.

The direction of a disagreement decides adoptability. A reference that is *stricter* than us
can be adopted: it never lets through what we would have caught. A reference that is *laxer*
is the defect this registry exists to prevent, and is recorded as a rejection with its
witness rather than quietly matched.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from radon.metrics import mi_visit
from radon.visitors import ComplexityVisitor

MIN_REASON_CHARS: Final[int] = 40


class Provenance(StrEnum):
    """Who computes a metric."""

    DELEGATED = "delegated"
    EXTENDED = "extended"


class MetricsUnavailable(RuntimeError):
    """Raised when a reference could not measure what it was asked to measure.

    Distinct from "measured and found nothing". `ruff check --select X --output-format json`
    on a path it cannot read prints a warning to stderr, emits `[]`, and **exits 0** — so a
    caller reading the findings alone concludes the file is clean when it was never linted.
    That is the `TargetIntegrity` defect this repository already guards against, re-entering
    through the delegation seam.
    """


@dataclass(frozen=True)
class Witness:
    """An executed comparison justifying a provenance decision.

    Every field is something a reader can paste into a shell. A rejection without one is a
    preference.
    """

    source: str
    reference_says: str
    ours_says: str


@dataclass(frozen=True)
class Metric:
    """One measurement this repository makes, and who is authoritative for it."""

    metric_id: str
    summary: str
    provenance: Provenance
    reference: str | None = None
    why_not_delegated: str = ""
    witness: Witness | None = None

    def __post_init__(self) -> None:
        """Refuse a row that does not justify itself."""
        if self.provenance is Provenance.DELEGATED and not self.reference:
            raise ValueError(f"{self.metric_id}: a delegated metric must name its reference")
        if self.provenance is Provenance.EXTENDED:
            if len(self.why_not_delegated) < MIN_REASON_CHARS:
                raise ValueError(
                    f"{self.metric_id}: computing a metric ourselves needs at least "
                    f"{MIN_REASON_CHARS} characters saying why no reference will do"
                )
            if self.witness is None:
                raise ValueError(f"{self.metric_id}: an extended metric must carry a witness")


METRICS: Final[dict[str, Metric]] = {
    metric.metric_id: metric
    for metric in (
        Metric(
            metric_id="cyclomatic_complexity",
            summary="McCabe cyclomatic complexity of one function",
            provenance=Provenance.DELEGATED,
            reference="radon.visitors.ComplexityVisitor",
        ),
        Metric(
            metric_id="maintainability_index",
            summary="Normalised maintainability score for a module",
            provenance=Provenance.DELEGATED,
            reference="radon.metrics.mi_visit",
        ),
        Metric(
            metric_id="too_many_public_methods",
            summary="Public methods on one class",
            provenance=Provenance.DELEGATED,
            reference="ruff PLR0904",
        ),
        Metric(
            metric_id="too_many_arguments",
            summary="Declared parameters on one callable",
            provenance=Provenance.DELEGATED,
            reference="ruff PLR0913",
        ),
        Metric(
            metric_id="too_many_statements",
            summary="Statements in one function body",
            provenance=Provenance.DELEGATED,
            reference="ruff PLR0915",
        ),
        Metric(
            metric_id="nesting_depth",
            summary="Deepest run of nested compound statements in one function",
            provenance=Provenance.EXTENDED,
            why_not_delegated=(
                "ruff's PLR1702 does not count `match`/`case` as nesting at all, so a "
                "four-level `for > match > case > for` passes at max-nested-blocks=2. A "
                "reference laxer than the gate cannot replace the gate."
            ),
            witness=Witness(
                source='def h(items, x):\n    for it in items:\n        match it:\n'
                       '            case {"k": v}:\n                for e in v:\n'
                       "                    print(e)\n",
                reference_says="ruff PLR1702 at max-nested-blocks=2: All checks passed",
                ours_says="depth 5, over the ceiling of 5's successor",
            ),
        ),
    )
}


def cyclomatic_complexity(node: ast.AST) -> int:
    """Return radon's complexity for one function node.

    Scored per node rather than per module. A module-level pass does not report every
    function definition — a method of a class declared inside a function body is not in the
    result — and a delegation that scores at module level therefore skips them in silence.

    radon reports a closure as its own block, where the visitor this replaces charged a
    nested function's decision points to the enclosing one. Some functions consequently
    score lower than they used to; that is radon's answer and radon is the reference.
    """
    visitor = ComplexityVisitor.from_ast(node)
    blocks = list(visitor.functions) + list(visitor.classes)
    return max((block.complexity for block in blocks), default=visitor.total_complexity)


def maintainability_index(source: str) -> float:
    """Return radon's maintainability index for a module."""
    return float(mi_visit(source, multi=True))


def nesting_depth(node: ast.AST, nesting_types: tuple[type[ast.AST], ...]) -> int:
    """Return the deepest run of nested compound statements in one function.

    Extended rather than delegated; see the registry row for the witness.
    """
    def walk(current: ast.AST, depth: int) -> int:
        deeper = depth + 1 if isinstance(current, nesting_types) else depth
        return max((walk(child, deeper) for child in ast.iter_child_nodes(current)), default=deeper)

    return max((walk(statement, 1) for statement in getattr(node, "body", [])), default=0)


def lint_findings(paths: Sequence[Path], rules: Sequence[str], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Run ruff over `paths` for `rules`, refusing to report a clean run it did not perform.

    `--output-format json` exits 0 and prints `[]` for a path it could not read, warning only
    on stderr. Reading the findings alone turns "not linted" into "clean", which is exactly
    the failure this repository's `TargetIntegrity` invariant exists to prevent.
    """
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise MetricsUnavailable(f"ruff was asked to lint paths that do not exist: {missing}")
    command = [
        sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache", "--preview",
        "--output-format", "json", "--select", ",".join(rules),
    ]
    for key, value in sorted(settings.items()):
        command += ["--config", f"{key} = {value}"]
    command += [str(path) for path in paths]
    # Fixed argv, no shell; every path is one the caller already resolved.
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=300)
    if "Failed to lint" in result.stderr:
        raise MetricsUnavailable(f"ruff could not lint every requested path: {result.stderr.strip()[:300]}")
    if result.returncode not in (0, 1):
        raise MetricsUnavailable(f"ruff exited {result.returncode}: {result.stderr.strip()[:300]}")
    parsed: list[dict[str, Any]] = json.loads(result.stdout or "[]")
    return parsed
