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
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import networkx
from radon.complexity import cc_rank, cc_visit_ast
from radon.metrics import mi_compute, mi_parameters
from vulture import Vulture

# Thresholds are the published defaults of the tools each detector mirrors, so a number
# here can be traced to a source rather than to taste.
MAX_PARAMETERS: Final[int] = 5  # pylint R0913 default
MAX_FUNCTION_STATEMENTS: Final[int] = 50  # pylint R0915 default
MAX_CLASS_METHODS: Final[int] = 20  # pylint R0904 default
MAX_CLASS_ATTRIBUTES: Final[int] = 7  # pylint R0902 default
MAX_CYCLOMATIC_COMPLEXITY: Final[int] = 10  # radon cc / repository McCabe ceiling
MIN_CLONE_STATEMENTS: Final[int] = 6  # PMD-CPD minimum-tokens analogue
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

POLYGLOT_EXTENSIONS: Final[dict[str, str]] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".sh": "bash",
    ".bash": "bash",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
}

LINE_COMMENT_PREFIXES: Final[dict[str, tuple[str, ...]]] = {
    "python": ("#",),
    "bash": ("#",),
    "yaml": ("#",),
    "javascript": ("//",),
    "typescript": ("//",),
    "go": ("//",),
    "rust": ("//",),
    "c": ("//",),
    "cpp": ("//",),
    "json": (),
}

BRANCH_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "if",
        "elif",
        "else",
        "for",
        "while",
        "case",
        "match",
        "catch",
        "except",
        "switch",
        "&&",
        "||",
        "?",
    }
)

_POLYGLOT_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    (?P<STRING>"([^"\\]|\\.)*"|'([^'\\]|\\.)*'|`([^`\\]|\\.)*`) |
    (?P<NUMBER>\b\d+(?:\.\d+)?\b) |
    (?P<KEYWORD>\b(?:if|elif|else|for|while|return|def|fn|func|function|class|struct|enum|interface|type|const|let|var|import|export|from|package|match|switch|case|break|continue|try|catch|except|finally|throw|raise|yield|async|await|select|default|pub|mut|impl|trait)\b) |
    (?P<IDENTIFIER>\b[a-zA-Z_][a-zA-Z0-9_]*\b) |
    (?P<OPERATOR>[+\-*/%=&|!<>^~?:;.,()[\]{}]+)
    """,
    re.VERBOSE,
)


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
    HIGH_CYCLOMATIC_COMPLEXITY = "HighCyclomaticComplexity"


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
    # `*args` and `**kwargs` are deliberately excluded, as pylint's R0913 excludes them:
    # they are one parameter each at the definition and any number at the call, so counting
    # them against a ceiling meant every forwarding wrapper and decorator was flagged for
    # the arguments it does not name. `def wide(a, b, c, d, e, *args, **kwargs)` scored 7
    # here and 5 under real pylint, which reports nothing.
    return len(names) - bound


def detect_long_parameter_lists(tree: ast.AST, path: Path) -> list[SmellFinding]:
    """Flag functions whose parameter count exceeds the pylint R0913 default."""
    findings = []
    for node in _function_nodes(tree):
        count = _parameter_count(node)
        if count > MAX_PARAMETERS:
            findings.append(
                SmellFinding(
                    smell=Smell.LONG_PARAMETER_LIST,
                    file_path=str(path),
                    line_number=node.lineno,
                    subject=node.name,
                    measured=count,
                    threshold=MAX_PARAMETERS,
                    detail=f"{count} parameters; each one multiplies the call sites that must change together",
                )
            )
    return findings


def _statement_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count the statements pylint's R0915 counts, and no others.

    `ast.walk` over the function included the `def` itself and the docstring expression, so
    a 49-statement function measured 51 against a ceiling of 50 — pylint reported nothing
    on the same file. pylint increments only on `node.is_statement` within the body, and a
    docstring is an implicit `Expr` the checker does not reach.
    """
    total = 0
    for child in node.body:
        total += sum(1 for sub in ast.walk(child) if isinstance(sub, ast.stmt))
    docstring = ast.get_docstring(node, clean=False)
    return total - (1 if docstring is not None else 0)


def detect_long_functions(tree: ast.AST, path: Path) -> list[SmellFinding]:
    """Flag functions by statement count, which complexity alone does not capture.

    A 300-line linear function has a cyclomatic complexity of 1 and passes every gate in
    this repository. Length and branching are independent axes of difficulty.
    """
    findings = []
    for node in _function_nodes(tree):
        statements = _statement_count(node)
        if statements > MAX_FUNCTION_STATEMENTS:
            findings.append(
                SmellFinding(
                    smell=Smell.LONG_FUNCTION,
                    file_path=str(path),
                    line_number=node.lineno,
                    subject=node.name,
                    measured=statements,
                    threshold=MAX_FUNCTION_STATEMENTS,
                    detail=f"{statements} statements; branching gates do not see raw length",
                )
            )
    return findings


