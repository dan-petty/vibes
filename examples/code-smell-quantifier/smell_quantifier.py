#!/usr/bin/env python3
"""Code Smell Quantifier: deterministic, stdlib-only measurement of structural decay.

This repository already enforces two metrics — cyclomatic complexity and nesting depth —
and both are pass/fail gates. A gate answers "may this ship"; it does not answer "where is
this codebase getting worse, and by how much". Ten further metrics that established tooling
measures had no counterpart here:

    radon mi / hal      maintainability index, Halstead volume
    PMD-CPD, jscpd      duplicated blocks
    pylint R0913/R0915  long parameter lists, long functions
    pylint R0902/R0904  god objects
    sonar, cohesion     LCOM4
    import-linter       coupling, instability, import cycles
    vulture             unreferenced module-level symbols

Each detector here is deterministic and reports a *measured value against a stated
threshold*, never a judgement. A finding that cannot name its number is an opinion, and
opinions do not belong in a mechanical oracle.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

import networkx
from radon.metrics import mi_compute, mi_parameters
from vulture import Vulture

# Thresholds are the published defaults of the tools each detector mirrors, so a number
# here can be traced to a source rather than to taste.
MAX_PARAMETERS: Final[int] = 5           # pylint R0913 default
MAX_FUNCTION_STATEMENTS: Final[int] = 50  # pylint R0915 default
MAX_CLASS_METHODS: Final[int] = 20        # pylint R0904 default
MAX_CLASS_ATTRIBUTES: Final[int] = 7      # pylint R0902 default
MIN_CLONE_STATEMENTS: Final[int] = 6      # PMD-CPD minimum-tokens analogue
# Statement count alone makes six consecutive imports a "clone" of any other six imports,
# because they are structurally identical everywhere. CPD avoids this by counting tokens.
# Calibrated on this corpus: import windows peak at 27 AST nodes while code windows have a
# median of 128, so a 40-node floor excludes every import run and keeps 90% of code.
MIN_CLONE_NODE_MASS: Final[int] = 40
# Vulture's own default. Below this its findings need a human to confirm reachability.
VULTURE_MIN_CONFIDENCE: Final[int] = 60
# radon's normalized MI scale grades A = 20-100, B = 10-19, C = 0-9; Visual Studio uses
# the same normalization with green >= 20. 20 is therefore the A/B boundary, not 65 —
# 65 belongs to the unnormalized SEI scale and is a common transcription error.
LOW_MAINTAINABILITY_INDEX: Final[float] = 20.0


class Smell(StrEnum):
    """The closed set of smells this tool can measure."""

    LONG_PARAMETER_LIST = "LongParameterList"
    LONG_FUNCTION = "LongFunction"
    GOD_CLASS = "GodClass"
    LOW_COHESION = "LowCohesion"
    DUPLICATED_BLOCK = "DuplicatedBlock"
    IMPORT_CYCLE = "ImportCycle"
    UNREFERENCED_SYMBOL = "UnreferencedSymbol"
    LOW_MAINTAINABILITY = "LowMaintainability"


@dataclass(frozen=True)
class SmellFinding:
    """A measured deviation: the value, the threshold it exceeded, and where."""

    smell: Smell
    file_path: str
    line_number: int
    subject: str
    measured: float
    threshold: float
    detail: str

    @property
    def excess(self) -> float:
        """Return how far past the threshold the measurement sits."""
        return round(self.measured - self.threshold, 2)


def module_metrics(source: str) -> tuple[float, float, int]:
    """Return (maintainability index, Halstead volume, max complexity) from radon.

    These were hand-written here first, against radon's published formulae. Measured on
    this repository the hand-written maintainability index ran 18 to 40 points below
    radon's on the same modules: the ranking survived, the absolute values did not, and a
    threshold calibrated to radon's scale was being applied to numbers that were not on
    it. Reporting the reference implementation's number is not a convenience, it is the
    difference between a metric and a paraphrase of one.
    """
    volume, complexity, sloc, comments = mi_parameters(source, count_multi=True)
    return round(mi_compute(volume, complexity, sloc, comments), 1), round(volume, 2), complexity


def _function_nodes(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return every function definition, including methods and nested closures."""
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    return [node for node in ast.walk(tree) if isinstance(node, kinds)]


