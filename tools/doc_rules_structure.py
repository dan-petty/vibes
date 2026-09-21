#!/usr/bin/env python3
"""Structural rules: required observation sections and embedded directory maps.

Both rules validate a document's shape against something outside the text — a required
five-section skeleton, or the filesystem the document claims to describe. Neither can be
checked by reading the prose, which is why a text tree drifts silently until an oracle
diffs it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Sequence

from doc_core import DocFinding, extract_fenced_blocks

_TREE_ENTRY_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<indent>(?:[\u2502]   |    )*)(?:\u251c\u2500\u2500|\u2514\u2500\u2500) (?P<name>\S+)"
)

# Filesystem entries a directory map is never expected to enumerate.
TREE_IGNORED_NAMES: Final[frozenset[str]] = frozenset({"__pycache__", "node_modules"})
# A tree line consisting of an ellipsis marks the listing as deliberately partial.
TREE_ELLIPSIS: Final[frozenset[str]] = frozenset({"...", "\u2026"})

_OBSERVATION_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"^\d+-.+\.md$")
_OBSERVATION_NUMBERED_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^##\s+(\d+)\.")

# A pattern declares this metadata block so a reader can judge applicability before
# reading the body. Three competing vocabularies (Pattern Type, Category, prose subtitle)
# had accumulated across 19 files before this was made mechanical.
PATTERN_REQUIRED_FIELDS: Final[tuple[str, ...]] = ("Pattern Class", "Problem", "Solution")
_PATTERN_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(^|/)patterns/[a-z0-9-]+\.md$")

# Observations must have numbered sections 1-5 (## 1. ... through ## 5. ...)
OBSERVATION_REQUIRED_SECTION_COUNT: Final[int] = 5


@dataclass(frozen=True)
class TreeEntry:
    """A single path declared by an embedded directory map."""

    line_number: int
    path: Path
    is_directory: bool


def _parse_tree_line(line: str) -> tuple[int, str, bool] | None:
    """Return (depth, name, is_directory) for a tree line, or None if it is not one."""
    match = _TREE_ENTRY_RE.match(line)
    if not match:
        return None
    name = match.group("name")
    return len(match.group("indent")) // 4, name.rstrip("/"), name.endswith("/")


def _tree_entries(block: Sequence[tuple[int, str]], root: Path) -> list[TreeEntry]:
    """Resolve an embedded tree into filesystem paths, or [] if the listing is partial."""
    entries: list[TreeEntry] = []
    ancestors: dict[int, Path] = {}
    for line_no, text in block:
        parsed = _parse_tree_line(text)
        if not parsed:
            continue
        depth, name, is_directory = parsed
        if name in TREE_ELLIPSIS:
            return []
        path = ancestors.get(depth - 1, root) / name
        ancestors[depth] = path
        entries.append(TreeEntry(line_no, path, is_directory))
    return entries


def _path_kind(path: Path, cache: dict[Path, str | None]) -> str | None:
    """Return "dir", "file", or None for a path, stat-ing it at most once per document.

    Every mapped entry was previously stat-ed twice — once to decide whether the block is
    a map at all, then again to check the entry exists with its declared kind. On a
    129-entry map over a network or virtualised filesystem that doubling is the dominant
    cost of validating the document.
    """
    if path not in cache:
        try:
            cache[path] = "dir" if path.is_dir() else ("file" if path.is_file() else None)
        except OSError:
            cache[path] = None
    return cache[path]


def _is_directory_map(entries: Sequence[TreeEntry], cache: dict[Path, str | None]) -> bool:
    """Distinguish a filesystem map from other art drawn with the same box characters.

    Trace waterfalls and AST dumps use the identical `\u251c\u2500\u2500` glyphs. A map is
    identified structurally: it marks at least one child as a directory with a trailing
    slash, and most of what it names exists on disk. A map whose every single entry has
    vanished is indistinguishable from unrelated art without guessing, and is not reported.
    """
    if not entries or not any(entry.is_directory for entry in entries):
        return False
    resolved = sum(1 for entry in entries if _path_kind(entry.path, cache) is not None)
    return resolved * 2 >= len(entries)


def _check_tree_entry_exists(
    entry: TreeEntry, root: Path, file_str: str, cache: dict[Path, str | None]
) -> DocFinding | None:
    """Verify a mapped path still exists on disk with the declared kind."""
    expected = "dir" if entry.is_directory else "file"
    if _path_kind(entry.path, cache) == expected:
        return None
    kind = "directory" if entry.is_directory else "file"
    return DocFinding(
        file_path=file_str,
        line_number=entry.line_number,
        category="directory_map",
        message=(
            f"Directory map lists {kind} '{entry.path.relative_to(root)}', which does not exist. "
            "A text tree breaks no link and fails no test, so it drifts silently — regenerate it."
        ),
    )


def _undeclared_children(parent: Path, declared: set[str], kinds: set[bool]) -> list[str]:
    """Return on-disk children of the kinds the map enumerates that the map omits."""
    candidates = (
        child
        for child in sorted(parent.iterdir())
        if not child.name.startswith(".")
        and child.name not in TREE_IGNORED_NAMES
        and child.is_dir() in kinds
    )
    return [child.name for child in candidates if child.name not in declared]


def _check_tree_completeness(
    entries: Sequence[TreeEntry], root: Path, file_str: str
) -> list[DocFinding]:
    """Report children absent from a map that already enumerates that kind of sibling."""
    children: dict[Path, list[TreeEntry]] = {}
    for entry in entries:
        children.setdefault(entry.path.parent, []).append(entry)
    findings: list[DocFinding] = []
    for parent, listed in children.items():
        if not parent.is_dir():
            continue
        declared = {entry.path.name for entry in listed}
        missing = _undeclared_children(parent, declared, {entry.is_directory for entry in listed})
        findings.extend(
            _missing_child_finding(parent, root, name, listed[0].line_number, file_str)
            for name in missing
        )
    return findings


def _missing_child_finding(
    parent: Path, root: Path, name: str, line_number: int, file_str: str
) -> DocFinding:
    """Build a finding for a filesystem entry the map claims to enumerate but omits."""
    location = parent.relative_to(root) if parent != root else Path(".")
    return DocFinding(
        file_path=file_str,
        line_number=line_number,
        category="directory_map",
        message=(
            f"Directory map enumerates '{location}' but omits '{name}'. Either list it or mark the "
            "listing partial with an ellipsis entry."
        ),
    )


def check_directory_maps(lines: Sequence[str], file_path: Path) -> list[DocFinding]:
    """Diff every embedded text directory tree against the filesystem it describes."""
    root = file_path.parent.resolve()
    if not root.is_dir():
        return []
    findings: list[DocFinding] = []
    cache: dict[Path, str | None] = {}
    for block in _extract_text_blocks(lines):
        entries = _tree_entries(block, root)
        if not _is_directory_map(entries, cache):
            continue
        findings.extend(
            finding
            for finding in (
                _check_tree_entry_exists(entry, root, str(file_path), cache) for entry in entries
            )
            if finding
        )
        findings.extend(_check_tree_completeness(entries, root, str(file_path)))
    return findings


def _extract_text_blocks(lines: Sequence[str]) -> list[list[tuple[int, str]]]:
    """Extract fenced `text` blocks as (line_no, content) pairs."""
    return [body for _, body in extract_fenced_blocks(lines, "text")]


def _is_observation_file(file_path: Path) -> bool:
    """Predicate: true if file is a numbered observation doc under observations/."""
    return (
        "observations" in file_path.parts
        and _OBSERVATION_FILENAME_RE.match(file_path.name) is not None
    )


def _extract_numbered_sections(lines: Sequence[str]) -> set[int]:
    """Extract numbered section indices (## N. ...) from markdown outside fences."""
    found: set[int] = set()
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _OBSERVATION_NUMBERED_SECTION_RE.match(stripped)
        if m:
            found.add(int(m.group(1)))
    return found


def _missing_numbered_sections(found: set[int]) -> list[int]:
    """Return required section numbers (1–N) absent from the found set."""
    return [n for n in range(1, OBSERVATION_REQUIRED_SECTION_COUNT + 1) if n not in found]


def _is_pattern_file(file_path: Path) -> bool:
    """Identify a pattern document by its location, not by guessing from content."""
    return _PATTERN_PATH_RE.search(file_path.as_posix()) is not None and file_path.name != "README.md"


def check_pattern_header(lines: Sequence[str], file_path: Path) -> list[DocFinding]:
    """Verify a pattern declares its metadata block with the canonical field names."""
    if not _is_pattern_file(file_path):
        return []
    header = "\n".join(lines[:12])
    missing = [field for field in PATTERN_REQUIRED_FIELDS if f"**{field}**:" not in header]
    if not missing:
        return []
    return [
        DocFinding(
            file_path=str(file_path),
            line_number=1,
            category="pattern_header",
            message=(
                f"Pattern header omits {', '.join(missing)}. Declare the metadata block in the "
                "first 12 lines so a reader can judge applicability before reading the body: "
                "> **Pattern Class**, > **Problem**, > **Solution**, and optionally "
                "> **Reference Implementation**."
            ),
        )
    ]


def check_observation_structure(
    lines: Sequence[str], file_path: Path
) -> list[DocFinding]:
    """Verify that observation documents contain numbered sections ## 1. through ## 5."""
    if not _is_observation_file(file_path):
        return []
    found = _extract_numbered_sections(lines)
    missing = _missing_numbered_sections(found)
    return [
        DocFinding(
            file_path=str(file_path),
            line_number=1,
            category="observation_structure",
            message=(
                f"Observation document missing required numbered section '## {n}.'. "
                f"Sections ## 1. through ## {OBSERVATION_REQUIRED_SECTION_COUNT}. are mandatory."
            ),
            severity="error",
        )
        for n in missing
    ]
