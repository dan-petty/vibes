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
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable, Final, Iterable, Sequence

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


@dataclass
class HalsteadMetrics:
    """Halstead's operator/operand counts, computed over AST nodes.

    The original is defined over lexical tokens. Counting AST operator nodes and
    Name/Constant operands is the approximation radon also makes; it preserves the
    metric's ordering behaviour, which is what a trend needs, and the docstring says so
    rather than implying token fidelity.
    """

    distinct_operators: int = 0
    distinct_operands: int = 0
    total_operators: int = 0
    total_operands: int = 0

    @property
    def vocabulary(self) -> int:
        """Return n, the distinct operator and operand count."""
        return self.distinct_operators + self.distinct_operands

    @property
    def length(self) -> int:
        """Return N, the total operator and operand count."""
        return self.total_operators + self.total_operands

    @property
    def volume(self) -> float:
        """Return V = N log2(n), the size of the implementation in bits."""
        return 0.0 if self.vocabulary <= 1 else round(self.length * math.log2(self.vocabulary), 2)


_OPERATOR_NODES: Final[tuple[type[ast.AST], ...]] = (
    ast.BinOp, ast.BoolOp, ast.UnaryOp, ast.Compare, ast.Call, ast.Attribute,
    ast.Subscript, ast.Assign, ast.AugAssign, ast.Return, ast.Raise, ast.Assert,
    ast.If, ast.For, ast.While, ast.With, ast.Lambda, ast.Await, ast.Yield,
)


def _classify_halstead_token(node: ast.AST) -> tuple[str, str]:
    """Classify a node as a Halstead operator, operand, or neither."""
    if isinstance(node, _OPERATOR_NODES):
        return "operator", type(node).__name__
    if isinstance(node, ast.Name):
        return "operand", node.id
    if isinstance(node, ast.Constant):
        return "operand", repr(node.value)
    return "", ""


def compute_halstead(tree: ast.AST) -> HalsteadMetrics:
    """Measure Halstead operator and operand counts across a syntax tree."""
    classified = [_classify_halstead_token(node) for node in ast.walk(tree)]
    operators = [value for kind, value in classified if kind == "operator"]
    operands = [value for kind, value in classified if kind == "operand"]
    return HalsteadMetrics(
        distinct_operators=len(set(operators)),
        distinct_operands=len(set(operands)),
        total_operators=len(operators),
        total_operands=len(operands),
    )


def maintainability_index(halstead: HalsteadMetrics, complexity: int, loc: int) -> float:
    """Return the normalized 0-100 maintainability index used by radon.

    MI = max(0, (171 - 5.2 ln(V) - 0.23 G - 16.2 ln(LOC)) * 100 / 171)

    Below 65 is radon's B/C boundary: still shippable, measurably harder to change.
    """
    volume = max(halstead.volume, 1.0)
    lines = max(loc, 1)
    raw = 171.0 - 5.2 * math.log(volume) - 0.23 * complexity - 16.2 * math.log(lines)
    return round(max(0.0, raw * 100.0 / 171.0), 1)


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


def _class_attribute_names(node: ast.ClassDef) -> set[str]:
    """Return instance attribute names assigned anywhere in a class body."""
    targets = (
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
        and child.value.id == "self"
    )
    return set(targets)


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
    findings = []
    for node in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        methods, attributes = len(_class_methods(node)), len(_class_attribute_names(node))
        checks = [(methods, MAX_CLASS_METHODS, "methods")]
        if not _is_data_carrier(node):
            checks.append((attributes, MAX_CLASS_ATTRIBUTES, "instance attributes"))
        for measured, threshold, noun in checks:
            if measured > threshold:
                findings.append(SmellFinding(
                    smell=Smell.GOD_CLASS, file_path=str(path), line_number=node.lineno,
                    subject=node.name, measured=measured, threshold=threshold,
                    detail=f"{measured} {noun}; responsibilities accumulate faster than they are split",
                ))
    return findings


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
        m.name: _class_attribute_names(m) | {
            c.func.attr for c in ast.walk(m)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr in method_names
        }
        for m in methods
    }
    return _count_disjoint_clusters(touched)


def _count_disjoint_clusters(touched: dict[str, set[str]]) -> int:
    """Count connected components among methods linked by shared symbols."""
    unvisited, clusters = set(touched), 0
    while unvisited:
        frontier, cluster = {unvisited.pop()}, set()
        while frontier:
            current = frontier.pop()
            cluster.add(current)
            shared = {
                other for other in unvisited
                if touched[other] & touched[current] or other in touched[current]
            }
            frontier |= shared
            unvisited -= shared
        clusters += 1
    return clusters


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


def _node_mass(window: Sequence[ast.stmt]) -> int:
    """Return the total AST node count of a statement window."""
    return sum(len(list(ast.walk(stmt))) for stmt in window)


def _statement_windows(
    tree: ast.AST, path: Path, size: int
) -> Iterable[tuple[str, str, int]]:
    """Yield (fingerprint, subject, line) for each sliding window of sibling statements."""
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or len(body) < size:
            continue
        subject = getattr(node, "name", type(node).__name__)
        windows = (body[start : start + size] for start in range(len(body) - size + 1))
        substantial = (w for w in windows if _node_mass(w) >= MIN_CLONE_NODE_MASS)
        for window in substantial:
            fingerprint = "|".join(_normalize_for_clone(stmt) for stmt in window)
            yield fingerprint, f"{path.name}:{subject}", window[0].lineno


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
    stems: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            stems.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Import):
            stems.update(alias.name.split(".")[-1] for alias in node.names)
    return stems & known


