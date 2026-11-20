#!/usr/bin/env python3
"""Closed-Loop PR Review Thread Synchronizer & Sequential Resolver.

Reads GitHub PR review threads from a supplied JSON payload (it makes no network
call), correlates review feedback with local AST state and mechanical invariant
oracles (complexity, nesting, sanitization, docstrings), checks whether defects have
been resolved, and writes resolution plans and GraphQL mutation text.

CC001, ND001 and DOC001 check the definition that the thread names: the node's
`symbolName` field, else the definition that encloses the review-time line in the version
of the file that the review was made on (`--base-root`), else the definition that the
first comment's `diffHunk` shows. A function that moved is found by its name. The check
fails when the file has no definition of that name, or when the thread names no symbol and
no definition encloses the thread line, for example a line past the end of the file.
SEC001 reads the whole file.

A thread on the LEFT side of the diff (a comment on a deleted line) numbers its line in
the left version of the file: the pull request's base at review time (`--left-root`). Its
line is never read against the current file.

Each reply states only what the tool measured. A CC001 reply gives the symbol, its radon
complexity in the base version and now, and the limit. An ND001 reply gives the symbol and
its nesting depth, counted as the AST sentinel counts it.

The reply and resolve mutations are not atomic: GraphQL runs mutation fields in order,
with no transaction, so a failed resolve leaves the reply in place.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from metric_reference import cyclomatic_complexity, nesting_depth
from sarif_report import SARIF_SCHEMA_URI

# Rule identification patterns in review comments
_RULE_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("CC001", re.compile(r"(?i)\b(?:CC001|cyclomatic|complexity|mccabe)\b")),
    ("ND001", re.compile(r"(?i)\b(?:ND001|nesting|indentation|nested depth)\b")),
    ("SEC001", re.compile(r"(?i)\b(?:SEC001|rfc\s*1918|private\s*ip|secret|credential|token)\b")),
    ("DOC001", re.compile(r"(?i)\b(?:DOC\d+|docstring|documentation|markdown|typo|link)\b")),
    ("TYP001", re.compile(r"(?i)\b(?:TYP\d+|typecheck|mypy|annotation|type signature)\b")),
)

# Private RFC 1918 IP address pattern
_RFC1918_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"
)

# A `def`, `async def` or `class` line, and a unified diff hunk header with its context text
_DEFINITION_LINE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+(?P<name>\w+)")
_HUNK_HEADER: Final[re.Pattern[str]] = re.compile(r"^@@[^@]*@@(?P<context>.*)$")

type _Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
_FUNCTION_TYPES: Final[tuple[type[_Definition], ...]] = (ast.FunctionDef, ast.AsyncFunctionDef)
_DEFINITION_TYPES: Final[tuple[type[_Definition], ...]] = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# The CC001 and ND001 limits of AGENTS.md §10
_COMPLEXITY_LIMIT: Final[int] = 10
_NESTING_LIMIT: Final[int] = 5

# The compound statements that the AST sentinel counts as one ND001 nesting level
_NESTING_TYPES: Final[tuple[type[ast.AST], ...]] = (
    ast.If,
    ast.While,
    ast.For,
    ast.AsyncFor,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.TryStar,
    ast.ExceptHandler,
    ast.Match,
    ast.match_case,
)


class DiffSide(StrEnum):
    """The side of the diff that a review thread is on, as GitHub names it."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"


class ResolutionStatus(StrEnum):
    """Resolution status of a PR review thread."""

    RESOLVED = "RESOLVED"
    FIX_VERIFIED = "FIX_VERIFIED"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    ORPHANED_PATH = "ORPHANED_PATH"


@dataclass(frozen=True)
class ReviewComment:
    """A single comment within a review discussion thread."""

    comment_id: str
    author: str
    body: str
    created_at: str


