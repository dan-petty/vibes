#!/usr/bin/env python3
"""Mermaid diagram rules: label quoting, diagram type, accessibility, and grammar traps.

Textual rules cannot tell whether a diagram renders; `tools/verify_mermaid.mjs` parses
every block with the real engine and is the authoritative gate. These rules catch the
classes a parser cannot: inaccessible colour pairs, deprecated declarations, and the
semicolon that silently truncates a sequence diagram message.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from doc_core import DocFinding, extract_fenced_blocks, first_directive_line

# Valid Mermaid diagram declarations
VALID_MERMAID_TYPES: Final[frozenset[str]] = frozenset(
    {
        "flowchart", "graph", "sequencediagram", "classdiagram", "statediagram",
        "erdiagram", "gantt", "pie", "gitgraph", "mindmap", "timeline",
        "quadrantchart", "xychart", "block", "packet", "architecture",
    }
)

_MERMAID_NODE_RE: Final[re.Pattern[str]] = re.compile(r"([A-Za-z0-9_]+)\[([^\]]+)\]")
_MERMAID_EDGE_RE: Final[re.Pattern[str]] = re.compile(r"(-->|-\.->|==>)\|([^|]+)\|")
_MERMAID_FILL_RE: Final[re.Pattern[str]] = re.compile(r"fill:\s*(#[0-9a-fA-F]{3,6})")
_MERMAID_TEXT_COLOR_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w-])color:\s*(#[0-9a-fA-F]{3,6})")
_LEGACY_MERMAID_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"^graph\s+(?:TB|TD|BT|RL|LR)\b")
_SEQUENCE_STATEMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:\s*[Nn]ote\s|[^:]*(?:->>|-->>|-\)|--\)|-x|--x|->|-->))[^:]*:(.+)$"
)

# WCAG 2.1 AA contrast floor for normal text; mermaid renders node labels at body size.
MIN_MERMAID_CONTRAST_RATIO: Final[float] = 4.5


def _is_unquoted_parens_node_label(label: str) -> bool:
    """Predicate checking if node label contains unquoted parentheses."""
    if label.startswith('"') and label.endswith('"'):
        return False
    return ("(" in label) or (")" in label)


def _check_mermaid_node_label(line: str, line_no: int, file_str: str) -> list[DocFinding]:
    """Validate that node shape labels with parentheses are properly quoted."""
    findings: list[DocFinding] = []
    for match in _MERMAID_NODE_RE.finditer(line):
        node_id, label = match.group(1), match.group(2)
        if _is_unquoted_parens_node_label(label):
            findings.append(
                DocFinding(
                    file_path=file_str,
                    line_number=line_no,
                    category="mermaid",
                    message=(
                        f"Unquoted parentheses in node '{node_id}': '{label}'. "
                        f'Wrap in double quotes: {node_id}["{label}"]'
                    ),
                )
            )
    return findings


def _is_unquoted_special_edge_label(label: str) -> bool:
    """Predicate checking if edge label contains unquoted special characters."""
    if label.startswith('"') and label.endswith('"'):
        return False
    return any(c in label for c in ("(", ")", ">", "<"))


def _check_mermaid_edge_label(line: str, line_no: int, file_str: str) -> list[DocFinding]:
    """Validate that edge labels with parentheses or special characters are properly quoted."""
    findings: list[DocFinding] = []
    for match in _MERMAID_EDGE_RE.finditer(line):
        label = match.group(2).strip()
        if _is_unquoted_special_edge_label(label):
            findings.append(
                DocFinding(
                    file_path=file_str,
                    line_number=line_no,
                    category="mermaid",
                    message=(
                        f"Unquoted special characters in Mermaid edge label: '|{label}|'. "
                        f'Wrap in double quotes: |"{label}"|'
                    ),
                )
            )
    return findings


def _expand_hex_color(hex_color: str) -> tuple[int, int, int]:
    """Expand a 3- or 6-digit hex color into its 8-bit RGB channels."""
    digits = hex_color.lstrip("#")
    if len(digits) == 3:
        digits = "".join(channel * 2 for channel in digits)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _srgb_channel(value: int) -> float:
    """Linearize a single 8-bit sRGB channel per WCAG 2.1."""
    ratio = value / 255
    return ratio / 12.92 if ratio <= 0.03928 else ((ratio + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    """Compute WCAG 2.1 relative luminance for a hex color."""
    red, green, blue = _expand_hex_color(hex_color)
    return 0.2126 * _srgb_channel(red) + 0.7152 * _srgb_channel(green) + 0.0722 * _srgb_channel(blue)


def contrast_ratio(foreground: str, background: str) -> float:
    """Return the WCAG 2.1 contrast ratio between two hex colors (1.0 to 21.0)."""
    luminances = (_relative_luminance(foreground), _relative_luminance(background))
    return (max(luminances) + 0.05) / (min(luminances) + 0.05)


def _check_mermaid_style_line(line: str, line_no: int, file_str: str) -> list[DocFinding]:
    """Verify every styled mermaid node declares legible, theme-independent colors."""
    fill = _MERMAID_FILL_RE.search(line)
    if not fill:
        return []
    text_color = _MERMAID_TEXT_COLOR_RE.search(line)
    if not text_color:
        return [
            DocFinding(
                file_path=file_str,
                line_number=line_no,
                category="mermaid_style",
                message=(
                    f"Mermaid fill '{fill.group(1)}' declares no explicit 'color:'. Inherited label "
                    "color flips between GitHub light and dark themes, rendering the node illegible in one."
                ),
            )
        ]
    ratio = contrast_ratio(text_color.group(1), fill.group(1))
    if ratio < MIN_MERMAID_CONTRAST_RATIO:
        return [
            DocFinding(
                file_path=file_str,
                line_number=line_no,
                category="mermaid_style",
                message=(
                    f"Mermaid contrast {ratio:.2f}:1 between color '{text_color.group(1)}' and fill "
                    f"'{fill.group(1)}' is below the WCAG AA floor of {MIN_MERMAID_CONTRAST_RATIO}:1."
                ),
            )
        ]
    return []


def _check_sequence_semicolon(line: str, line_no: int, file_str: str) -> list[DocFinding]:
    """Reject semicolons in sequence diagram text, where they terminate the statement."""
    match = _SEQUENCE_STATEMENT_RE.match(line)
    if not match or ";" not in match.group(1):
        return []
    return [
        DocFinding(
            file_path=file_str,
            line_number=line_no,
            category="mermaid",
            message=(
                "Semicolon in sequence diagram text. Mermaid treats ';' as a statement separator, "
                "so the message or note is truncated at that point and the remainder fails to parse. "
                "Use a comma or a full stop."
            ),
        )
    ]


def _check_legacy_mermaid_header(first: str, m_start: int, file_str: str) -> DocFinding | None:
    """Reject the legacy `graph` declaration in favour of modern `flowchart`."""
    if not _LEGACY_MERMAID_HEADER_RE.match(first):
        return None
    return DocFinding(
        file_path=file_str,
        line_number=m_start,
        category="mermaid",
        message=(
            f"Legacy Mermaid declaration '{first}'. Use 'flowchart {first.split()[1]}' — "
            "`graph` is the deprecated alias and does not support the full modern edge grammar."
        ),
    )


def _validate_mermaid_header(
    m_lines: list[str], m_start: int, file_str: str
) -> DocFinding | None:
    """Validate that the first non-comment line of a mermaid block specifies a known diagram type."""
    first = first_directive_line(m_lines)
    if first and not any(first.lower().startswith(vt) for vt in VALID_MERMAID_TYPES):
        return DocFinding(
            file_path=file_str,
            line_number=m_start,
            category="mermaid",
            message=f"Unrecognized Mermaid diagram type: '{first}'",
        )
    return None


def _extract_mermaid_blocks(lines: Sequence[str]) -> list[tuple[int, list[tuple[int, str]]]]:
    """Extract mermaid code blocks as (start_line, [(line_no, line_content)])."""
    return extract_fenced_blocks(lines, "mermaid")


def _validate_single_mermaid_block(
    start_line: int,
    block_lines: Sequence[tuple[int, str]],
    file_str: str,
) -> list[DocFinding]:
    """Validate header, node labels, and edge labels within a single mermaid block."""
    raw_lines = [text for _, text in block_lines]
    findings: list[DocFinding] = []
    first = first_directive_line(raw_lines)
    findings.extend(
        finding
        for finding in (
            _validate_mermaid_header(raw_lines, start_line, file_str),
            _check_legacy_mermaid_header(first, start_line, file_str),
        )
        if finding
    )
    is_sequence = first.lower().startswith("sequencediagram")
    for line_no, text in block_lines:
        findings.extend(_check_mermaid_node_label(text, line_no, file_str))
        findings.extend(_check_mermaid_edge_label(text, line_no, file_str))
        findings.extend(_check_mermaid_style_line(text, line_no, file_str))
        if is_sequence:
            findings.extend(_check_sequence_semicolon(text, line_no, file_str))
    return findings


def check_mermaid_diagrams(lines: Sequence[str], file_path: Path) -> list[DocFinding]:
    """Inspect all mermaid diagrams for syntax structure and unquoted characters."""
    file_str = str(file_path)
    findings: list[DocFinding] = []
    for start_line, block_lines in _extract_mermaid_blocks(lines):
        findings.extend(_validate_single_mermaid_block(start_line, block_lines, file_str))
    return findings