def detect_import_cycles(trees: dict[Path, ast.AST]) -> list[SmellFinding]:
    """Flag cycles in the local import graph, which force whole-group loading."""
    known = {p.stem for p in trees}
    graph = {p.stem: _local_imports(t, known) for p, t in trees.items()}
    by_stem = {p.stem: p for p in trees}
    cycles = _find_cycles(graph)
    return [
        SmellFinding(
            smell=Smell.IMPORT_CYCLE, file_path=str(by_stem[cycle[0]]), line_number=1,
            subject=" -> ".join(cycle + [cycle[0]]), measured=len(cycle), threshold=0,
            detail="Cyclic imports force the whole group to load together and resist extraction",
        )
        for cycle in cycles
    ]


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return one representative cycle per strongly connected group."""
    seen: set[str] = set()
    cycles: list[list[str]] = []
    for start in sorted(graph):
        if start in seen:
            continue
        stack = [(start, [start])]
        while stack:
            node, path_so_far = stack.pop()
            for nxt in sorted(graph.get(node, set())):
                if nxt == start and len(path_so_far) > 1:
                    cycles.append(path_so_far)
                    seen.update(path_so_far)
                    stack = []
                    break
                if nxt not in path_so_far and nxt not in seen:
                    stack.append((nxt, path_so_far + [nxt]))
    return cycles


def _module_level_symbols(tree: ast.AST) -> dict[str, int]:
    """Return public module-level function and class names with their line numbers."""
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    body = getattr(tree, "body", [])
    return {n.name: n.lineno for n in body if isinstance(n, kinds) and not n.name.startswith("_")}


def _node_reference(node: ast.AST) -> str | None:
    """Return the name a single node references, by any means, or None."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _referenced_names(trees: dict[Path, ast.AST]) -> set[str]:
    """Return every name referenced anywhere in the corpus, by any means."""
    references = (
        _node_reference(node) for tree in trees.values() for node in ast.walk(tree)
    )
    return {name for name in references if name is not None}


def _is_convention_discovered(path: Path) -> bool:
    """Return True for modules whose contents a runner discovers by naming convention.

    Nothing references a pytest test function by name; the collector finds it by prefix.
    Reporting such functions as dead would be a detector that recommends deleting the
    test suite, which is the failure mode worth designing against rather than explaining.
    """
    return path.name.startswith("test_") or path.name.endswith("_test.py")


def detect_unreferenced_symbols(
    trees: dict[Path, ast.AST], entry_points: Sequence[str] = ("main",)
) -> list[SmellFinding]:
    """Flag public module-level symbols no file in the corpus references.

    Deliberately conservative: string constants count as references, because a name
    reachable only through `getattr` or a registry lookup is still reachable. Over-reporting
    dead code costs more than missing some, since the suggested action is deletion.
    """
    referenced = _referenced_names(trees)
    findings: list[SmellFinding] = []
    for path, tree in trees.items():
        if _is_convention_discovered(path):
            continue
        for name, line in _module_level_symbols(tree).items():
            if name in entry_points or name in referenced or name.startswith("test_"):
                continue
            findings.append(SmellFinding(
                smell=Smell.UNREFERENCED_SYMBOL, file_path=str(path), line_number=line,
                subject=name, measured=0, threshold=1,
                detail="Defined and exported, referenced nowhere in the scanned corpus",
            ))
    return findings


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


def _max_complexity(tree: ast.AST) -> int:
    """Return the highest branch count of any function in a module."""
    branch_kinds = (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.Assert, ast.IfExp, ast.BoolOp)
    scores = [
        1 + sum(1 for c in ast.walk(fn) if isinstance(c, branch_kinds))
        for fn in _function_nodes(tree)
    ]
    return max(scores, default=1)


def _score_module(path: Path, tree: ast.AST, source: str) -> ModuleScore:
    """Quantify one module, before any threshold is applied."""
    loc = len([ln for ln in source.splitlines() if ln.strip() and not ln.strip().startswith("#")])
    halstead = compute_halstead(tree)
    complexity = _max_complexity(tree)
    return ModuleScore(
        path=str(path), loc=loc, halstead_volume=halstead.volume, max_complexity=complexity,
        maintainability=maintainability_index(halstead, complexity, loc),
    )


def analyze(paths: Sequence[Path]) -> SmellReport:
    """Quantify every Python module under the supplied targets."""
    report = SmellReport()
    trees: dict[Path, ast.AST] = {}
    for file_path in _python_files(paths):
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(file_path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        trees[file_path] = tree
        report.modules.append(_score_module(file_path, tree, source))
        for detector in PER_FILE_DETECTORS:
            report.findings.extend(detector(tree, file_path))

    report.findings.extend(detect_duplicated_blocks(trees))
    report.findings.extend(detect_import_cycles(trees))
    report.findings.extend(detect_unreferenced_symbols(trees))
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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI runner for the code smell quantifier."""
    args = build_arg_parser().parse_args(list(argv[1:]) if argv is not None else None)
    targets = [Path(raw) for raw in args.paths] or [Path(".")]
    missing = [t for t in targets if not t.exists()]
    for target in missing:
        print(f"MISSING {target} — refusing to certify a path that does not exist.", file=sys.stderr)

    report = analyze([t for t in targets if t.exists()])
    if args.json:
        import json

        print(json.dumps({
            "mean_maintainability": report.mean_maintainability,
            "counts": report.counts(),
            "modules": [vars(m) for m in report.modules],
            "findings": [vars(f) | {"smell": f.smell.value} for f in report.findings],
        }, indent=2))
    else:
        print("\n".join(render(report)))

    blocking = {"none": [], "gating": report.gating, "any": report.findings}[args.fail_on]
    return 1 if blocking or missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