@dataclass(frozen=True)
class ReviewThread:
    """A GitHub PR review discussion thread anchored to a file path, a line and a symbol.

    `symbol_name` is the bare or dotted name of the reviewed definition at review time,
    or an empty string when the payload does not show it. `symbol_is_explicit` is true
    when that name came from the node's `symbolName` field, not from the diff hunk.
    `original_line` is the commented line at review time (`originalLine`); zero means that
    the payload gave only `line`. `side` is the side of the diff: on the LEFT side, both
    line numbers count lines of the pull request's base, not of the current file.
    """

    thread_id: str
    is_resolved: bool
    path: str
    line: int
    comments: list[ReviewComment] = field(default_factory=list)
    symbol_name: str = ""
    symbol_is_explicit: bool = False
    original_line: int = 0
    side: DiffSide = DiffSide.RIGHT

    @property
    def review_line(self) -> int:
        """Returns the commented line in the version of the file that the review was made on."""
        return self.original_line or self.line

    @property
    def current_line(self) -> int:
        """Returns the thread line in the current file, or 0 for a LEFT-side line, which has none."""
        return 0 if self.side == DiffSide.LEFT else self.line


@dataclass(frozen=True)
class ThreadAnalysis:
    """Analysis result correlating a review thread with current codebase AST state."""

    thread: ReviewThread
    status: ResolutionStatus
    rule_id: str
    symbol_name: str
    explanation: str
    suggested_reply: str

    def to_dict(self) -> dict[str, Any]:
        """Converts analysis to a structured dictionary."""
        return {
            "thread_id": self.thread.thread_id,
            "path": self.thread.path,
            "line": self.thread.line,
            "is_resolved": self.thread.is_resolved,
            "status": self.status.value,
            "rule_id": self.rule_id,
            "symbol_name": self.symbol_name,
            "explanation": self.explanation,
            "suggested_reply": self.suggested_reply,
            "comment_count": len(self.thread.comments),
        }


def classify_comment_rule(body: str) -> str:
    """Classifies a review comment body to determine the primary rule identifier."""
    for rule_id, pattern in _RULE_PATTERNS:
        if pattern.search(body):
            return rule_id
    return "GENERAL"


def _parse_json_safe(payload: str) -> dict[str, Any] | list[dict[str, Any]]:
    """Safely decodes JSON text into dictionary or list."""
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return []