def _parameter_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count declared parameters, excluding an implicit self or cls."""
    args = node.args
    declared = args.posonlyargs + args.args + args.kwonlyargs
    names = [a.arg for a in declared]
    bound = 1 if names and names[0] in ("self", "cls") else 0
    extras = bool(args.vararg) + bool(args.kwarg)
    return len(names) - bound + extras


def detect_long_parameter_lists(tree: ast.AST, path: Path) -> list[SmellFinding]:
    """Flag functions whose parameter count exceeds the pylint R0913 default."""
    findings = []
    for node in _function_nodes(tree):
        count = _parameter_count(node)
        if count > MAX_PARAMETERS:
            findings.append(SmellFinding(
                smell=Smell.LONG_PARAMETER_LIST, file_path=str(path), line_number=node.lineno,
                subject=node.name, measured=count, threshold=MAX_PARAMETERS,
                detail=f"{count} parameters; each one multiplies the call sites that must change together",
            ))
    return findings


def detect_long_functions(tree: ast.AST, path: Path) -> list[SmellFinding]:
    """Flag functions by statement count, which complexity alone does not capture.

    A 300-line linear function has a cyclomatic complexity of 1 and passes every gate in
    this repository. Length and branching are independent axes of difficulty.
    """
    findings = []
    for node in _function_nodes(tree):
        statements = sum(1 for child in ast.walk(node) if isinstance(child, ast.stmt))
        if statements > MAX_FUNCTION_STATEMENTS:
            findings.append(SmellFinding(
                smell=Smell.LONG_FUNCTION, file_path=str(path), line_number=node.lineno,
                subject=node.name, measured=statements, threshold=MAX_FUNCTION_STATEMENTS,
                detail=f"{statements} statements; branching gates do not see raw length",
            ))
    return findings


def _class_attribute_names(node: ast.AST) -> set[str]:
    """Return instance attribute names *assigned* within a class or function body.

    Only Store contexts count. Collecting every `self.X` reference counts method calls
    and class constants as attributes: `self.run_command(...)` and `self.IGNORE_TAGS`
    are not state. That inflation put nine classes over the attribute ceiling here when
    their real counts were at or below it, and each would have been a refactor of
    correct code.
    """
    targets = (
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
        and child.value.id == "self"
        and isinstance(child.ctx, ast.Store)
    )
    return set(targets)


def _class_attribute_references(node: ast.AST) -> set[str]:
    """Return every `self.X` name a body touches, read or written.

    LCOM4 connects two methods when they access a shared instance variable, regardless
    of which one writes it, so cohesion needs references while the attribute ceiling
    needs assignments. They are different questions about the same syntax.
    """
    return {
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
        and child.value.id == "self"
    }


def _class_methods(node: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return methods declared directly on a class."""
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    return [child for child in node.body if isinstance(child, kinds)]


def _is_data_carrier(node: ast.ClassDef) -> bool:
    """Return True for classes whose whole purpose is to hold fields.

    A dataclass with nine fields is a record, not a god object. pylint's R0902 has the
    same false positive, and excluding data carriers is the standard correction: the
    attribute ceiling is about accumulated responsibility, and a record has one.
    """
    decorators = {
        d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
        for d in node.decorator_list
        if isinstance(d, (ast.Name, ast.Attribute))
    }
    decorators |= {
        getattr(d.func, "id", getattr(d.func, "attr", ""))
        for d in node.decorator_list if isinstance(d, ast.Call)
    }
    bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases}
    return bool(decorators & {"dataclass"}) or bool(
        bases & {"NamedTuple", "TypedDict", "BaseModel", "Enum", "StrEnum", "IntEnum"}
    )


