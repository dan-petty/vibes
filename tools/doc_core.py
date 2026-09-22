#!/usr/bin/env python3
"""Shared foundation for the documentation validation rules.

Extracted so that rule modules depend on types and fence mechanics without depending
on each other or on the validator that orchestrates them. A rule module importing the
validator would be a cycle; this module is the only thing they share.
"""

from __future__ import annotations

import os
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
