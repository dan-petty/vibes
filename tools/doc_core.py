#!/usr/bin/env python3
"""Shared foundation for the documentation validation rules.

Extracted so that rule modules depend on types and fence mechanics without depending
on each other or on the validator that orchestrates them. A rule module importing the
validator would be a cycle; this module is the only thing they share.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

# Supported documentation extensions
SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset({".md", ".markdown"})


def _scan_directory(parent: Path) -> dict[str, str]:
    """Return {name: kind} for one directory, treating an unreadable path as empty."""
    try:
        with os.scandir(parent) as entries:
            return {entry.name: ("dir" if entry.is_dir() else "file") for entry in entries}
    except OSError:
        return {}


class PathOracle:
    """Answer existence questions from one `scandir` per directory, shared across a sweep.

    Every rule that consults the filesystem asks overlapping questions: link rules stat
    each target, map rules stat each entry, and one directory backs many documents. A
    stat costs ~1.8ms on the virtualised mount this repository is developed on against
    ~0.001ms on tmpfs, so the *number* of filesystem round trips, not the work between
    them, sets the wall clock. Scoping one oracle to a whole corpus sweep rather than to
    a single document is what collapses that count.
    """

    def __init__(self) -> None:
        self._listings: dict[Path, dict[str, str]] = {}
        self._locks: dict[Path, threading.Lock] = {}
        self._lock = threading.Lock()

    def _cached(self, parent: Path) -> dict[str, str] | None:
        """Return an already-scanned listing, or None if this directory is still unscanned."""
        with self._lock:
            return self._listings.get(parent)

    def _scan_lock(self, parent: Path) -> threading.Lock:
        """Return the lock that admits exactly one thread to the first scan of a directory."""
        with self._lock:
            return self._locks.setdefault(parent, threading.Lock())

    def _store(self, parent: Path, listing: dict[str, str]) -> None:
        """Publish a completed scan to every thread that asks for this directory next."""
        with self._lock:
            self._listings[parent] = listing

    def listing(self, parent: Path) -> dict[str, str]:
        """Return {name: kind} for a directory, scanning it at most once per oracle.

        The sweep runs a thread per document, so without a per-directory lock every
        thread that starts before the first scan finishes repeats it — the saving
        would depend on scheduling. Each directory gets its own lock so that
        concurrent scans of *different* directories still overlap.
        """
        cached = self._cached(parent)
        if cached is not None:
            return cached
        with self._scan_lock(parent):
            settled = self._cached(parent)
            if settled is not None:
                return settled
            scanned = _scan_directory(parent)
            self._store(parent, scanned)
            return scanned

    def kind(self, path: Path) -> str | None:
        """Return "dir", "file", or None for a path, using its parent's cached listing."""
        name = path.name
        if name in ("", ".."):
            # A directory listing cannot answer for a path that names no entry within it:
            # `a/..` and a filesystem root have no row in their own parent. Ask directly.
            if path.is_dir():
                return "dir"
            return "file" if path.exists() else None
        return self.listing(path.parent).get(name)

    def exists(self, path: Path) -> bool:
        """Report whether a path is present on disk, of any kind."""
        return self.kind(path) is not None


@dataclass(frozen=True)
class DocFinding:
    """A single syntax, link, or structural issue detected in documentation."""

    file_path: str
    line_number: int
    category: str
    message: str
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        """Serialize finding to dictionary."""
        return asdict(self)