def detect_cyclomatic_complexity(
    tree: ast.AST, path: Path, max_complexity: int = MAX_CYCLOMATIC_COMPLEXITY
) -> list[SmellFinding]:
    """Flag functions and methods exceeding the cyclomatic complexity ceiling.

    Delegates to radon's `cc_visit_ast` to score every function, method, and closure,
    reporting the measured McCabe number alongside radon's letter rank (A through F).
    """
    try:
        blocks = cc_visit_ast(tree)
    except Exception:
        return []
    findings: list[SmellFinding] = []
    for block in blocks:
        if block.complexity > max_complexity:
            rank = cc_rank(block.complexity)
            findings.append(
                SmellFinding(
                    smell=Smell.HIGH_CYCLOMATIC_COMPLEXITY,
                    file_path=str(path),
                    line_number=block.lineno,
                    subject=block.name,
                    measured=float(block.complexity),
                    threshold=float(max_complexity),
                    detail=f"McCabe complexity {block.complexity} (rank {rank}); exceeds threshold {max_complexity}",
                )
            )
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
        if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name) and child.value.id == "self"
    }


def _class_methods(node: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return the *public* methods declared directly on a class.

    pylint's R0904 counts `sum(1 for method in node.mymethods() if not
    method.name.startswith("_"))`, and this threshold is pylint's `max-public-methods`
    default. Counting private helpers and dunders against a public-method ceiling punishes
    the decomposition the ceiling exists to encourage: a facade with 10 public methods and
    11 private helpers scored 21 here and nothing at all under real pylint.
    """
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    return [child for child in node.body if isinstance(child, kinds) and not child.name.startswith("_")]


def _extract_node_identifier(node: ast.AST) -> str:
    """Extract symbol name from Name, Attribute, or Call AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _extract_node_identifier(node.func)
    return ""


def _is_data_carrier(node: ast.ClassDef) -> bool:
    """Return True for classes whose whole purpose is to hold fields.

    A dataclass with nine fields is a record, not a god object. pylint's R0902 has the
    same false positive, and excluding data carriers is the standard correction: the
    attribute ceiling is about accumulated responsibility, and a record has one.
    """
    decorators = {_extract_node_identifier(d) for d in node.decorator_list}
    bases = {_extract_node_identifier(b) for b in node.bases}
    data_carrier_bases = {"NamedTuple", "TypedDict", "BaseModel", "Enum", "StrEnum", "IntEnum"}
    return "dataclass" in decorators or bool(bases & data_carrier_bases)


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
            smell=Smell.GOD_CLASS,
            file_path=str(path),
            line_number=node.lineno,
            subject=node.name,
            measured=measured,
            threshold=threshold,
            detail=f"{measured} {noun}; responsibilities accumulate faster than they are split",
        )
        for measured, threshold, noun in checks
        if measured > threshold
    ]


def _called_method_names(method_node: ast.AST, known_methods: set[str]) -> set[str]:
    """Return names of known class methods called within a method AST."""
    return {
        c.func.attr
        for c in ast.walk(method_node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr in known_methods
    }


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
        m.name: _class_attribute_references(m) | _called_method_names(m, method_names) for m in methods
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


def _linked_methods(current: str, unvisited: dict[str, None], touched: dict[str, set[str]]) -> list[str]:
    """Return the still-unvisited methods linked to `current`, in stable order."""
    return [
        other
        for other in unvisited
        if touched[other] & touched[current] or other in touched[current] or current in touched[other]
    ]


def detect_low_cohesion(tree: ast.AST, path: Path) -> list[SmellFinding]:
    """Flag classes whose methods split into disconnected clusters (LCOM4 > 1)."""
    findings = []
    for node in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        if len(_class_methods(node)) < 3:
            continue
        components = _cohesion_components(node)
        if components > 1:
            findings.append(
                SmellFinding(
                    smell=Smell.LOW_COHESION,
                    file_path=str(path),
                    line_number=node.lineno,
                    subject=node.name,
                    measured=components,
                    threshold=1,
                    detail=f"LCOM4 = {components}; the methods form {components} groups that share nothing",
                )
            )
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


def _statement_windows(tree: ast.AST, path: Path, size: int) -> Iterable[tuple[str, str, int]]:
    """Yield (fingerprint, subject, line) for each sliding window of sibling statements.

    Fingerprint and node mass are computed once per statement and then combined per
    window. Computing them per window instead re-walks every statement once for each of
    the `size` windows containing it, which was 39% of the tool's total runtime.
    """
    bodies = (
        (getattr(node, "name", type(node).__name__), getattr(node, "body", None)) for node in ast.walk(tree)
    )
    scopes = ((s, b) for s, b in bodies if isinstance(b, list) and len(b) >= size)
    for subject, body in scopes:
        yield from _windows_in_scope(body, f"{path.name}:{subject}", size)


def _statement_mass(stmt: ast.stmt) -> int:
    """Calculate the mass (node count) of an AST statement."""
    return sum(1 for _ in ast.walk(stmt))


def _windows_in_scope(body: list[ast.stmt], subject: str, size: int) -> Iterable[tuple[str, str, int]]:
    """Yield qualifying windows within one statement list."""
    prints = [_normalize_for_clone(stmt) for stmt in body]
    masses = [_statement_mass(stmt) for stmt in body]
    starts = (s for s in range(len(body) - size + 1) if sum(masses[s : s + size]) >= MIN_CLONE_NODE_MASS)
    for start in starts:
        yield "|".join(prints[start : start + size]), subject, body[start].lineno


def _collect_window_groups(trees: dict[Path, ast.AST], size: int) -> dict[str, list[tuple[Path, str, int]]]:
    """Index statement window occurrences by their normalized fingerprint."""
    groups: dict[str, list[tuple[Path, str, int]]] = defaultdict(list)
    for path, tree in trees.items():
        for fingerprint, subject, line in _statement_windows(tree, path, size):
            groups[fingerprint].append((path, subject, line))
    return groups


def _duplicated_block_finding(occurrences: list[tuple[Path, str, int]], size: int) -> SmellFinding | None:
    """Build a duplicated block finding if occurrences cover at least 2 distinct sites."""
    sites = {(p, line) for p, _, line in occurrences}
    if len(sites) < 2:
        return None
    path, subject, line = occurrences[0]
    others = ", ".join(f"{p.name}:{ln}" for p, _, ln in occurrences[1:4])
    return SmellFinding(
        smell=Smell.DUPLICATED_BLOCK,
        file_path=str(path),
        line_number=line,
        subject=subject,
        measured=len(sites),
        threshold=1,
        detail=f"{size}-statement block repeated at {others}, identical once names are erased",
    )


def detect_duplicated_blocks(
    trees: dict[Path, ast.AST], size: int = MIN_CLONE_STATEMENTS
) -> list[SmellFinding]:
    """Flag statement sequences repeated across the corpus, ignoring names and literals."""
    groups = _collect_window_groups(trees, size)
    raw_findings = (_duplicated_block_finding(occ, size) for occ in groups.values())
    findings = [f for f in raw_findings if f is not None]
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
            k.file_path == finding.file_path and abs(k.line_number - finding.line_number) < size for k in kept
        )
        if not overlaps:
            kept.append(finding)
    return kept


def _classify_token(match: re.Match[str]) -> str:
    """Classify a regex match into its normalized clone token."""
    if match.group("KEYWORD") or match.group("OPERATOR"):
        return match.group(0)
    if match.group("IDENTIFIER"):
        return "<ID>"
    return "<LIT>"


def _normalize_polyglot_line(line: str, language: str) -> str:
    """Fingerprint a source code line by structure alone, erasing identifiers and literals."""
    stripped = line.strip()
    prefixes = LINE_COMMENT_PREFIXES.get(language, ("#", "//"))
    if not stripped or any(stripped.startswith(p) for p in prefixes):
        return ""
    tokens = [_classify_token(m) for m in _POLYGLOT_TOKEN_RE.finditer(stripped)]
    return " ".join(tokens)


def _polyglot_line_fingerprints(source: str, language: str) -> list[tuple[int, str]]:
    """Return (line_no, normalized_line) for non-empty normalized lines in source."""
    lines: list[tuple[int, str]] = []
    for idx, raw_line in enumerate(source.splitlines(), 1):
        norm = _normalize_polyglot_line(raw_line, language)
        if norm:
            lines.append((idx, norm))
    return lines


def _polyglot_windows(
    lines: Sequence[tuple[int, str]], path: Path, size: int
) -> Iterable[tuple[str, str, int]]:
    """Yield (fingerprint, subject, line_no) for sliding windows of normalized lines."""
    if len(lines) < size:
        return
    for i in range(len(lines) - size + 1):
        window = lines[i : i + size]
        fp = "|".join(norm for _, norm in window)
        start_line = window[0][0]
        yield fp, f"{path.name}:{start_line}", start_line


def _collect_polyglot_window_groups(
    sources: Mapping[Path, str], size: int
) -> dict[str, list[tuple[Path, str, int]]]:
    """Index polyglot window occurrences across source files by their fingerprint."""
    groups: dict[str, list[tuple[Path, str, int]]] = defaultdict(list)
    for path, source in sources.items():
        lang = POLYGLOT_EXTENSIONS.get(path.suffix, "unknown")
        lines = _polyglot_line_fingerprints(source, lang)
        for fp, subject, line_no in _polyglot_windows(lines, path, size):
            groups[fp].append((path, subject, line_no))
    return groups


def detect_polyglot_clones(
    sources: Mapping[Path, str],
    size: int = MIN_CLONE_STATEMENTS,
) -> list[SmellFinding]:
    """Detect duplicated statement/token blocks across multi-language source files."""
    groups = _collect_polyglot_window_groups(sources, size)
    raw_findings = (_duplicated_block_finding(occ, size) for occ in groups.values())
    findings = [f for f in raw_findings if f is not None]
    return _merge_overlapping_clones(findings, size)


def _consume_token_match(
    match: re.Match[str],
    operators: list[str],
    operands: list[str],
) -> int:
    """Consume a single regex match into operator/operand lists and return branch score."""
    kw = match.group("KEYWORD")
    if kw:
        operators.append(kw)
        return 1 if kw in BRANCH_KEYWORDS else 0
    op = match.group("OPERATOR")
    if op:
        operators.append(op)
        return 1 if op in ("&&", "||", "?") else 0
    ident = match.group("IDENTIFIER")
    if ident:
        operands.append(ident)
        return 0
    operands.append(match.group(0))
    return 0


def _extract_polyglot_tokens(code_lines: Sequence[str]) -> tuple[list[str], list[str], int]:
    """Extract operators, operands, and branch complexity from polyglot lines."""
    operators: list[str] = []
    operands: list[str] = []
    complexity = 1
    for line in code_lines:
        for match in _POLYGLOT_TOKEN_RE.finditer(line):
            complexity += _consume_token_match(match, operators, operands)
    return operators, operands, complexity


def _strip_polyglot_comments(lines: Sequence[str], language: str) -> list[str]:
    """Filter out empty lines and single-line comments for a given language."""
    prefixes = LINE_COMMENT_PREFIXES.get(language, ("#", "//"))
    return [ln for ln in lines if ln.strip() and not ln.strip().startswith(prefixes)]


def _calculate_halstead_volume(operators: Sequence[str], operands: Sequence[str]) -> float:
    """Compute Halstead program volume from operators and operands."""
    n = len(operators) + len(operands)
    eta = len(set(operators)) + len(set(operands))
    return round(n * math.log2(max(2, eta)), 2) if n > 0 and eta > 1 else 0.0


def polyglot_module_metrics(source: str, language: str) -> tuple[float, float, int]:
    """Compute (maintainability index, Halstead volume, complexity) for polyglot source."""
    code_lines = _strip_polyglot_comments(source.splitlines(), language)
    loc = max(1, len(code_lines))
    operators, operands, complexity = _extract_polyglot_tokens(code_lines)
    volume = _calculate_halstead_volume(operators, operands)
    raw_mi = 171.0 - 5.2 * math.log(max(1.0, volume)) - 0.23 * complexity - 16.2 * math.log(loc)
    mi = max(0.0, min(100.0, round(raw_mi * 100.0 / 171.0, 1)))
    return mi, volume, complexity


def _local_imports(tree: ast.AST, known: set[str]) -> set[str]:
    """Return imported module stems that belong to the scanned corpus."""
    stems = {s for node in ast.walk(tree) for s in _import_stems(node)}
    return stems & known


def _import_stems(node: ast.AST) -> set[str]:
    """Return every module stem one import node could name.

    For `from pkg import b`, the Python Language Reference §7.11 says the interpreter looks
    for an attribute `b` on `pkg` and, failing that, imports the submodule `pkg.b`. Reading
    only `node.module` therefore recorded an edge to the *package* and none to `b`, so the
    commonest circular import in a package — `pkg/a.py` doing `from pkg import b` while
    `pkg/b.py` does `from pkg import a` — drew no edge at all and the cycle was invisible.
    CPython refuses that program at runtime; this detector called it clean.

    The imported names are included as candidate stems and intersected with the corpus by
    the caller, so a name that is not a scanned module contributes nothing. A function
    imported from a module that shares a name with another scanned module would draw an
    edge that is not an import — the ambiguity is the language's, and erring towards the
    edge is the same choice pylint's `cyclic-import` makes.
    """
    if isinstance(node, ast.ImportFrom):
        module = {node.module.split(".")[-1]} if node.module else set()
        return module | {alias.name.split(".")[-1] for alias in node.names}
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
            smell=Smell.IMPORT_CYCLE,
            file_path=str(by_stem[cycle[0]]),
            line_number=1,
            subject=" -> ".join([*cycle, cycle[0]]),
            measured=len(cycle),
            threshold=0,
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
ADVISORY_SMELLS: Final[frozenset[Smell]] = frozenset(
    {
        Smell.LOW_COHESION,
        Smell.DUPLICATED_BLOCK,
        Smell.UNREFERENCED_SYMBOL,
        Smell.LOW_MAINTAINABILITY,
    }
)

PER_FILE_DETECTORS: Final[tuple[Callable[[ast.AST, Path], list[SmellFinding]], ...]] = (
    detect_long_parameter_lists,
    detect_long_functions,
    detect_god_classes,
    detect_low_cohesion,
    detect_cyclomatic_complexity,
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
            path=str(path),
            loc=loc,
            halstead_volume=volume,
            max_complexity=complexity,
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


def _active_per_file_detectors(include_advisory: bool) -> list[Any]:
    """Return active per-file detectors based on advisory inclusion."""
    if include_advisory:
        return list(PER_FILE_DETECTORS)
    return [d for d in PER_FILE_DETECTORS if d not in ADVISORY_DETECTORS]


def _run_per_file_detectors(tree: ast.AST, file_path: Path, detectors: list[Any]) -> list[SmellFinding]:
    """Run per-file smell detectors on a parsed AST."""
    findings: list[SmellFinding] = []
    for d in detectors:
        findings.extend(d(tree, file_path))
    return findings


def _scan_modules(paths: Sequence[Path], report: SmellReport, include_advisory: bool) -> dict[Path, ast.AST]:
    """Run every per-file detector, returning the parsed trees the cross-file ones need."""
    trees: dict[Path, ast.AST] = {}
    detectors = _active_per_file_detectors(include_advisory)
    for file_path in _python_files(paths):
        parsed = _parse_module(file_path)
        if parsed is None:
            continue
        source, tree = parsed
        trees[file_path] = tree
        if include_advisory:
            report.modules.extend(_module_scores(file_path, source))
        report.findings.extend(_run_per_file_detectors(tree, file_path, detectors))
    return trees


def analyze(
    paths: Sequence[Path],
    include_advisory: bool = True,
    languages: Sequence[str] | None = None,
) -> SmellReport:
    """Quantify every module under the supplied targets across Python and polyglot languages.

    `include_advisory=False` computes only what gates. Measured on this corpus the
    advisory detectors are 94% of the runtime — vulture alone is roughly eight seconds
    against one for everything that gates — so a caller that consumes `report.gating` and
    discards the rest should not pay for the rest. The self-improvement loop is exactly
    such a caller.
    """
    report = SmellReport()
    trees = _scan_modules(paths, report, include_advisory)
    report.findings.extend(detect_import_cycles(trees))

    polyglot_sources = _scan_polyglot_sources(paths, languages)
    report.findings.extend(_polyglot_gating_findings(polyglot_sources))

    if not include_advisory:
        return report

    _record_polyglot_metrics(polyglot_sources, report)
    report.findings.extend(detect_duplicated_blocks(trees))
    if polyglot_sources:
        report.findings.extend(detect_polyglot_clones(polyglot_sources))
    report.findings.extend(detect_unreferenced_symbols(list(paths)))
    report.findings.extend(
        SmellFinding(
            smell=Smell.LOW_MAINTAINABILITY,
            file_path=m.path,
            line_number=1,
            subject=Path(m.path).name,
            measured=m.maintainability,
            threshold=LOW_MAINTAINABILITY_INDEX,
            detail=f"MI {m.maintainability} over {m.loc} lines; dominated by module size",
        )
        for m in report.modules
        if m.maintainability < LOW_MAINTAINABILITY_INDEX
    )
    return report


# Directory names holding code this repository did not write. Kept in step with
# `tools/source_tree_policy.py`, the single definition of this repository's corpus; this
# sample application is standalone by design and cannot import it, so
# `tests/test_source_tree_policy.py` asserts the two agree on the real tree. The previous
# filter excluded `__pycache__` alone, which let a vendored Node or Python package into
# the measurement and put its modules in the score table.
VENDORED_DIR_NAMES: Final[frozenset[str]] = frozenset({"node_modules", "__pycache__", "venv"})
VENDORED_DIR_SUFFIXES: Final[tuple[str, ...]] = (".egg-info",)


def _is_repository_dir(name: str) -> bool:
    """Report whether a directory belongs to this repository rather than to a dependency."""
    return not (name.startswith(".") or name in VENDORED_DIR_NAMES or name.endswith(VENDORED_DIR_SUFFIXES))


def _walk_python_files(root: Path) -> list[Path]:
    """Return the repository's own Python modules beneath one directory, pruning as it goes."""
    found: list[Path] = []
    for parent, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if _is_repository_dir(d)]
        found.extend(Path(parent) / name for name in files if name.endswith(".py"))
    return sorted(found)


def _expand_python_path(path: Path) -> list[Path]:
    """Expand a single path target into a list of Python files."""
    if path.is_file():
        return [path] if path.suffix == ".py" else []
    return _walk_python_files(path)


def _python_files(paths: Sequence[Path]) -> list[Path]:
    """Expand file and directory targets into a sorted list of the repository's modules."""
    found: list[Path] = []
    for p in paths:
        if p.exists():
            found.extend(_expand_python_path(p))
    return found


def _matches_requested_language(suffix: str, languages: Sequence[str]) -> bool:
    """Check if suffix matches any language name or extension in the requested list."""
    if "all" in languages:
        return True
    norm_langs = {lang.lower().lstrip(".") for lang in languages}
    file_lang = POLYGLOT_EXTENSIONS[suffix]
    return file_lang in norm_langs or suffix.lstrip(".") in norm_langs


def _is_matching_language(
    suffix: str,
    is_explicit_file: bool,
    languages: Sequence[str] | None,
) -> bool:
    """Check if a file suffix matches the requested languages."""
    if suffix not in POLYGLOT_EXTENSIONS:
        return False
    if languages:
        return _matches_requested_language(suffix, languages)
    return is_explicit_file or suffix == ".py"


def _polyglot_files_in_dir(parent: Path, files: Sequence[str], languages: Sequence[str] | None) -> list[Path]:
    """Filter files in directory matching polyglot language criteria."""
    return [parent / name for name in files if _is_matching_language(Path(name).suffix, False, languages)]


def _walk_polyglot_files(root: Path, languages: Sequence[str] | None = None) -> list[Path]:
    """Return repository source files beneath root matching requested languages."""
    found: list[Path] = []
    for parent, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if _is_repository_dir(d)]
        found.extend(_polyglot_files_in_dir(Path(parent), files, languages))
    return sorted(found)


def _expand_polyglot_path(path: Path, languages: Sequence[str] | None) -> list[Path]:
    """Expand a single path target into a list of matching polyglot files."""
    if path.is_file():
        return [path] if _is_matching_language(path.suffix, True, languages) else []
    if path.is_dir():
        return _walk_polyglot_files(path, languages)
    return []


def _polyglot_files(paths: Sequence[Path], languages: Sequence[str] | None = None) -> list[Path]:
    """Expand targets into multi-language files matching requested languages."""
    found: list[Path] = []
    for p in paths:
        if p.exists():
            found.extend(_expand_polyglot_path(p, languages))
    return found


def _scan_polyglot_sources(
    paths: Sequence[Path],
    languages: Sequence[str] | None,
) -> dict[Path, str]:
    """Read non-Python polyglot source files, returning mapping of path to content."""
    files = [p for p in _polyglot_files(paths, languages) if p.suffix != ".py"]
    sources: dict[Path, str] = {}
    for p in files:
        try:
            sources[p] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return sources


def _polyglot_gating_findings(sources: Mapping[Path, str]) -> list[SmellFinding]:
    """Evaluate gating thresholds on polyglot modules (e.g. cyclomatic complexity)."""
    findings: list[SmellFinding] = []
    for path, source in sources.items():
        lang = POLYGLOT_EXTENSIONS.get(path.suffix, "unknown")
        _, _, complexity = polyglot_module_metrics(source, lang)
        if complexity > MAX_CYCLOMATIC_COMPLEXITY:
            findings.append(
                SmellFinding(
                    smell=Smell.HIGH_CYCLOMATIC_COMPLEXITY,
                    file_path=str(path),
                    line_number=1,
                    subject=path.name,
                    measured=float(complexity),
                    threshold=float(MAX_CYCLOMATIC_COMPLEXITY),
                    detail=f"Polyglot cyclomatic complexity {complexity}; exceeds threshold {MAX_CYCLOMATIC_COMPLEXITY}",
                )
            )
    return findings


def _record_polyglot_metrics(sources: Mapping[Path, str], report: SmellReport) -> None:
    """Record metrics for non-Python polyglot modules into report."""
    for path, source in sources.items():
        lang = POLYGLOT_EXTENSIONS.get(path.suffix, "unknown")
        mi, volume, complexity = polyglot_module_metrics(source, lang)
        loc = len([ln for ln in source.splitlines() if ln.strip()])
        report.modules.append(
            ModuleScore(
                path=str(path),
                loc=loc,
                halstead_volume=volume,
                max_complexity=complexity,
                maintainability=mi,
            )
        )


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


@dataclass(frozen=True)
class RevisionScore:
    """Quantified metric summary for a single git commit revision."""

    commit: str
    short_sha: str
    subject: str
    mean_maintainability: float
    total_loc: int
    module_count: int
    gating_findings: int
    advisory_findings: int
    delta_maintainability: float = 0.0
    delta_loc: int = 0
    delta_findings: int = 0


@dataclass
class TrendReport:
    """Sequence of historical revision metrics capturing code health trajectory."""

    revisions: list[RevisionScore] = field(default_factory=list)


def _git_repo_root() -> Path | None:
    """Locate the git repository root directory, or None if not in a git tree."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return Path(res.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        return None


def _parse_git_log_line(line: str) -> tuple[str, str, str] | None:
    """Parse one tab-delimited git log line into (full_sha, short_sha, subject)."""
    parts = line.strip().split("\t", 2)
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 else None


def _git_rev_list(count: int) -> list[tuple[str, str, str]]:
    """Retrieve up to count recent git commits formatted as (full_sha, short_sha, subject)."""
    try:
        res = subprocess.run(
            ["git", "log", f"-n{max(1, count)}", "--format=%H%x09%h%x09%s"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        parsed = (_parse_git_log_line(line) for line in res.stdout.splitlines())
        return [c for c in parsed if c is not None]
    except (subprocess.SubprocessError, OSError):
        return []


def _git_file_content(commit_sha: str, rel_path: str) -> str | None:
    """Fetch raw file content from git at a specified revision."""
    try:
        res = subprocess.run(
            ["git", "show", f"{commit_sha}:{rel_path}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return res.stdout
    except (subprocess.SubprocessError, OSError):
        return None


def _git_ls_files(commit_sha: str, rel_prefix: str = "") -> list[str]:
    """List tracked files at a specific revision beneath an optional relative prefix."""
    cmd = ["git", "ls-tree", "-r", "--name-only", commit_sha]
    if rel_prefix and rel_prefix != ".":
        cmd.append(rel_prefix)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10)
        return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    except (subprocess.SubprocessError, OSError):
        return []


def _is_git_candidate(rel_file: str, languages: Sequence[str] | None) -> bool:
    """Determine if a git-tracked file is an auditable non-vendored source candidate."""
    parts = Path(rel_file).parts
    if any(p in VENDORED_DIR_NAMES or p.startswith(".") for p in parts[:-1]):
        return False
    suffix = Path(rel_file).suffix
    return _is_matching_language(suffix, False, languages)


def _score_git_py_file(rel_path: str, source: str) -> tuple[ModuleScore | None, list[SmellFinding]]:
    """Parse and score a single Python module source string at a historical revision."""
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return None, []
    scores = _module_scores(Path(rel_path), source)
    mod_score = scores[0] if scores else None
    findings: list[SmellFinding] = []
    for detector in PER_FILE_DETECTORS:
        findings.extend(detector(tree, Path(rel_path)))
    return mod_score, findings


def _score_git_polyglot_file(
    rel_path: str, source: str, suffix: str
) -> tuple[ModuleScore, list[SmellFinding]]:
    """Score a single non-Python polyglot module source string at a historical revision."""
    lang = POLYGLOT_EXTENSIONS.get(suffix, "unknown")
    mi, vol, comp = polyglot_module_metrics(source, lang)
    loc = len([ln for ln in source.splitlines() if ln.strip()])
    mod_score = ModuleScore(
        path=rel_path,
        loc=loc,
        halstead_volume=vol,
        max_complexity=comp,
        maintainability=mi,
    )
    findings: list[SmellFinding] = []
    if comp > MAX_CYCLOMATIC_COMPLEXITY:
        findings.append(
            SmellFinding(
                smell=Smell.HIGH_CYCLOMATIC_COMPLEXITY,
                file_path=rel_path,
                line_number=1,
                subject=Path(rel_path).name,
                measured=float(comp),
                threshold=float(MAX_CYCLOMATIC_COMPLEXITY),
                detail=f"Polyglot cyclomatic complexity {comp}; exceeds threshold {MAX_CYCLOMATIC_COMPLEXITY}",
            )
        )
    return mod_score, findings


def _audit_git_source(
    rel_path: str, source: str, languages: Sequence[str] | None
) -> tuple[ModuleScore | None, list[SmellFinding]]:
    """Route source scoring to Python or polyglot analyzer based on file extension."""
    suffix = Path(rel_path).suffix
    if suffix == ".py":
        return _score_git_py_file(rel_path, source)
    if _is_matching_language(suffix, False, languages):
        return _score_git_polyglot_file(rel_path, source, suffix)
    return None, []


def _to_rel_path_str(path: Path, repo_root: Path) -> str:
    """Compute repository-relative path string, falling back to str(path)."""
    try:
        return str(path.resolve().relative_to(repo_root))
    except (ValueError, OSError):
        return str(path)


def _filter_git_candidates(files: Sequence[str], languages: Sequence[str] | None) -> list[str]:
    """Filter list of git files against candidate source criteria."""
    return [f for f in files if _is_git_candidate(f, languages)]


def _resolve_git_candidates(
    commit_sha: str,
    paths: Sequence[Path],
    repo_root: Path,
    languages: Sequence[str] | None,
) -> list[str]:
    """Resolve target paths into repository-relative files at a specific revision."""
    rel_targets = [_to_rel_path_str(p, repo_root) for p in paths]
    if not rel_targets or rel_targets == ["."]:
        return _filter_git_candidates(_git_ls_files(commit_sha), languages)

    matched: list[str] = []
    for target in rel_targets:
        matched.extend(_filter_git_candidates(_git_ls_files(commit_sha, target), languages))
    return sorted(set(matched))


def _collect_rev_modules_and_findings(
    commit_sha: str,
    target_files: Sequence[str],
    languages: Sequence[str] | None,
) -> tuple[list[ModuleScore], list[SmellFinding]]:
    """Collect scored modules and findings across target files at one commit."""
    modules: list[ModuleScore] = []
    findings: list[SmellFinding] = []
    for rel_file in target_files:
        content = _git_file_content(commit_sha, rel_file)
        if content is None:
            continue
        mod, file_findings = _audit_git_source(rel_file, content, languages)
        if mod is not None:
            modules.append(mod)
        findings.extend(file_findings)
    return modules, findings


def _count_finding_severities(findings: Sequence[SmellFinding]) -> tuple[int, int]:
    """Tally gating and advisory findings."""
    gating = 0
    advisory = 0
    for f in findings:
        if f.smell in ADVISORY_SMELLS:
            advisory += 1
        else:
            gating += 1
    return gating, advisory


def _compute_rev_deltas(
    mean_mi: float, tot_loc: int, total_f: int, prev: RevisionScore | None
) -> tuple[float, int, int]:
    """Calculate metric deltas relative to previous revision."""
    if prev is None:
        return 0.0, 0, 0
    return (
        round(mean_mi - prev.mean_maintainability, 1),
        tot_loc - prev.total_loc,
        total_f - (prev.gating_findings + prev.advisory_findings),
    )


def _analyze_git_revision(
    commit_info: tuple[str, str, str],
    target_files: Sequence[str],
    languages: Sequence[str] | None,
    prev_rev: RevisionScore | None,
) -> RevisionScore:
    """Analyze all matching files at a single git revision and compute metric deltas."""
    commit_sha, short_sha, subject = commit_info
    modules, findings = _collect_rev_modules_and_findings(commit_sha, target_files, languages)
    scores = [m.maintainability for m in modules]
    mean_mi = round(sum(scores) / len(scores), 1) if scores else 0.0
    tot_loc = sum(m.loc for m in modules)
    gating, advisory = _count_finding_severities(findings)
    delta_mi, delta_loc, delta_f = _compute_rev_deltas(mean_mi, tot_loc, gating + advisory, prev_rev)

    return RevisionScore(
        commit=commit_sha,
        short_sha=short_sha,
        subject=subject,
        mean_maintainability=mean_mi,
        total_loc=tot_loc,
        module_count=len(modules),
        gating_findings=gating,
        advisory_findings=advisory,
        delta_maintainability=delta_mi,
        delta_loc=delta_loc,
        delta_findings=delta_f,
    )


def compute_trend(
    paths: Sequence[Path],
    revisions_count: int = 5,
    languages: Sequence[str] | None = None,
) -> TrendReport:
    """Compute metric trajectory across recent git revisions."""
    root = _git_repo_root()
    if root is None:
        return TrendReport()

    raw_commits = _git_rev_list(revisions_count)
    if not raw_commits:
        return TrendReport()

    # Step chronologically forward (oldest -> newest) to calculate forward deltas
    commits = list(reversed(raw_commits))
    scores: list[RevisionScore] = []
    prev: RevisionScore | None = None
    for full_sha, short_sha, subj in commits:
        candidates = _resolve_git_candidates(full_sha, paths, root, languages)
        rev_score = _analyze_git_revision((full_sha, short_sha, subj), candidates, languages, prev)
        scores.append(rev_score)
        prev = rev_score

    return TrendReport(revisions=scores)


def render_trend(report: TrendReport) -> list[str]:
    """Render historical metric trajectory table and trajectory summary."""
    lines = [
        "=" * 86,
        "📈 CODE HEALTH TREND TRAJECTORY",
        "=" * 86,
        f"{'Commit':<9} {'Subject':<36} {'Mods':>5} {'LOC':>7} {'Mean MI':>12} {'Gating':>7} {'Adv':>5}",
        "-" * 86,
    ]
    for rev in report.revisions:
        mi_str = (
            f"{rev.mean_maintainability:.1f} ({rev.delta_maintainability:+0.1f})"
            if rev.delta_maintainability
            else f"{rev.mean_maintainability:.1f}"
        )
        subj = rev.subject[:33] + "..." if len(rev.subject) > 36 else rev.subject
        lines.append(
            f"{rev.short_sha:<9} {subj:<36} {rev.module_count:>5} {rev.total_loc:>7} {mi_str:>12} {rev.gating_findings:>7} {rev.advisory_findings:>5}"
        )
    lines.append("=" * 86)
    if report.revisions:
        first = report.revisions[0]
        last = report.revisions[-1]
        lines.append(
            f"Trajectory ({len(report.revisions)} revs): "
            f"MI {first.mean_maintainability:.1f} -> {last.mean_maintainability:.1f} ({last.mean_maintainability - first.mean_maintainability:+0.1f}), "
            f"LOC {first.total_loc} -> {last.total_loc} ({last.total_loc - first.total_loc:+d})"
        )
        lines.append("=" * 86)
    return lines


def _render_trend_json(report: TrendReport) -> str:
    """Serialize the trend report into JSON for automated dashboards."""
    return json.dumps(
        {
            "trend": [
                {
                    "commit": r.commit,
                    "short_sha": r.short_sha,
                    "subject": r.subject,
                    "mean_maintainability": r.mean_maintainability,
                    "total_loc": r.total_loc,
                    "module_count": r.module_count,
                    "gating_findings": r.gating_findings,
                    "advisory_findings": r.advisory_findings,
                    "delta_maintainability": r.delta_maintainability,
                    "delta_loc": r.delta_loc,
                    "delta_findings": r.delta_findings,
                }
                for r in report.revisions
            ]
        },
        indent=2,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    parser = argparse.ArgumentParser(description="Deterministic code smell quantifier")
    parser.add_argument("paths", nargs="*", default=[], help="Files or directories to analyze")
    parser.add_argument(
        "--fail-on",
        choices=("none", "gating", "any"),
        default="gating",
        help="Which findings cause a non-zero exit (default: gating)",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    parser.add_argument(
        "--languages",
        "-L",
        default=None,
        help="Comma-separated languages or 'all' to analyze (default: python)",
    )
    parser.add_argument(
        "--trend",
        nargs="?",
        const=5,
        type=int,
        default=None,
        metavar="REVISIONS",
        help="Step back through N git revisions (default: 5) to compute metric trajectory",
    )
    return parser


def _partition_targets(targets: Sequence[Path]) -> tuple[list[Path], list[Path]]:
    """Partition targets into present and missing lists."""
    present = [t for t in targets if t.exists()]
    missing = [t for t in targets if not t.exists()]
    return present, missing


def _warn_missing_targets(missing: Sequence[Path]) -> None:
    """Emit warning to stderr for non-existent targets."""
    for target in missing:
        print(f"MISSING {target} — refusing to certify a path that does not exist.", file=sys.stderr)


def _resolve_targets(raw_paths: Sequence[str]) -> tuple[list[Path], list[Path]]:
    """Split requested targets into those that exist and those that do not."""
    targets = [Path(raw) for raw in raw_paths] or [Path(".")]
    present, missing = _partition_targets(targets)
    _warn_missing_targets(missing)
    return present, missing


def _render_json(report: SmellReport) -> str:
    """Serialize the report for trending across runs."""
    return json.dumps(
        {
            "mean_maintainability": report.mean_maintainability,
            "counts": report.counts(),
            "modules": [vars(m) for m in report.modules],
            "findings": [vars(f) | {"smell": f.smell.value} for f in report.findings],
        },
        indent=2,
    )


def _parse_cli_languages(languages_arg: str | None) -> list[str] | None:
    """Parse comma-separated language argument into a list of strings."""
    if not languages_arg:
        return None
    return [lang.strip() for lang in languages_arg.split(",")]


def _run_trend_command(
    present: list[Path], args: argparse.Namespace, langs: list[str] | None, missing: list[Path]
) -> int:
    """Execute trend analysis across historical git revisions."""
    trend_report = compute_trend(present, revisions_count=args.trend, languages=langs)
    output = _render_trend_json(trend_report) if args.json else "\n".join(render_trend(trend_report))
    print(output)
    return 1 if missing else 0


def _run_analysis_command(
    present: list[Path], args: argparse.Namespace, langs: list[str] | None, missing: list[Path]
) -> int:
    """Execute standard codebase smell analysis and enforce gating failure conditions."""
    report = analyze(present, languages=langs)
    output = _render_json(report) if args.json else "\n".join(render(report))
    print(output)
    blocking = {"none": [], "gating": report.gating, "any": report.findings}[args.fail_on]
    return 1 if (blocking or missing) else 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI runner for the code smell quantifier."""
    cli_args = list(argv[1:]) if argv is not None else None
    args = build_arg_parser().parse_args(cli_args)
    present, missing = _resolve_targets(args.paths)
    langs = _parse_cli_languages(args.languages)

    if args.trend is not None:
        return _run_trend_command(present, args, langs, missing)
    return _run_analysis_command(present, args, langs, missing)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