def _normalize_raw_payload(
    payload: dict[str, Any] | list[dict[str, Any]] | str,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Deserializes raw JSON strings if needed."""
    if not isinstance(payload, str):
        return payload
    return _parse_json_safe(payload)


def parse_review_threads(payload: dict[str, Any] | list[dict[str, Any]] | str) -> list[ReviewThread]:
    """Parses review threads from GraphQL, REST API payload formats, or JSON string."""
    resolved_payload = _normalize_raw_payload(payload)
    raw_nodes = _extract_raw_nodes(resolved_payload)
    return [t for node in raw_nodes if (t := _parse_single_thread(node)) is not None]


def _extract_raw_nodes(payload: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extracts raw thread nodes from GraphQL or list structures."""
    if isinstance(payload, list):
        return payload
    if "data" in payload and isinstance(payload["data"], dict):
        repo = payload["data"].get("repository", {})
        pr = repo.get("pullRequest", {})
        return pr.get("reviewThreads", {}).get("nodes", [])
    if "nodes" in payload and isinstance(payload["nodes"], list):
        return payload["nodes"]
    return []


def _parse_single_thread(node: dict[str, Any]) -> ReviewThread | None:
    """Parses a single dictionary into a ReviewThread instance."""
    thread_id = str(node.get("id") or node.get("thread_id", ""))
    if not thread_id:
        return None
    is_resolved = bool(node.get("isResolved", node.get("is_resolved", False)))
    path = str(node.get("path", ""))
    line = int(node.get("line") or node.get("originalLine") or 1)
    raw_comments = _comment_nodes(node.get("comments", []))
    explicit = _explicit_symbol_name(node)
    side = _diff_side(node, raw_comments)
    return ReviewThread(
        thread_id=thread_id,
        is_resolved=is_resolved,
        path=path,
        line=line,
        comments=[_parse_single_comment(item) for item in raw_comments],
        symbol_name=explicit or _hunk_symbol_name(raw_comments, side),
        symbol_is_explicit=bool(explicit),
        original_line=int(node.get("originalLine") or node.get("original_line") or 0),
        side=side,
    )


def _diff_side(node: dict[str, Any], raw_comments: list[dict[str, Any]]) -> DiffSide:
    """Reads the thread's diff side: GraphQL `diffSide`, else REST `side`; RIGHT when absent."""
    first = raw_comments[0] if raw_comments else {}
    raw = node.get("diffSide") or node.get("side") or first.get("side") or ""
    return DiffSide.LEFT if str(raw).upper() == DiffSide.LEFT else DiffSide.RIGHT


def _comment_nodes(raw_comments: Any) -> list[dict[str, Any]]:
    """Returns the comment dictionaries of a raw comments list or GraphQL connection."""
    nodes = raw_comments.get("nodes", []) if isinstance(raw_comments, dict) else raw_comments
    if not isinstance(nodes, list):
        return []
    return [item for item in nodes if isinstance(item, dict)]


def _explicit_symbol_name(node: dict[str, Any]) -> str:
    """Reads the reviewed symbol name that the thread node states, or an empty string."""
    explicit = node.get("symbolName") or node.get("symbol_name") or node.get("symbol")
    return str(explicit) if explicit else ""


def _hunk_symbol_name(raw_comments: list[dict[str, Any]], side: DiffSide) -> str:
    """Reads the reviewed symbol name from the diff hunk of the first comment."""
    first_hunk = raw_comments[0].get("diffHunk", "") if raw_comments else ""
    return symbol_from_diff_hunk(str(first_hunk or ""), side)


def symbol_from_diff_hunk(diff_hunk: str, side: DiffSide = DiffSide.RIGHT) -> str:
    """Names the definition that encloses the commented line of a review comment's diff hunk.

    GitHub ends a review comment's `diffHunk` at the commented line. The walk goes up from
    that line through the lines of the comment's side (the new-side lines, or the old-side
    lines for a LEFT-side comment on a deleted line) and keeps each `def` or `class` line
    that is less indented than every line below it, so the result is a dotted path such as
    `Calc.compute`. When the hunk shows no enclosing definition and the walk did not reach
    module level, the hunk header's context text gives a bare name. That header can name a
    different definition than the one that holds the line (the class of a method, or the
    function above the line), so `resolve_review_symbol` prefers the base version of the
    file. An empty string means the hunk shows no symbol.
    """
    lines = diff_hunk.splitlines()
    changed = "-" if side == DiffSide.LEFT else "+"
    side_lines = [text[1:] for text in lines[1:] if text[:1] in (" ", changed)]
    names, ceiling = _enclosing_definitions(side_lines)
    if not names and ceiling != 0 and lines:
        names = _header_definition(lines[0])
    return ".".join(names)


def _enclosing_definitions(lines: Sequence[str]) -> tuple[list[str], int | None]:
    """Walks up from the last line, and returns the enclosing definition names and the lowest indent."""
    names: list[str] = []
    ceiling: int | None = None
    for text in reversed(lines):
        if not text.strip():
            continue
        indent = len(text) - len(text.lstrip())
        match = _DEFINITION_LINE.match(text)
        if match and (ceiling is None or indent < ceiling):
            names.insert(0, match["name"])
        ceiling = indent if ceiling is None else min(ceiling, indent)
    return names, ceiling


def _header_definition(header: str) -> list[str]:
    """Returns the definition name in a hunk header's context text, if it shows one."""
    header_match = _HUNK_HEADER.match(header)
    definition = _DEFINITION_LINE.match(header_match["context"]) if header_match else None
    return [definition["name"]] if definition else []


def _parse_single_comment(item: dict[str, Any]) -> ReviewComment:
    """Extracts a single comment instance from dictionary."""
    c_id = str(item.get("id", ""))
    author = item.get("author", {})
    author_name = str(author.get("login", "") if isinstance(author, dict) else item.get("author", ""))
    body = str(item.get("body", ""))
    created_at = str(item.get("createdAt", item.get("created_at", "")))
    return ReviewComment(c_id, author_name, body, created_at)


def locate_ast_symbol(file_path: Path, line: int, symbol_name: str = "") -> str:
    """Describes the thread's AST symbol (function, method, class).

    With a symbol name, describes the definition of that name wherever it now is, or says
    that it was not found. Without one, describes the definition that encloses the line.
    """
    if file_path.suffix.lower() != ".py" or not file_path.exists():
        return "file scope"
    tree = _parse_ast_safe(file_path)
    if tree is None:
        return "file scope"
    if not symbol_name:
        return _find_enclosing_symbol(tree, line)
    node = find_symbol_by_name(tree, symbol_name)
    return _format_symbol_name(node) if node else f"'{symbol_name}' (not found)"


def find_symbol_by_name(tree: ast.AST, qualified_name: str) -> _Definition | None:
    """Finds a definition by bare or dotted name; the outermost, then earliest, match wins."""
    scope: ast.AST | None = tree
    for part in qualified_name.split("."):
        scope = _first_definition_named(scope, part) if scope is not None else None
    return scope if isinstance(scope, _DEFINITION_TYPES) else None


def _first_definition_named(scope: ast.AST, name: str) -> _Definition | None:
    """Returns the first definition named `name` below `scope`, in breadth-first order."""
    for node in ast.walk(scope):
        if node is not scope and isinstance(node, _DEFINITION_TYPES) and node.name == name:
            return node
    return None


def resolve_review_symbol(thread: ReviewThread, base_root: Path | None, left_root: Path | None = None) -> str:
    """Names the definition that a thread reviewed, from the best evidence that is available.

    The node's `symbolName` comes first. Next is the dotted path of the definitions that
    enclose the review-time line in the version of the file that the line numbers, for
    example `B.run`: `base_root` holds the reviewed commit (the RIGHT side of the diff),
    and `left_root` holds the pull request's base at review time (the LEFT side). The name
    from the diff hunk is used only when that lookup fails: no root for the thread's side,
    no such file, no parse, or no definition at the line.
    """
    review_root = left_root if thread.side == DiffSide.LEFT else base_root
    if thread.symbol_is_explicit or review_root is None:
        return thread.symbol_name
    tree = _parse_ast_safe(review_root / thread.path)
    in_base = definition_path_at_line(tree, thread.review_line) if tree is not None else ""
    return in_base or thread.symbol_name


def definition_path_at_line(tree: ast.AST, line: int) -> str:
    """Returns the dotted names of the definitions that span the line, outermost first.

    Two definitions that span one line are nested, and the outer one starts first, so the
    start lines give the order. An empty string means that no definition spans the line.
    """
    definitions = [node for node in ast.walk(tree) if isinstance(node, _DEFINITION_TYPES)]
    spanning = [node for node in definitions if _node_encloses_line(node, line)]
    return ".".join(node.name for node in sorted(spanning, key=lambda node: node.lineno))


def _parse_ast_safe(file_path: Path) -> ast.AST | None:
    """Parses Python AST safely, returning None on errors."""
    try:
        content = file_path.read_text(encoding="utf-8")
        return ast.parse(content, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None


def _find_enclosing_symbol(tree: ast.AST, line: int) -> str:
    """Finds enclosing symbol name from parsed AST tree."""
    node = _find_enclosing_node(tree, line, _DEFINITION_TYPES)
    return _format_symbol_name(node) if node else "module level"


def _find_enclosing_node(tree: ast.AST, line: int, kinds: tuple[type[_Definition], ...]) -> _Definition | None:
    """Returns the innermost definition of the given kinds that spans the line."""
    innermost: _Definition | None = None
    for node in ast.walk(tree):
        if isinstance(node, kinds) and _node_encloses_line(node, line):
            innermost = node
    return innermost


def _node_encloses_line(node: ast.AST, line: int) -> bool:
    """Predicate checking if node spans the given line number."""
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", 0)
    return start <= line <= end


def _format_symbol_name(node: ast.AST) -> str:
    """Formats human-readable symbol description with line bounds."""
    kind = "class" if isinstance(node, ast.ClassDef) else "function"
    name = getattr(node, "name", "unknown")
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", 0)
    return f"{kind} '{name}' (lines {start}-{end})"


def verify_thread_resolution(
    root_dir: Path, thread: ReviewThread, base_root: Path | None = None, left_root: Path | None = None
) -> ThreadAnalysis:
    """Evaluates whether an open review thread has been resolved in the workspace.

    `base_root` holds the base version of the files: the version that the review was made
    on. With it, the thread's symbol name comes from that version (`resolve_review_symbol`)
    and a CC001 reply states the old complexity. `left_root` holds the pull request's base
    at review time, where a LEFT-side thread's line is looked up. The analysis keeps the
    thread with that name.
    """
    if thread.is_resolved:
        return _make_resolved_analysis(thread)

    target_path = root_dir / thread.path
    if not target_path.exists():
        return _make_orphaned_analysis(thread)

    thread = replace(thread, symbol_name=resolve_review_symbol(thread, base_root, left_root))
    last_body = thread.comments[-1].body if thread.comments else ""
    rule_id = classify_comment_rule(last_body)
    symbol_name = locate_ast_symbol(target_path, thread.current_line, thread.symbol_name)
    base_path = base_root / thread.path if base_root is not None else None

    status, explanation, reply = _audit_rule_resolution(target_path, thread, rule_id, symbol_name, base_path)
    return ThreadAnalysis(thread, status, rule_id, symbol_name, explanation, reply)


def _make_resolved_analysis(thread: ReviewThread) -> ThreadAnalysis:
    """Constructs analysis for already resolved threads."""
    return ThreadAnalysis(
        thread=thread,
        status=ResolutionStatus.RESOLVED,
        rule_id="NONE",
        symbol_name="resolved",
        explanation="Thread is already resolved on GitHub.",
        suggested_reply="",
    )


def _make_orphaned_analysis(thread: ReviewThread) -> ThreadAnalysis:
    """Constructs analysis for threads on missing or deleted files."""
    return ThreadAnalysis(
        thread=thread,
        status=ResolutionStatus.ORPHANED_PATH,
        rule_id="ORPHAN",
        symbol_name="missing",
        explanation=f"Target file '{thread.path}' does not exist on disk.",
        suggested_reply="Path has been removed or renamed in active branch.",
    )


def _audit_rule_resolution(
    target_path: Path,
    thread: ReviewThread,
    rule_id: str,
    symbol_name: str,
    base_path: Path | None,
) -> tuple[ResolutionStatus, str, str]:
    """Audits specific rule compliance using table-driven dispatch."""
    content = _read_file_safe(target_path)
    dispatch: dict[str, Callable[[], tuple[ResolutionStatus, str, str]]] = {
        "SEC001": lambda: _audit_security_rule(content, symbol_name),
        "CC001": lambda: _audit_complexity_rule(content, thread, symbol_name, base_path),
        "ND001": lambda: _audit_nesting_rule(content, thread, symbol_name),
        "DOC001": lambda: _audit_doc_rule(content, thread, symbol_name),
    }
    audit_fn = dispatch.get(rule_id)
    if audit_fn is not None:
        return audit_fn()
    return (
        ResolutionStatus.ACTION_REQUIRED,
        f"General feedback in {symbol_name} requires manual verification.",
        "Addressed review feedback in active revision.",
    )


def _read_file_safe(path: Path) -> str:
    """Reads file text safely, returning empty string on error."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _audit_security_rule(content: str, symbol_name: str) -> tuple[ResolutionStatus, str, str]:
    """Audits security rule for presence of private RFC 1918 IP addresses."""
    if not _RFC1918_PATTERN.search(content):
        return (
            ResolutionStatus.FIX_VERIFIED,
            f"Zero RFC 1918 IP leaks verified in {symbol_name}.",
            "Verified: the file contains no RFC 1918 private IPv4 address. "
            "Secrets, tokens and credentials were not checked.",
        )
    return (
        ResolutionStatus.ACTION_REQUIRED,
        "Private RFC 1918 IP address detected in file.",
        "The file still contains an RFC 1918 private IPv4 address.",
    )


def _audit_complexity_rule(
    content: str, thread: ReviewThread, symbol_name: str, base_path: Path | None
) -> tuple[ResolutionStatus, str, str]:
    """Audits radon's McCabe complexity of the function that the thread names against the limit."""
    func_node, failure = _locate_audit_target(content, thread, _FUNCTION_TYPES)
    if func_node is None:
        return (ResolutionStatus.ACTION_REQUIRED, failure, "")
    new = cyclomatic_complexity(func_node)
    old = _base_complexity(base_path, thread.symbol_name)
    reply = _complexity_reply(thread.symbol_name or func_node.name, old, new)
    if new <= _COMPLEXITY_LIMIT:
        return (
            ResolutionStatus.FIX_VERIFIED,
            f"Cyclomatic complexity M={new} <= {_COMPLEXITY_LIMIT} verified for {symbol_name}.",
            f"Verified: {reply}",
        )
    return (
        ResolutionStatus.ACTION_REQUIRED,
        f"Cyclomatic complexity M={new} still exceeds ceiling of {_COMPLEXITY_LIMIT} in {symbol_name}.",
        reply,
    )


def _base_complexity(base_path: Path | None, symbol: str) -> int | None:
    """Measures the named function in the base version of the file, or returns None if it cannot."""
    tree = _parse_ast_safe(base_path) if base_path is not None and symbol else None
    node = find_symbol_by_name(tree, symbol) if tree is not None else None
    return cyclomatic_complexity(node) if isinstance(node, _FUNCTION_TYPES) else None


def _complexity_reply(label: str, old: int | None, new: int) -> str:
    """States what the CC001 check measured: the symbol, its old and new complexity, and the limit."""
    relation = "at or below" if new <= _COMPLEXITY_LIMIT else "above"
    limit = _COMPLEXITY_LIMIT
    if old is None:
        before = f"No base version of {label} was found, so the old complexity was not measured."
    else:
        before = f"In the base version, {label} measured M={old}."
    measured = f"radon measures cyclomatic complexity M={new} for {label}, {relation} the limit of {limit}."
    return f"{measured} {before}"


def _locate_audit_target(
    content: str, thread: ReviewThread, kinds: tuple[type[_Definition], ...]
) -> tuple[_Definition | None, str]:
    """Finds the definition that a thread is about, or says why no definition can be checked.

    A thread with a symbol name is checked against the definition of that name, wherever it
    now is. A thread without one falls back to the definition that encloses its line in the
    current file; a LEFT-side line has none. The lookup fails closed: no definition means
    no verified fix.
    """
    tree = _parse_ast_safe_content(content)
    if tree is None:
        return None, "File does not parse as Python, so the check cannot show a fix."
    if thread.symbol_name:
        named = find_symbol_by_name(tree, thread.symbol_name)
        if isinstance(named, kinds):
            return named, ""
        return None, f"No matching definition named '{thread.symbol_name}' exists in the current file."
    enclosing = _find_enclosing_node(tree, thread.current_line, kinds)
    if enclosing is None:
        return None, _unlocated_reason(content, thread)
    return enclosing, ""


def _unlocated_reason(content: str, thread: ReviewThread) -> str:
    """Says why a thread that names no symbol has no definition in the current file to check."""
    if thread.side == DiffSide.LEFT:
        return (
            f"The thread is on the LEFT side of the diff, so line {thread.line} is a line of the "
            "pull request's base, not of the current file. Neither the left version (--left-root) "
            "nor the diff hunk names a definition at that line."
        )
    line_count = len(content.splitlines())
    if thread.line > line_count:
        return f"The thread names no symbol, and line {thread.line} is not in the current file ({line_count} lines)."
    return f"The thread names no symbol, and no matching definition encloses line {thread.line}."


def _parse_ast_safe_content(content: str) -> ast.AST | None:
    """Parses Python AST from string safely."""
    try:
        return ast.parse(content)
    except SyntaxError:
        return None


def _audit_nesting_rule(content: str, thread: ReviewThread, symbol_name: str) -> tuple[ResolutionStatus, str, str]:
    """Audits the nesting depth of the function that the thread names against the limit.

    The depth is the deepest run of nested compound statements in the function, as the AST
    sentinel counts it (`metric_reference.nesting_depth`).
    """
    func_node, failure = _locate_audit_target(content, thread, _FUNCTION_TYPES)
    if func_node is None:
        return (ResolutionStatus.ACTION_REQUIRED, failure, "")
    depth = nesting_depth(func_node, _NESTING_TYPES)
    measured = f"{thread.symbol_name or func_node.name} has nesting depth {depth}"
    if depth <= _NESTING_LIMIT:
        return (
            ResolutionStatus.FIX_VERIFIED,
            f"Nesting depth {depth} <= {_NESTING_LIMIT} verified for {symbol_name}.",
            f"Verified: {measured}, at or below the limit of {_NESTING_LIMIT}.",
        )
    return (
        ResolutionStatus.ACTION_REQUIRED,
        f"Nesting depth {depth} still exceeds ceiling of {_NESTING_LIMIT} in {symbol_name}.",
        f"Not fixed: {measured}, above the limit of {_NESTING_LIMIT}.",
    )


def _audit_doc_rule(content: str, thread: ReviewThread, symbol_name: str) -> tuple[ResolutionStatus, str, str]:
    """Audits that the definition the thread names now has a non-empty docstring.

    This shows that a docstring exists. It cannot show that a typo or a link was fixed.
    """
    node, failure = _locate_audit_target(content, thread, _DEFINITION_TYPES)
    if node is None:
        return (ResolutionStatus.ACTION_REQUIRED, failure, "")
    if ast.get_docstring(node):
        return (
            ResolutionStatus.FIX_VERIFIED,
            f"Non-empty docstring verified on {symbol_name}.",
            f"Verified: {node.name} now has a docstring.",
        )
    return (
        ResolutionStatus.ACTION_REQUIRED,
        f"{symbol_name} still has no docstring.",
        f"Adding a docstring to {node.name}.",
    )


def synchronize_threads(
    root_dir: Path,
    threads: Sequence[ReviewThread],
    base_root: Path | None = None,
    left_root: Path | None = None,
) -> list[ThreadAnalysis]:
    """Synchronizes a sequence of review threads with the active codebase state."""
    return [verify_thread_resolution(root_dir, t, base_root, left_root) for t in threads]


def generate_resolution_mutation(thread_id: str, reply_body: str) -> str:
    """Synthesizes a GraphQL mutation query to reply to and resolve a review thread."""
    sanitized_body = json.dumps(reply_body)
    return (
        f'mutation {{ addPullRequestReviewThreadReply(input: {{ pullRequestReviewThreadId: "{thread_id}", body: {sanitized_body} }}) '
        f'{{ comment {{ id }} }} resolveReviewThread(input: {{ threadId: "{thread_id}" }}) {{ thread {{ isResolved }} }} }}'
    )


def export_sarif(analyses: Sequence[ThreadAnalysis]) -> dict[str, Any]:
    """Exports thread analyses as an OASIS SARIF 2.1.0 telemetry payload."""
    results = [_make_sarif_result(item) for item in analyses]
    rules = [
        {"id": f"PR_THREAD_{item.rule_id}", "shortDescription": {"text": f"PR Thread Rule {item.rule_id}"}}
        for item in analyses
    ]

    return {
        "$schema": SARIF_SCHEMA_URI,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "pr_thread_sync",
                        "informationUri": "https://github.com/dan-petty/vibes",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def _make_sarif_result(item: ThreadAnalysis) -> dict[str, Any]:
    """Creates a single SARIF result entry from a ThreadAnalysis."""
    level = "warning" if item.status == ResolutionStatus.ACTION_REQUIRED else "note"
    return {
        "ruleId": f"PR_THREAD_{item.rule_id}",
        "level": level,
        "message": {"text": f"[{item.status.value}] {item.explanation}"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": item.thread.path},
                    "region": {"startLine": item.thread.line},
                }
            }
        ],
    }


def render_terminal_summary(analyses: Sequence[ThreadAnalysis]) -> str:
    """Renders a readable summary table for terminal display."""
    lines: list[str] = [
        "================================================================================",
        f"{'Closed-Loop PR Review Thread Synchronizer & Sequential Resolver':^80}",
        "================================================================================",
        f" Total Threads: {len(analyses)}",
        "--------------------------------------------------------------------------------",
    ]
    for item in analyses:
        mark = "✓" if item.status in (ResolutionStatus.RESOLVED, ResolutionStatus.FIX_VERIFIED) else "✗"
        lines.append(
            f" [{mark}] {item.status.value:15} | Rule: {item.rule_id:7} | {item.thread.path}:{item.thread.line}"
        )
        lines.append(f"     Symbol: {item.symbol_name}")
        lines.append(f"     Status: {item.explanation}")
        if item.suggested_reply:
            lines.append(f"     Reply:  {item.suggested_reply}")
        lines.append("")
    return "\n".join(lines)


def summarize_analyses(analyses: Sequence[ThreadAnalysis]) -> dict[str, int]:
    """Generates a summary count of thread resolution statuses."""
    counts = Counter(a.status for a in analyses)
    return {
        "total": len(analyses),
        "resolved": counts[ResolutionStatus.RESOLVED],
        "fix_verified": counts[ResolutionStatus.FIX_VERIFIED],
        "action_required": counts[ResolutionStatus.ACTION_REQUIRED],
        "orphaned_path": counts[ResolutionStatus.ORPHANED_PATH],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for PR thread synchronization."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    threads = _load_threads_from_input(args.input)
    analyses = synchronize_threads(args.root, threads, args.base_root, args.left_root)

    _emit_outputs(analyses, args)

    if args.exit_code and any(a.status == ResolutionStatus.ACTION_REQUIRED for a in analyses):
        return 1
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    """Constructs command line argument parser."""
    parser = argparse.ArgumentParser(
        description="Closed-Loop PR Review Thread Synchronizer & Sequential Resolver."
    )
    parser.add_argument("--input", "-i", type=Path, help="Path to input JSON file containing review threads.")
    parser.add_argument(
        "--root", "-r", type=Path, default=Path("."), help="Root repository directory (default: .)."
    )
    parser.add_argument(
        "--base-root",
        type=Path,
        help=(
            "Directory with the base version of the files: the version that the review was made on, "
            "for example a git worktree of the reviewed commit. It names the definition at each "
            "thread's review-time line and gives the old complexity in CC001 replies."
        ),
    )
    parser.add_argument(
        "--left-root",
        type=Path,
        help=(
            "Directory with the left side of the reviewed diff: the pull request's base commit at "
            "review time. It names the definition at the line of each LEFT-side thread (a comment "
            "on a deleted line). Without it, such a thread uses its diff hunk, else stays unresolved."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit structured machine-readable JSON output.")
    parser.add_argument("--sarif", type=Path, help="Export OASIS SARIF 2.1.0 payload to target file.")
    parser.add_argument(
        "--resolve-verified", action="store_true", help="Print GraphQL mutations for verified threads."
    )
    parser.add_argument(
        "--exit-code", action="store_true", help="Exit 1 if any ACTION_REQUIRED threads remain."
    )
    return parser


def _emit_outputs(analyses: Sequence[ThreadAnalysis], args: argparse.Namespace) -> None:
    """Dispatches output rendering according to CLI arguments."""
    if args.json:
        print(json.dumps([a.to_dict() for a in analyses], indent=2))
    else:
        print(render_terminal_summary(analyses))

    if args.sarif:
        sarif_payload = export_sarif(analyses)
        args.sarif.write_text(json.dumps(sarif_payload, indent=2), encoding="utf-8")

    if args.resolve_verified:
        _print_verified_mutations(analyses)


def _load_threads_from_input(input_path: Path | None) -> list[ReviewThread]:
    """Loads review threads from file or stdin."""
    if not input_path or not input_path.exists():
        return []
    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
        return parse_review_threads(data)
    except (json.JSONDecodeError, OSError):
        return []


def _print_verified_mutations(analyses: Sequence[ThreadAnalysis]) -> None:
    """Prints GraphQL resolution mutations for verified threads."""
    verified = [a for a in analyses if a.status == ResolutionStatus.FIX_VERIFIED]
    if not verified:
        print("# Zero verified threads pending resolution.")
        return
    print(f"# GraphQL Mutations to Resolve {len(verified)} Verified Threads:")
    print("# Each document runs its reply, then its resolve; a failed resolve does not undo the reply.")
    for item in verified:
        print(generate_resolution_mutation(item.thread.thread_id, item.suggested_reply))


if __name__ == "__main__":
    sys.exit(main())