@dataclass
class DocValidationReport:
    """Consolidated summary report of all audited documentation files."""

    total_files: int = 0
    files_with_findings: int = 0
    error_count: int = 0
    warning_count: int = 0
    is_valid: bool = True
    findings: list[DocFinding] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize report to dictionary."""
        return {
            "total_files": self.total_files,
            "files_with_findings": self.files_with_findings,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "is_valid": self.is_valid,
            "findings": [f.to_dict() for f in self.findings],
            "duration_seconds": round(self.duration_seconds, 4),
        }


def closing_fence_index(lines: Sequence[str], start: int) -> int:
    """Return the index of the fence closing the block opened at start, or end of input."""
    closers = (idx for idx in range(start + 1, len(lines)) if lines[idx].strip().startswith("```"))
    return next(closers, len(lines))


def extract_fenced_blocks(
    lines: Sequence[str], info: str
) -> list[tuple[int, list[tuple[int, str]]]]:
    """Extract every fenced block carrying the given info string.

    Returns (1-based opening line, [(1-based line number, raw text)]). Shared by the
    Mermaid and directory-map rules: two hand-rolled fence state machines previously
    drifted apart, and each one nested an elif ladder deep enough to strain the caps.
    """
    blocks: list[tuple[int, list[tuple[int, str]]]] = []
    consumed = -1
    for start, line in enumerate(lines):
        if start <= consumed or not line.strip().startswith(f"```{info}"):
            continue
        consumed = closing_fence_index(lines, start)
        blocks.append((start + 1, [(idx + 1, lines[idx]) for idx in range(start + 1, consumed)]))
    return blocks


def first_directive_line(m_lines: Sequence[str]) -> str:
    """Return the first non-blank, non-comment line of a fenced diagram block."""
    return next(
        (
            line_text.strip()
            for line_text in m_lines
            if line_text.strip() and not line_text.strip().startswith("%%")
        ),
        "",
    )


_FENCE_LINE: Final[re.Pattern[str]] = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def fenced_line_flags(lines: Sequence[str]) -> list[bool]:
    """Return, per line, whether it lies inside (or is) a fenced code block.

    CommonMark §4.5: a fence is three or more backticks **or** three or more tildes, the
    two cannot be mixed, and a closing fence must use the same character and be at least as
    long as the opener. Five call sites across this package instead toggled a boolean on
    any line starting with ``` or ~~~, so a `~~~markdown` block containing a ``` line
    closed the block early and inverted the state for the rest of the document — every
    link, tag and heading after it judged in the wrong context, silently.

    An info string after a closing fence is not a close (§4.5), so it is ignored here too.
    """
    flags: list[bool] = []
    marker: str | None = None
    length = 0
    for line in lines:
        match = _FENCE_LINE.match(line)
        if match is None:
            flags.append(marker is not None)
            continue
        run, info = match.group(1), match.group(2).strip()
        if marker is None:
            marker, length = run[0], len(run)
        elif run[0] == marker and len(run) >= length and not info:
            marker, length = None, 0
        flags.append(True)
    return flags


@dataclass(frozen=True)
class MarkdownLink:
    """One inline link, as the CommonMark parser resolved it."""

    href: str
    title: str
    line: int


@dataclass(frozen=True)
class MarkdownHeading:
    """One heading, however it was spelled."""

    text: str
    line: int


@dataclass(frozen=True)
class ParsedDocument:
    """A document read through the CommonMark parser rather than through regexes.

    Every rule that used to scan raw lines can be expressed against this, and several
    defects disappear rather than getting fixed:

    * a `~~~` block containing a backtick fence is one `fence` token, so no toggle can
      desynchronise;
    * an indented code block is a `code_block` token, so its example links are not resolved
      as paths;
    * a multi-line `<!-- ... -->` is one `html_block`, so tags inside it are not counted as
      live markup;
    * a setext heading is a `heading_open`, so it produces an anchor like any other;
    * `[text](url)` inside a code span is a `code_inline` child and never a link, without
      masking anything;
    * a link's title is an attribute, so it can never be mistaken for part of the
      destination, and `<a b.md>` and `a%20b.md` arrive already normalised.
    """

    links: tuple[MarkdownLink, ...]
    headings: tuple[MarkdownHeading, ...]
    code_lines: frozenset[int]
    html_lines: frozenset[int]


def parse_document(parser: Any, content: str) -> ParsedDocument:
    """Read a document once and return what the rules need from it."""
    state = _ParseState()
    for token in parser.parse(content):
        handler = _TOKEN_HANDLERS.get(token.type)
        if handler:
            handler(state, token)
    return ParsedDocument(
        links=tuple(state.links),
        headings=tuple(state.headings),
        code_lines=frozenset(state.code_lines),
        html_lines=frozenset(state.html_lines),
    )


@dataclass
class _ParseState:
    """What one pass over the token stream accumulates."""

    links: list[MarkdownLink] = field(default_factory=list)
    headings: list[MarkdownHeading] = field(default_factory=list)
    code_lines: set[int] = field(default_factory=set)
    html_lines: set[int] = field(default_factory=set)
    pending_heading: int | None = None


def _on_code(state: _ParseState, token: Any) -> None:
    """Record the lines a fenced or indented code block occupies."""
    state.code_lines.update(_token_lines(token))


def _on_html(state: _ParseState, token: Any) -> None:
    """Record the lines an HTML block occupies, comments included."""
    state.html_lines.update(_token_lines(token))


def _on_heading(state: _ParseState, token: Any) -> None:
    """Remember that the next inline token is a heading's text."""
    state.pending_heading = (token.map[0] + 1) if token.map else 0


def _on_inline(state: _ParseState, token: Any) -> None:
    """Collect the links in one inline run, and its text if it is a heading."""
    state.links.extend(_inline_links(token))
    if state.pending_heading is not None:
        state.headings.append(MarkdownHeading(token.content.strip(), state.pending_heading))
        state.pending_heading = None


# Table dispatch per §10.1. The predecessor was an `elif` ladder whose nesting the sentinel
# refused at depth 7 against a ceiling of 5.
_TOKEN_HANDLERS: dict[str, Any] = {
    "fence": _on_code,
    "code_block": _on_code,
    "html_block": _on_html,
    "heading_open": _on_heading,
    "inline": _on_inline,
}


def _token_lines(token: Any) -> range:
    """Return the 1-based line numbers a block token spans."""
    if not token.map:
        return range(0)
    return range(token.map[0] + 1, token.map[1] + 1)


def _inline_links(token: Any) -> list[MarkdownLink]:
    """Return every link in one inline token, with the line it actually sits on.

    An inline token spans a whole paragraph, so `token.map` gives the block's first line and
    not the link's. Four links on four consecutive lines all reported the first, which moves
    a finding away from the thing it is about. `softbreak` and `hardbreak` are the children
    that represent a newline inside a paragraph, so counting them walks the line forward
    exactly.
    """
    line = (token.map[0] + 1) if token.map else 0
    found: list[MarkdownLink] = []
    for child in token.children or []:
        if child.type in ("softbreak", "hardbreak"):
            line += 1
        elif child.type == "link_open":
            attrs = dict(child.attrs or {})
            found.append(MarkdownLink(str(attrs.get("href", "")), str(attrs.get("title", "")), line))
        else:
            line += child.content.count("\n")
    return found
