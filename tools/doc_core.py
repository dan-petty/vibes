#!/usr/bin/env python3
"""Shared foundation for the documentation validation rules.

Extracted so that rule modules depend on types and fence mechanics without depending
on each other or on the validator that orchestrates them. A rule module importing the
validator would be a cycle; this module is the only thing they share.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Sequence

# Supported documentation extensions
SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset({".md", ".markdown"})


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
