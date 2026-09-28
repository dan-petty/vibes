#!/usr/bin/env python3
"""Configurable prose style, terminology, and inclusive language rules for documentation.

Extracted so that prose style guides (Google Developer Style Guide, Microsoft Writing
Style Guide) can be enforced on documentation text without coupling rule implementations
to the validator CLI or CommonMark parsing orchestrators.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from doc_core import DocFinding, fenced_line_flags

# Regular expression removing inline backtick code spans from prose
_INLINE_CODE_SPAN_RE: Final[re.Pattern[str]] = re.compile(r"`[^`]*`")

# Repeated doubled words in prose (e.g. "the the", "in in")
_DOUBLED_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(the|in|of|to|and|is|that|for|it|with|on)\s+\1\b", re.IGNORECASE
)

# Unresolved development placeholders
_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(TODO|FIXME|TBD|LOREM\s+IPSUM)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class StyleTermRule:
    """A terminology rule defining a deprecated term and its recommended replacement."""

    pattern: re.Pattern[str]
    replacement: str
    guide: str
    severity: str = "error"


# Google and Microsoft Style Guide Terminology Rules
STYLE_TERMINOLOGY_RULES: Final[tuple[StyleTermRule, ...]] = (
    StyleTermRule(
        pattern=re.compile(r"\bblacklist(?:ed|ing|s)?\b", re.IGNORECASE),
        replacement="blocklist / denylist",
        guide="Google/Microsoft Developer Style Guide",
    ),
    StyleTermRule(
        pattern=re.compile(r"\bwhitelist(?:ed|ing|s)?\b", re.IGNORECASE),
        replacement="allowlist",
        guide="Google/Microsoft Developer Style Guide",
    ),
    StyleTermRule(
        pattern=re.compile(r"\bmaster[-/]slave\b", re.IGNORECASE),
        replacement="primary/replica or main/worker",
        guide="Google/Microsoft Developer Style Guide",
    ),
    StyleTermRule(
        pattern=re.compile(r"\bsanity[- ]check(?:ed|ing|s)?\b", re.IGNORECASE),
        replacement="coherence check or validity check",
        guide="Google/Microsoft Developer Style Guide",
    ),
)


def _strip_inline_code(line: str) -> str:
    """Remove inline backtick code spans from line to prevent false matches on code symbols."""
    return _INLINE_CODE_SPAN_RE.sub(" __code__ ", line)


def _check_terminology_rules(
    clean_line: str, line_no: int, file_str: str, guide: str | None = None
) -> list[DocFinding]:
    """Check line for non-inclusive or deprecated terminology."""
    findings: list[DocFinding] = []
    for rule in STYLE_TERMINOLOGY_RULES:
        if guide and guide.lower() not in rule.guide.lower():
            continue
        match = rule.pattern.search(clean_line)
        if match:
            matched_text = match.group(0)
            findings.append(
                DocFinding(
                    file_path=file_str,
                    line_number=line_no,
                    category="prose_style",
                    message=(
                        f"Non-inclusive or deprecated term '{matched_text}' detected. "
                        f"Use '{rule.replacement}' per {rule.guide}."
                    ),
                    severity=rule.severity,
                )
            )
    return findings


def _check_doubled_words(clean_line: str, line_no: int, file_str: str) -> list[DocFinding]:
    """Check line for duplicated adjacent words (e.g. 'the the')."""
    match = _DOUBLED_WORD_RE.search(clean_line)
    if not match:
        return []
    word = match.group(1)
    return [
        DocFinding(
            file_path=file_str,
            line_number=line_no,
            category="prose_style",
            message=f"Duplicated word '{word} {word}' detected in prose.",
            severity="error",
        )
    ]


def _check_placeholders(clean_line: str, line_no: int, file_str: str) -> list[DocFinding]:
    """Check line for unresolved placeholders (TODO, FIXME, TBD, LOREM IPSUM)."""
    match = _PLACEHOLDER_RE.search(clean_line)
    if not match:
        return []
    marker = match.group(0).upper()
    return [
        DocFinding(
            file_path=file_str,
            line_number=line_no,
            category="prose_style",
            message=f"Unresolved placeholder '{marker}' detected in documentation.",
            severity="warning",
        )
    ]


def _check_prose_line(
    line: str, line_no: int, file_str: str, guide: str | None = None
) -> list[DocFinding]:
    """Audit single non-fenced line across all prose style rules."""
    clean_line = _strip_inline_code(line)
    if not clean_line.strip():
        return []
    findings: list[DocFinding] = []
    findings.extend(_check_terminology_rules(clean_line, line_no, file_str, guide))
    findings.extend(_check_doubled_words(clean_line, line_no, file_str))
    findings.extend(_check_placeholders(clean_line, line_no, file_str))
    return findings


def check_prose_style(
    lines: Sequence[str],
    file_path: Path,
    guide: str | None = None,
) -> list[DocFinding]:
    """Validate documentation prose style, terminology, and repeated words.

    Excludes fenced code blocks and inline code spans so that technical code
    identifiers (e.g. `whitelist`, `master`) are never falsely flagged.
    """
    file_str = str(file_path)
    fenced_flags = fenced_line_flags(lines)
    findings: list[DocFinding] = []

    for idx, (line, is_fenced) in enumerate(zip(lines, fenced_flags, strict=True)):
        if is_fenced:
            continue
        findings.extend(_check_prose_line(line, idx + 1, file_str, guide))

    return findings