def detect_god_classes(tree: ast.AST, path: Path) -> list[SmellFinding]:
    """Flag classes exceeding the pylint method or attribute ceilings."""
    classes = (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    return [f for node in classes for f in _class_ceiling_findings(node, path)]


def _class_ceiling_findings(node: ast.ClassDef, path: Path) -> list[SmellFinding]:
    """Return ceiling breaches for one class."""
    checks = [(len(_class_methods(node)), MAX_CLASS_METHODS, "methods")]
    if not _is_data_carrier(node):
        checks.append((len(_class_attribute_names(node)), MAX_CLASS_ATTRIBUTES, "instance attributes"))
    return [
        SmellFinding(
            smell=Smell.GOD_CLASS, file_path=str(path), line_number=node.lineno,
            subject=node.name, measured=measured, threshold=threshold,
            detail=f"{measured} {noun}; responsibilities accumulate faster than they are split",
        )
        for measured, threshold, noun in checks
        if measured > threshold
    ]


def _cohesion_components(node: ast.ClassDef) -> int:
    """Return LCOM4: the number of disjoint method-attribute clusters in a class.

    Methods are connected when they touch a shared attribute or call one another. A class
    whose methods form two disconnected clusters is two classes sharing a name; LCOM4 is
    the count of those clusters, so 1 is cohesive and anything higher is a seam.
    """
    methods = _class_methods(node)
    if len(methods) < 2:
        return 1
    method_names = {m.name for m in methods}
    touched = {
        m.name: _class_attribute_references(m) | {
            c.func.attr for c in ast.walk(m)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr in method_names
        }
        for m in methods
    }
    return _count_disjoint_clusters(touched)


def _count_disjoint_clusters(touched: dict[str, set[str]]) -> int:
    """Count connected components among methods linked by shared symbols.

    The linking relation has to be symmetric. "A shares an attribute with B" already is,
    but "A calls B" is not, and testing only that direction made membership depend on
    which method the traversal happened to reach first: the same unchanged class scored
    LCOM4 5 on one run and 7 on the next, because `set.pop()` returns an arbitrary
    element and string hashing is randomised per process. A metric that answers
    differently for identical input cannot support a judgement about a class.

    Traversal order is fixed as well, so that anything derived from these clusters later
    is reproducible rather than merely counted reproducibly.
    """
    unvisited, clusters = dict.fromkeys(sorted(touched)), 0
    while unvisited:
        _drain_cluster(next(iter(unvisited)), unvisited, touched)
        clusters += 1
    return clusters


def _drain_cluster(start: str, unvisited: dict[str, None], touched: dict[str, set[str]]) -> None:
    """Remove every method reachable from `start` from the unvisited set."""
    del unvisited[start]
    frontier = [start]
    while frontier:
        for other in _linked_methods(frontier.pop(), unvisited, touched):
            del unvisited[other]
            frontier.append(other)


def _linked_methods(
    current: str, unvisited: dict[str, None], touched: dict[str, set[str]]
) -> list[str]:
    """Return the still-unvisited methods linked to `current`, in stable order."""
    return [
        other
        for other in unvisited
        if touched[other] & touched[current]
        or other in touched[current]
        or current in touched[other]
    ]


def detect_low_cohesion(tree: ast.AST, path: Path) -> list[SmellFinding]:
    """Flag classes whose methods split into disconnected clusters (LCOM4 > 1)."""
    findings = []
    for node in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        if len(_class_methods(node)) < 3:
            continue
        components = _cohesion_components(node)
        if components > 1:
            findings.append(SmellFinding(
                smell=Smell.LOW_COHESION, file_path=str(path), line_number=node.lineno,
                subject=node.name, measured=components, threshold=1,
                detail=f"LCOM4 = {components}; the methods form {components} groups that share nothing",
            ))
    return findings


def _normalize_for_clone(node: ast.AST) -> str:
    """Fingerprint a statement by structure alone, erasing identifiers and literals.

    This is Type-2 clone detection: blocks differing only in names or constants produce
    the same fingerprint. Type-1 (byte-identical) detection misses the copies people
    actually make, because the first thing anyone changes after pasting is a name.

    The shape is taken from the node tree rather than from `ast.dump` text. Erasing
    characters inside quotes leaves a placeholder whose *length* still encodes the
    identifier, so `step0` and `phase0` fingerprint differently — a renaming defeats the
    detector built to survive renaming.
    """
    children = " ".join(_normalize_for_clone(child) for child in ast.iter_child_nodes(node))
    return f"({type(node).__name__} {children})" if children else f"({type(node).__name__})"


def _statement_windows(
    tree: ast.AST, path: Path, size: int
) -> Iterable[tuple[str, str, int]]:
    """Yield (fingerprint, subject, line) for each sliding window of sibling statements.

    Fingerprint and node mass are computed once per statement and then combined per
    window. Computing them per window instead re-walks every statement once for each of
    the `size` windows containing it, which was 39% of the tool's total runtime.
    """
    bodies = (
        (getattr(node, "name", type(node).__name__), getattr(node, "body", None))
        for node in ast.walk(tree)
    )
    scopes = ((s, b) for s, b in bodies if isinstance(b, list) and len(b) >= size)
    for subject, body in scopes:
        yield from _windows_in_scope(body, f"{path.name}:{subject}", size)


def _windows_in_scope(
    body: list[ast.stmt], subject: str, size: int
) -> Iterable[tuple[str, str, int]]:
    """Yield qualifying windows within one statement list."""
    prints = [_normalize_for_clone(stmt) for stmt in body]
    masses = [sum(1 for _ in ast.walk(stmt)) for stmt in body]
    starts = (
        s for s in range(len(body) - size + 1)
        if sum(masses[s : s + size]) >= MIN_CLONE_NODE_MASS
    )
    for start in starts:
        yield "|".join(prints[start : start + size]), subject, body[start].lineno


def detect_duplicated_blocks(
    trees: dict[Path, ast.AST], size: int = MIN_CLONE_STATEMENTS
) -> list[SmellFinding]:
    """Flag statement sequences repeated across the corpus, ignoring names and literals."""
    groups: dict[str, list[tuple[Path, str, int]]] = defaultdict(list)
    for path, tree in trees.items():
        for fingerprint, subject, line in _statement_windows(tree, path, size):
            groups[fingerprint].append((path, subject, line))

    findings: list[SmellFinding] = []
    for occurrences in groups.values():
        sites = {(p, line) for p, _, line in occurrences}
        if len(sites) < 2:
            continue
        path, subject, line = occurrences[0]
        others = ", ".join(f"{p.name}:{ln}" for p, _, ln in occurrences[1:4])
        findings.append(SmellFinding(
            smell=Smell.DUPLICATED_BLOCK, file_path=str(path), line_number=line,
            subject=subject, measured=len(sites), threshold=1,
            detail=f"{size}-statement block repeated at {others}, identical once names are erased",
        ))
    return _merge_overlapping_clones(findings, size)


def _merge_overlapping_clones(findings: list[SmellFinding], size: int) -> list[SmellFinding]:
    """Collapse sliding windows that overlap into one reported clone.

    A window advancing one statement at a time reports the same copied region repeatedly.
    PMD-CPD reports maximal non-overlapping matches for the same reason: the reader wants
    one duplicate, not one per offset into it.
    """
    kept: list[SmellFinding] = []
    for finding in sorted(findings, key=lambda f: (f.file_path, f.line_number)):
        overlaps = any(
            k.file_path == finding.file_path and abs(k.line_number - finding.line_number) < size
            for k in kept
        )
        if not overlaps:
            kept.append(finding)
    return kept


def _local_imports(tree: ast.AST, known: set[str]) -> set[str]:
    """Return imported module stems that belong to the scanned corpus."""
    stems = {s for node in ast.walk(tree) for s in _import_stems(node)}
    return stems & known


def _import_stems(node: ast.AST) -> set[str]:
    """Return the module stems one import node names."""
    if isinstance(node, ast.ImportFrom):
        return {node.module.split(".")[-1]} if node.module else set()
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[-1] for alias in node.names}
    return set()


def _cycles_in(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return the elementary cycles of a directed graph.

    Delegated to networkx rather than hand-rolled. The version this replaces was a
    depth-first walk that cleared its own stack mid-iteration to emit "one representative
    cycle per group" — a heuristic, at the nesting ceiling, approximating an algorithm
    that has a correct published implementation. networkx carries no hard dependencies,
    so the proportionality test in `AGENTS.md` §1a is satisfied.
    """
    digraph = networkx.DiGraph((src, dst) for src, targets in graph.items() for dst in targets)
    return [list(cycle) for cycle in networkx.simple_cycles(digraph)]


def detect_import_cycles(trees: dict[Path, ast.AST]) -> list[SmellFinding]:
    """Flag cycles in the local import graph, which force whole-group loading."""
    known = {p.stem for p in trees}
    graph = {p.stem: _local_imports(t, known) for p, t in trees.items()}
    by_stem = {p.stem: p for p in trees}
    cycles = _cycles_in(graph)
    return [
        SmellFinding(
            smell=Smell.IMPORT_CYCLE, file_path=str(by_stem[cycle[0]]), line_number=1,
            subject=" -> ".join([*cycle, cycle[0]]), measured=len(cycle), threshold=0,
            detail="Cyclic imports force the whole group to load together and resist extraction",
        )
        for cycle in cycles
    ]


def detect_unreferenced_symbols(
    paths: Sequence[Path], min_confidence: int = VULTURE_MIN_CONFIDENCE
) -> list[SmellFinding]:
    """Report unreferenced code using vulture, carrying its confidence as the measurement.

    This was hand-written first and saw less: it examined module-level definitions only,
    so an unused import or an unused loop variable was invisible to it, and it graded
    every finding the same. Vulture reports unused imports, variables, attributes,
    properties and unreachable code, each with a confidence, which is the number a reader
    needs to decide whether deletion is safe.
    """
    scanner = Vulture(verbose=False)
    scanner.scavenge([str(p) for p in paths])
    return [
        SmellFinding(
            smell=Smell.UNREFERENCED_SYMBOL,
            file_path=str(item.filename),
            line_number=item.first_lineno,
            subject=f"{item.typ} {item.name}",
            measured=item.confidence,
            threshold=min_confidence,
            detail=f"Unused {item.typ}, {item.confidence}% confidence per vulture",
        )
        for item in scanner.get_unused_code(min_confidence=min_confidence)
    ]


# Smells whose measurement is sound but whose *action* requires judgement. They are
# reported and scored, and they never fail a build. LCOM4 flags any facade of independent
# checkers; normalized clone detection flags deliberate boilerplate; dead-symbol detection
# cannot see reflective access; maintainability index is dominated by module size. Each is
# worth knowing and none is worth a red build on its own.
ADVISORY_SMELLS: Final[frozenset[Smell]] = frozenset({
    Smell.LOW_COHESION,
    Smell.DUPLICATED_BLOCK,
    Smell.UNREFERENCED_SYMBOL,
    Smell.LOW_MAINTAINABILITY,
})

PER_FILE_DETECTORS: Final[tuple[Callable[[ast.AST, Path], list[SmellFinding]], ...]] = (
    detect_long_parameter_lists,
    detect_long_functions,
    detect_god_classes,
    detect_low_cohesion,
)
# Per-file detectors whose findings are advisory, and so are skipped when a caller only
# wants what gates.
ADVISORY_DETECTORS: Final[frozenset[Callable[[ast.AST, Path], list[SmellFinding]]]] = frozenset(
    {detect_low_cohesion}
)


@dataclass
class ModuleScore:
    """Per-module quantification, independent of any threshold."""

    path: str
    loc: int
    halstead_volume: float
    max_complexity: int
    maintainability: float


@dataclass
class SmellReport:
    """The quantified state of a corpus."""

    modules: list[ModuleScore] = field(default_factory=list)
    findings: list[SmellFinding] = field(default_factory=list)

    @property
    def gating(self) -> list[SmellFinding]:
        """Return findings that describe a structural defect rather than a tension."""
        return [f for f in self.findings if f.smell not in ADVISORY_SMELLS]

    @property
    def advisory(self) -> list[SmellFinding]:
        """Return findings worth knowing that must never fail a build on their own."""
        return [f for f in self.findings if f.smell in ADVISORY_SMELLS]

    @property
    def mean_maintainability(self) -> float:
        """Return the corpus mean maintainability index."""
        scores = [m.maintainability for m in self.modules]
        return round(sum(scores) / len(scores), 1) if scores else 0.0

    def counts(self) -> dict[str, int]:
        """Return a finding count per smell, for trending across runs."""
        tally: dict[str, int] = defaultdict(int)
        for finding in self.findings:
            tally[finding.smell.value] += 1
        return dict(sorted(tally.items()))


def _module_scores(path: Path, source: str) -> list[ModuleScore]:
    """Quantify one module with radon, or report and skip it when radon cannot read it.

    Returns zero or one score, because the module may not be scorable. `_parse_module`
    guards the corpus with `ast.parse`, and radon tokenizes independently — the two do not
    accept the same language. A module holding a form feed inside a string literal parses
    as valid Python and raises `SyntaxError` out of radon's raw analyser, which previously
    aborted the entire repository scan from one file. A quantifier that stops at the first
    module it cannot score reports nothing about the hundreds it could.

    The skip is announced rather than swallowed: a module silently missing from the score
    table is indistinguishable from one that scored well. Found by
    `tools/fuzz_harness.py`; the input is kept as a regression case.
    """
    try:
        maintainability, volume, complexity = module_metrics(source)
    except (SyntaxError, ValueError) as err:
        print(f"⚠️  {path}: not scored, radon could not read it ({err})", file=sys.stderr)
        return []
    loc = len([ln for ln in source.splitlines() if ln.strip() and not ln.strip().startswith("#")])
    return [
        ModuleScore(
            path=str(path), loc=loc, halstead_volume=volume, max_complexity=complexity,
            maintainability=maintainability,
        )
    ]


def _parse_module(file_path: Path) -> tuple[str, ast.AST] | None:
    """Read and parse a module, or return None when it cannot be read or parsed."""
    try:
        source = file_path.read_text(encoding="utf-8")
        return source, ast.parse(source, filename=str(file_path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def analyze(paths: Sequence[Path], include_advisory: bool = True) -> SmellReport:
    """Quantify every Python module under the supplied targets.

    `include_advisory=False` computes only what gates. Measured on this corpus the
    advisory detectors are 94% of the runtime — vulture alone is roughly eight seconds
    against one for everything that gates — so a caller that consumes `report.gating` and
    discards the rest should not pay for the rest. The self-improvement loop is exactly
    such a caller.
    """
    report = SmellReport()
    trees: dict[Path, ast.AST] = {}
    detectors = [
        d for d in PER_FILE_DETECTORS if include_advisory or d not in ADVISORY_DETECTORS
    ]
    for file_path in _python_files(paths):
        parsed = _parse_module(file_path)
        if parsed is None:
            continue
        source, tree = parsed
        trees[file_path] = tree
        if include_advisory:
            report.modules.extend(_module_scores(file_path, source))
        report.findings.extend(f for d in detectors for f in d(tree, file_path))

    report.findings.extend(detect_import_cycles(trees))
    if not include_advisory:
        return report

    report.findings.extend(detect_duplicated_blocks(trees))
    report.findings.extend(detect_unreferenced_symbols(list(paths)))
    report.findings.extend(
        SmellFinding(
            smell=Smell.LOW_MAINTAINABILITY, file_path=m.path, line_number=1,
            subject=Path(m.path).name, measured=m.maintainability,
            threshold=LOW_MAINTAINABILITY_INDEX,
            detail=f"MI {m.maintainability} over {m.loc} lines; dominated by module size",
        )
        for m in report.modules
        if m.maintainability < LOW_MAINTAINABILITY_INDEX
    )
    return report


def _python_files(paths: Sequence[Path]) -> list[Path]:
    """Expand file and directory targets into a sorted list of Python modules."""
    expanded = (
        [p] if p.is_file() and p.suffix == ".py" else sorted(p.rglob("*.py"))
        for p in paths if p.exists()
    )
    return [m for group in expanded for m in group if "__pycache__" not in m.parts]


def render(report: SmellReport) -> list[str]:
    """Render the quantified report."""
    lines = [
        "=" * 82,
        "🔬 CODE SMELL QUANTIFIER",
        "=" * 82,
        f"Modules: {len(report.modules)}   Mean maintainability index: {report.mean_maintainability}"
        f"   (A >= {LOW_MAINTAINABILITY_INDEX:.0f})",
        "-" * 82,
    ]
    lines.extend(f"  {smell:<22} {count:>4}" for smell, count in report.counts().items())
    lines.append("-" * 82)
    for label, group in (("GATING", report.gating), ("ADVISORY", report.advisory)):
        lines.append(f"{label}: {len(group)}")
        lines.extend(
            f"  [{f.smell.value}] {f.file_path}:{f.line_number} {f.subject} "
            f"— {f.measured:g} vs {f.threshold:g}: {f.detail}"
            for f in sorted(group, key=lambda x: -x.excess)[:10]
        )
    lines.append("=" * 82)
    return lines


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    parser = argparse.ArgumentParser(description="Deterministic code smell quantifier")
    parser.add_argument("paths", nargs="*", default=[], help="Files or directories to analyze")
    parser.add_argument(
        "--fail-on", choices=("none", "gating", "any"), default="gating",
        help="Which findings cause a non-zero exit (default: gating)",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    return parser


def _resolve_targets(raw_paths: Sequence[str]) -> tuple[list[Path], list[Path]]:
    """Split requested targets into those that exist and those that do not.

    A path that does not exist is reported and then excluded, never silently skipped: a
    gate that certifies a typo as clean is worse than one that refuses to run.
    """
    targets = [Path(raw) for raw in raw_paths] or [Path(".")]
    missing = [target for target in targets if not target.exists()]
    for target in missing:
        print(f"MISSING {target} — refusing to certify a path that does not exist.", file=sys.stderr)
    return [target for target in targets if target.exists()], missing


def _render_json(report: SmellReport) -> str:
    """Serialize the report for trending across runs."""
    return json.dumps({
        "mean_maintainability": report.mean_maintainability,
        "counts": report.counts(),
        "modules": [vars(m) for m in report.modules],
        "findings": [vars(f) | {"smell": f.smell.value} for f in report.findings],
    }, indent=2)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI runner for the code smell quantifier."""
    args = build_arg_parser().parse_args(list(argv[1:]) if argv is not None else None)
    present, missing = _resolve_targets(args.paths)
    report = analyze(present)
    print(_render_json(report) if args.json else "\n".join(render(report)))

    blocking = {"none": [], "gating": report.gating, "any": report.findings}[args.fail_on]
    return 1 if blocking or missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
