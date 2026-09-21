#!/usr/bin/env python3
"""Documentation Syntax, Code Fence Nesting, Mermaid AST, and Link Validator for Vibes.

Performs static validation of markdown documentation:
1. Code Fences: Unclosed code blocks and premature termination via inner 3-backtick fences.
2. Mermaid Blocks: Node labels with unquoted special characters (parentheses, brackets) and valid diagram types.
3. Tables: Row column count alignment with proper delimiter and backtick handling.
4. Links & Anchors: Broken relative file links, local anchor slugs, and host-specific URIs.
5. Code Snippets: Syntactic correctness for embedded Python, JSON, and YAML blocks.
6. HTML Tags: Pairing and balance for structural HTML tags (<details>, <summary>, etc.).
"""

from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import sys
import textwrap
import threading
import time
from typing import Any, Callable, Final, Sequence
import urllib.parse
import yaml
from markdown_it import MarkdownIt

# Supported documentation extensions
SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset({".md", ".markdown"})

# Valid Mermaid diagram declarations
VALID_MERMAID_TYPES: Final[frozenset[str]] = frozenset(
    {
        "flowchart",
        "graph",
        "sequencediagram",
        "classdiagram",
        "statediagram",
        "erdiagram",
        "gantt",
        "pie",
        "gitgraph",
        "mindmap",
        "timeline",
        "quadrantchart",
        "xychart",
        "block",
        "packet",
        "architecture",
    }
)

PAIRED_HTML_TAGS: Final[frozenset[str]] = frozenset(
    {"details", "summary", "div", "span", "table", "thead", "tbody", "tr", "th", "td"}
)
VOID_HTML_TAGS: Final[frozenset[str]] = frozenset({"br", "hr", "img", "input"})

# Observations must have numbered sections 1–5 (## 1. ... through ## 5. ...)
OBSERVATION_REQUIRED_SECTION_COUNT: Final[int] = 5

_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^(`{3,}|~{3,})(.*)$")
_SPACE_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[([^\]]+)\]\s+\(([^)]+)\)")
_MARKDOWN_LINK_RE: Final[re.Pattern[str]] = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")
_MERMAID_NODE_RE: Final[re.Pattern[str]] = re.compile(r"([A-Za-z0-9_]+)\[([^\]]+)\]")
_MERMAID_EDGE_RE: Final[re.Pattern[str]] = re.compile(r"(-->|-\.->|==>)\|([^|]+)\|")
_HTML_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<(/)?([a-zA-Z0-9]+)(?:\s+[^>]*)?>")
_FILE_URI_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[([^\]]*)\]\(file://(/[^)#\s]+)(#[^)\s]*)?\)")
_MERMAID_FILL_RE: Final[re.Pattern[str]] = re.compile(r"fill:\s*(#[0-9a-fA-F]{3,6})")
_MERMAID_TEXT_COLOR_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w-])color:\s*(#[0-9a-fA-F]{3,6})")
_LEGACY_MERMAID_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"^graph\s+(?:TB|TD|BT|RL|LR)\b")
_SEQUENCE_STATEMENT_RE: Final[re.Pattern[str]] = re.compile(r"^(?:\s*[Nn]ote\s|[^:]*(?:->>|-->>|-\)|--\)|-x|--x|->|-->))[^:]*:(.+)$")

# WCAG 2.1 AA contrast floor for normal text; mermaid renders node labels at body size.
MIN_MERMAID_CONTRAST_RATIO: Final[float] = 4.5


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


def slugify_heading(heading_text: str) -> str:
    """Generate GitHub-compatible anchor slug for a heading."""
    clean = re.sub(r"`([^`]+)`", r"\1", heading_text)
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
    clean = clean.lower()
    clean = re.sub(r"[^\w\s-]", "", clean)
    return re.sub(r"[\s_]+", "-", clean).strip("-")


def _extract_heading_slug(line: str) -> str | None:
    """Extract and slugify markdown heading text if line matches heading grammar."""
    m = re.match(r"^(#{1,6})\s+(.*)$", line)
    return slugify_heading(m.group(2).strip()) if m else None


def _extract_html_anchors(line: str) -> list[str]:
    """Extract explicit HTML anchor name and id attributes from line."""
    return [
        m.group(1)
        for m in re.finditer(r'<a\s+[^>]*(?:name|id)=["\']([^"\']+)["\']', line, re.I)
    ]


def extract_heading_anchors(content: str) -> set[str]:
    """Extract all heading anchor slugs and explicit HTML anchors from markdown."""
    anchors: set[str] = set()
    in_fence = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        slug = _extract_heading_slug(stripped)
        if slug:
            anchors.add(slug)
        anchors.update(_extract_html_anchors(line))
    return anchors


def _evaluate_fence_token(token: Any, file_str: str) -> DocFinding | None:
    """Evaluate individual token for suspicious empty or unclosed fence."""
    if token.type != "fence" or not token.map:
        return None
    start_l, end_l = token.map
    if not token.info and (end_l - start_l <= 1):
        return DocFinding(
            file_path=file_str,
            line_number=start_l + 1,
            category="code_fence",
            message=f"Suspicious unclosed or empty code fence at lines {start_l+1}-{end_l}",
        )
    return None


def _check_token_fences(tokens: Sequence[Any], file_str: str) -> list[DocFinding]:
    """Scan parsed tokens for malformed or empty code fences."""
    findings: list[DocFinding] = []
    for token in tokens:
        finding = _evaluate_fence_token(token, file_str)
        if finding:
            findings.append(finding)
    return findings


def _process_fence_line(
    chars: str,
    info: str,
    idx: int,
    file_str: str,
    state: list[Any],
    findings: list[DocFinding],
) -> None:
    """Process a single code fence line against active state."""
    in_fence, fence_char, fence_len, _ = state
    char_type, curr_len = chars[0], len(chars)
    if not in_fence:
        state[:] = [True, char_type, curr_len, idx]
        return
    if char_type != fence_char or curr_len < fence_len:
        return
    if info:
        findings.append(
            DocFinding(
                file_path=file_str,
                line_number=idx,
                category="code_fence",
                message=f"Nested code fence at line {idx} inside a {fence_len}-backtick block requires 4+ backticks",
            )
        )
    else:
        state[0] = False


def _check_line_fences(lines: Sequence[str], file_str: str) -> list[DocFinding]:
    """Inspect lines for unclosed code fences and nested blocks."""
    findings: list[DocFinding] = []
    state = [False, "", 0, 0]
    for idx, line in enumerate(lines, 1):
        m_fence = _FENCE_RE.match(line.strip())
        if m_fence:
            _process_fence_line(
                m_fence.group(1), m_fence.group(2).strip(), idx, file_str, state, findings
            )
    if state[0]:
        findings.append(
            DocFinding(
                file_path=file_str,
                line_number=state[3],
                category="code_fence",
                message=f"Unclosed code fence opened at line {state[3]}",
            )
        )
    return findings


def check_code_fences(
    lines: Sequence[str], file_path: Path, parser: MarkdownIt
) -> list[DocFinding]:
    """Verify code fences for unclosed blocks and premature termination via inner fences."""
    file_str = str(file_path)
    try:
        tokens = parser.parse("\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        return [
            DocFinding(
                file_path=file_str,
                line_number=1,
                category="parse_error",
                message=f"Markdown parser failed: {exc}",
            )
        ]
    findings = _check_token_fences(tokens, file_str)
    findings.extend(_check_line_fences(lines, file_str))
    return sorted(findings, key=lambda f: (f.line_number, f.category))


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


def _first_directive_line(m_lines: Sequence[str]) -> str:
    """Return the first non-blank, non-comment line of a mermaid block."""
    return next(
        (
            line_text.strip()
            for line_text in m_lines
            if line_text.strip() and not line_text.strip().startswith("%%")
        ),
        "",
    )


def _validate_mermaid_header(
    m_lines: list[str], m_start: int, file_str: str
) -> DocFinding | None:
    """Validate that the first non-comment line of a mermaid block specifies a known diagram type."""
    first = _first_directive_line(m_lines)
    if first and not any(first.lower().startswith(vt) for vt in VALID_MERMAID_TYPES):
        return DocFinding(
            file_path=file_str,
            line_number=m_start,
            category="mermaid",
            message=f"Unrecognized Mermaid diagram type: '{first}'",
        )
    return None


def _is_mermaid_start(stripped: str, in_block: bool) -> bool:
    """Check if line starts a mermaid block."""
    return not in_block and stripped.startswith("```mermaid")


def _extract_mermaid_blocks(lines: Sequence[str]) -> list[tuple[int, list[tuple[int, str]]]]:
    """Extract mermaid code blocks as (start_line, [(line_no, line_content)])."""
    blocks: list[tuple[int, list[tuple[int, str]]]] = []
    current: list[tuple[int, str]] = []
    start_line = 0
    in_block = False

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if _is_mermaid_start(stripped, in_block):
            in_block, start_line, current = True, idx, []
            continue
        if not in_block:
            continue
        if stripped.startswith("```"):
            blocks.append((start_line, current))
            in_block = False
            continue
        current.append((idx, line))
    return blocks


def _validate_single_mermaid_block(
    start_line: int,
    block_lines: Sequence[tuple[int, str]],
    file_str: str,
) -> list[DocFinding]:
    """Validate header, node labels, and edge labels within a single mermaid block."""
    raw_lines = [text for _, text in block_lines]
    findings: list[DocFinding] = []
    first = _first_directive_line(raw_lines)
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


def _mask_table_code_pipes(line: str) -> str:
    """Mask pipe characters inside backtick blocks with null bytes."""
    masked: list[str] = []
    in_code = False
    for ch in line:
        if ch == "`":
            in_code = not in_code
        is_masked_pipe = ch == "|" and in_code
        masked.append("\x00" if is_masked_pipe else ch)
    return "".join(masked)


def _split_table_row(line: str) -> list[str]:
    """Split a markdown table row by pipes while ignoring pipes inside backticks."""
    masked = _mask_table_code_pipes(line)
    parts = re.split(r"(?<!\\)\|", masked)
    if len(parts) >= 2 and parts[0].strip() == "" and parts[-1].strip() == "":
        parts = parts[1:-1]
    return [p.replace("\x00", "|").strip() for p in parts]


def _is_table_delimiter_row(cells: Sequence[str]) -> bool:
    """Predicate determining if row cells represent a markdown table header delimiter."""
    return bool(cells) and all(re.match(r"^:?-+:?$", c) for c in cells)


def _process_table_line(
    raw_cells: list[str],
    in_table: bool,
    header_cols: int,
    line_no: int,
    file_str: str,
) -> tuple[bool, int, DocFinding | None]:
    """Process a single table line, tracking header column state and reporting mismatches."""
    if _is_table_delimiter_row(raw_cells):
        return True, len(raw_cells), None
    if in_table and len(raw_cells) != header_cols:
        return True, header_cols, DocFinding(
            file_path=file_str,
            line_number=line_no,
            category="table",
            message=f"Table row column count mismatch: expected {header_cols}, found {len(raw_cells)}",
        )
    return in_table, header_cols, None


def _evaluate_table_line(
    stripped: str,
    in_table: bool,
    header_cols: int,
    line_no: int,
    file_str: str,
) -> tuple[bool, int, DocFinding | None]:
    """Evaluate candidate table line and return updated state and optional finding."""
    if "|" not in stripped:
        return False, header_cols, None
    raw_cells = _split_table_row(stripped)
    return _process_table_line(raw_cells, in_table, header_cols, line_no, file_str)


def check_markdown_tables(lines: Sequence[str], file_path: Path) -> list[DocFinding]:
    """Verify table row column counts match the table header definition."""
    file_str = str(file_path)
    findings: list[DocFinding] = []
    in_fence, in_table, header_cols = False, False, 0

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence, in_table = not in_fence, False
            continue
        if in_fence:
            in_table = False
            continue
        in_table, header_cols, finding = _evaluate_table_line(
            stripped, in_table, header_cols, idx, file_str
        )
        if finding:
            findings.append(finding)
    return findings


def _check_host_or_web_target(
    target: str, file_str: str, line_no: int
) -> tuple[bool, DocFinding | None]:
    """Inspect link target for absolute host URIs or external web links."""
    if target.startswith("file:///"):
        return True, DocFinding(
            file_path=file_str,
            line_number=line_no,
            category="link",
            message=f"Absolute host URI '{target}' should use a repository-relative link",
        )
    if target.startswith(("http://", "https://", "mailto:")):
        return True, None
    return False, None


def _check_local_anchor(
    fragment: str,
    file_path: Path,
    line_no: int,
    known_anchors: dict[Path, set[str]],
) -> DocFinding | None:
    """Validate internal document heading anchor reference."""
    if file_path not in known_anchors:
        return None
    target_anchors = known_anchors[file_path]
    clean_frag = re.sub(r"[^\w-]", "", fragment.lower())
    if not any(clean_frag in a or a in clean_frag for a in target_anchors):
        return DocFinding(
            file_path=str(file_path),
            line_number=line_no,
            category="link",
            message=f"Local anchor '#{fragment}' not found in {file_path.name}",
        )
    return None


def _check_path_target(path_part: str, file_path: Path, line_no: int) -> DocFinding | None:
    """Validate relative target path on filesystem."""
    target_path = file_path.parent / path_part
    if target_path.exists():
        return None
    # Fallback relative to repository root if testing from subdirectories
    repo_root_fallback = file_path.cwd() / path_part.lstrip("./")
    if repo_root_fallback.exists():
        return None
    return DocFinding(
        file_path=str(file_path),
        line_number=line_no,
        category="link",
        message=f"Target path does not exist: '{path_part}'",
    )


def _validate_link_target(
    target: str,
    file_path: Path,
    line_no: int,
    known_anchors: dict[Path, set[str]],
) -> DocFinding | None:
    """Validate a single markdown link destination against filesystem and heading anchors."""
    file_str = str(file_path)
    is_terminal, finding = _check_host_or_web_target(target, file_str, line_no)
    if is_terminal:
        return finding
    parsed = urllib.parse.urlparse(target)
    if not parsed.path and parsed.fragment:
        return _check_local_anchor(parsed.fragment, file_path, line_no, known_anchors)
    if parsed.path:
        return _check_path_target(parsed.path, file_path, line_no)
    return None


def _check_line_links(
    line: str,
    line_no: int,
    file_path: Path,
    file_str: str,
    effective_anchors: dict[Path, set[str]],
) -> list[DocFinding]:
    """Inspect a single markdown line for whitespace malformations and target link targets."""
    line_findings: list[DocFinding] = []
    if _SPACE_LINK_RE.search(line):
        line_findings.append(
            DocFinding(
                file_path=file_str,
                line_number=line_no,
                category="link",
                message=f"Malformed link with whitespace between brackets: '{line.strip()}'",
            )
        )
    for match in _MARKDOWN_LINK_RE.finditer(line):
        finding = _validate_link_target(match.group(2).strip(), file_path, line_no, effective_anchors)
        if finding:
            line_findings.append(finding)
    return line_findings


def _resolve_effective_anchors(
    file_path: Path,
    lines: Sequence[str],
    known_anchors: dict[Path, set[str]] | None,
) -> dict[Path, set[str]]:
    """Resolve known anchors map, populating local document anchors if missing."""
    anchors = dict(known_anchors) if known_anchors is not None else {}
    if file_path not in anchors:
        anchors[file_path] = extract_heading_anchors("\n".join(lines))
    return anchors


def check_markdown_links(
    lines: Sequence[str],
    file_path: Path,
    known_anchors: dict[Path, set[str]] | None = None,
) -> list[DocFinding]:
    """Inspect all markdown links for broken paths, missing anchors, and host-specific URIs."""
    file_str = str(file_path)
    findings: list[DocFinding] = []
    in_fence = False
    effective_anchors = _resolve_effective_anchors(file_path, lines, known_anchors)

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if not in_fence:
            findings.extend(_check_line_links(line, idx, file_path, file_str, effective_anchors))
    return findings


def _validate_python_snippet(code: str, start_line: int, file_str: str) -> DocFinding | None:
    """Validate syntax of an embedded Python code block."""
    dedented = textwrap.dedent(code)
    if re.search(r"^\s*(\.\.\.|…|>>>|\$|<[a-zA-Z0-9_-]+>)", dedented, re.M):
        return None
    if any(pline.strip().startswith(("+", "-", "@@")) for pline in code.splitlines()):
        return None
    try:
        ast.parse(dedented)
        return None
    except SyntaxError as exc:
        return DocFinding(
            file_path=file_str,
            line_number=start_line + (exc.lineno or 1) - 1,
            category="code_snippet",
            message=f"Python syntax error: {exc.msg}",
        )


def _is_json_container(text: str) -> bool:
    """Predicate checking if text is enclosed in braces or brackets."""
    if text.startswith("{"):
        return text.endswith("}")
    if text.startswith("["):
        return text.endswith("]")
    return False


def _validate_json_snippet(code: str, start_line: int, file_str: str) -> DocFinding | None:
    """Validate syntax of an embedded JSON code block."""
    dedented = textwrap.dedent(code).strip()
    if re.search(r"//|/\*|#|\.\.\.|<[a-zA-Z0-9_-]+>|\$\{", dedented):
        return None
    if not _is_json_container(dedented):
        return None
    try:
        json.loads(dedented)
        return None
    except json.JSONDecodeError as exc:
        return DocFinding(
            file_path=file_str,
            line_number=start_line,
            category="code_snippet",
            message=f"JSON syntax error: {exc.msg}",
        )


def _validate_yaml_snippet(code: str, start_line: int, file_str: str) -> DocFinding | None:
    """Validate syntax of an embedded YAML code block."""
    dedented = textwrap.dedent(code)
    if re.search(r"\.\.\.|<[a-zA-Z0-9_-]+>|\{\{|\}\}", dedented):
        return None
    try:
        list(yaml.safe_load_all(dedented))
        return None
    except yaml.YAMLError as exc:
        return DocFinding(
            file_path=file_str,
            line_number=start_line,
            category="code_snippet",
            message=f"YAML syntax error: {exc}",
        )


_SNIPPET_VALIDATORS: Final[
    dict[str, Callable[[str, int, str], DocFinding | None]]
] = {
    "python": _validate_python_snippet,
    "py": _validate_python_snippet,
    "json": _validate_json_snippet,
    "yaml": _validate_yaml_snippet,
    "yml": _validate_yaml_snippet,
}


def _validate_single_snippet(
    lang: str, code_lines: list[str], start_line: int, file_str: str
) -> DocFinding | None:
    """Dispatch code snippet to specialized validator based on language tag."""
    validator = _SNIPPET_VALIDATORS.get(lang.lower())
    if not validator:
        return None
    return validator("\n".join(code_lines), start_line, file_str)


def _handle_fence_transition(
    m_fence: re.Match[str],
    in_fence: bool,
    fence_lang: str,
    start_line: int,
    block_lines: list[str],
    line_no: int,
    file_str: str,
) -> tuple[bool, str, int, list[str], DocFinding | None]:
    """Handle state transition when entering or exiting a fenced code block."""
    if not in_fence:
        info = m_fence.group(2).strip().split()
        return True, info[0].lower() if info else "", line_no, [], None
    finding = _validate_single_snippet(fence_lang, block_lines, start_line, file_str)
    return False, "", 0, [], finding


def _process_snippet_line(
    line: str,
    idx: int,
    file_str: str,
    state: list[Any],
    findings: list[DocFinding],
) -> None:
    """Process a single line for embedded code snippets."""
    stripped = line.strip()
    m_fence = _FENCE_RE.match(stripped)
    in_fence, fence_lang, start_line, block_lines = state
    if m_fence:
        in_f, f_lang, s_line, b_lines, finding = _handle_fence_transition(
            m_fence, in_fence, fence_lang, start_line, block_lines, idx, file_str
        )
        state[:] = [in_f, f_lang, s_line, b_lines]
        if finding:
            findings.append(finding)
    elif in_fence:
        block_lines.append(line)


def check_embedded_snippets(lines: Sequence[str], file_path: Path) -> list[DocFinding]:
    """Syntactically parse embedded code blocks in supported languages."""
    file_str = str(file_path)
    findings: list[DocFinding] = []
    state: list[Any] = [False, "", 0, []]

    for idx, line in enumerate(lines, 1):
        _process_snippet_line(line, idx, file_str, state, findings)
    return findings


def _process_html_tag(
    is_closing: bool,
    tag: str,
    line_no: int,
    file_str: str,
    stack: list[tuple[str, int]],
) -> DocFinding | None:
    """Evaluate structural HTML opening/closing pairing on a stack."""
    if is_closing:
        if stack and stack[-1][0] == tag:
            stack.pop()
            return None
        return DocFinding(
            file_path=file_str,
            line_number=line_no,
            category="html_tag",
            message=f"Unexpected closing tag </{tag}> without matching open tag",
        )
    stack.append((tag, line_no))
    return None


def _evaluate_html_match(
    match: re.Match[str],
    line_no: int,
    file_str: str,
    stack: list[tuple[str, int]],
) -> DocFinding | None:
    """Evaluate a single HTML tag regex match against void/paired sets."""
    is_closing, tag = bool(match.group(1)), match.group(2).lower()
    if tag in VOID_HTML_TAGS or tag not in PAIRED_HTML_TAGS:
        return None
    return _process_html_tag(is_closing, tag, line_no, file_str, stack)


def _extract_line_html_findings(
    line: str,
    line_no: int,
    file_str: str,
    stack: list[tuple[str, int]],
) -> list[DocFinding]:
    """Scan unquoted text on a line for HTML tags and evaluate against tag stack."""
    clean = re.sub(r"`[^`]*`", "", re.sub(r"<!--.*?-->", "", line))
    findings: list[DocFinding] = []
    for match in _HTML_TAG_RE.finditer(clean):
        finding = _evaluate_html_match(match, line_no, file_str, stack)
        if finding:
            findings.append(finding)
    return findings


def check_html_tags(lines: Sequence[str], file_path: Path) -> list[DocFinding]:
    """Check pairing and nesting of structural HTML tags outside code blocks."""
    file_str = str(file_path)
    findings: list[DocFinding] = []
    in_fence, stack = False, []

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if not in_fence:
            findings.extend(_extract_line_html_findings(line, idx, file_str, stack))

    for tag, tag_line in stack:
        findings.append(
            DocFinding(
                file_path=file_str,
                line_number=tag_line,
                category="html_tag",
                message=f"Unclosed HTML tag <{tag}> was never closed",
            )
        )
    return findings


_OBSERVATION_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"^\d+-.+\.md$")


def _is_observation_file(file_path: Path) -> bool:
    """Predicate: true if file is a numbered observation doc under observations/."""
    return (
        "observations" in file_path.parts
        and _OBSERVATION_FILENAME_RE.match(file_path.name) is not None
    )


_OBSERVATION_NUMBERED_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^##\s+(\d+)\.")


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


def _discover_markdown_files(dir_path: Path, extensions: Sequence[str]) -> list[Path]:
    """Discover markdown files while pruning hidden directories and caches."""
    md_files: list[Path] = []
    ext_set = {ext.lower() for ext in extensions}
    for root, dirs, files in os.walk(dir_path):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in (".venv", "node_modules", "__pycache__")]
        matching = [Path(root) / f for f in files if (Path(root) / f).suffix.lower() in ext_set]
        md_files.extend(matching)
    return sorted(md_files)


def _fix_mermaid_line(line: str) -> tuple[str, int]:
    """Quote node and edge labels containing special characters in a single mermaid line."""
    fixes = 0
    new_line = line
    for match in _MERMAID_NODE_RE.finditer(line):
        node_id, label = match.group(1), match.group(2)
        if _is_unquoted_parens_node_label(label):
            target = f"{node_id}[{label}]"
            replacement = f'{node_id}["{label}"]'
            new_line = new_line.replace(target, replacement, 1)
            fixes += 1
    for match in _MERMAID_EDGE_RE.finditer(new_line):
        arrow, label = match.group(1), match.group(2)
        if _is_unquoted_special_edge_label(label.strip()):
            target = f"{arrow}|{label}|"
            replacement = f'{arrow}|"{label.strip()}"|'
            new_line = new_line.replace(target, replacement, 1)
            fixes += 1
    return new_line, fixes


def _process_mermaid_line(
    line: str, in_mermaid: bool
) -> tuple[str, bool, int]:
    """Process a single markdown line and apply Mermaid fixes if inside diagram."""
    stripped = line.strip()
    if stripped.startswith("```mermaid"):
        return line, True, 0
    if in_mermaid and stripped.startswith("```"):
        return line, False, 0
    if in_mermaid:
        fixed_line, count = _fix_mermaid_line(line)
        return fixed_line, True, count
    return line, False, 0


def _fix_mermaid_blocks(lines: list[str]) -> tuple[list[str], int]:
    """Fix unquoted Mermaid node and edge labels across all mermaid blocks."""
    in_mermaid = False
    total_fixes = 0
    result_lines: list[str] = []
    for line in lines:
        fixed_line, in_mermaid, count = _process_mermaid_line(line, in_mermaid)
        total_fixes += count
        result_lines.append(fixed_line)
    return result_lines, total_fixes


def _track_fence_marker(line: str, current_fence: str | None) -> str | None:
    """Update current unclosed fence marker based on line."""
    match = _FENCE_RE.match(line.strip())
    if not match:
        return current_fence
    marker = match.group(1)
    if current_fence is None:
        return marker
    if marker.startswith(current_fence[:3]):
        return None
    return current_fence


def _fix_unclosed_fences(lines: list[str]) -> tuple[list[str], int]:
    """Close any unclosed code fence at document end."""
    open_fence = None
    for line in lines:
        open_fence = _track_fence_marker(line, open_fence)
    if open_fence is not None:
        return [*lines, open_fence], 1
    return lines, 0


def _fix_absolute_links(content: str, doc_path: Path) -> tuple[str, int]:
    """Convert absolute file:/// links to relative markdown links."""
    fixes = 0
    doc_dir = doc_path.resolve().parent

    def _replace_uri(match: re.Match[str]) -> str:
        nonlocal fixes
        text, abs_path_str, anchor = match.group(1), match.group(2), match.group(3) or ""
        target_path = Path(abs_path_str)
        try:
            rel = os.path.relpath(target_path, doc_dir)
            fixes += 1
            return f"[{text}]({rel}{anchor})"
        except ValueError:
            return match.group(0)

    fixed_content = _FILE_URI_LINK_RE.sub(_replace_uri, content)
    return fixed_content, fixes


def auto_fix_content(content: str, doc_path: Path = Path("document.md")) -> tuple[str, int]:
    """Remediate fixable documentation issues in markdown string."""
    lines = content.splitlines()
    lines, fence_fixes = _fix_unclosed_fences(lines)
    lines, mermaid_fixes = _fix_mermaid_blocks(lines)
    reconstituted = "\n".join(lines)
    if content.endswith("\n") and not reconstituted.endswith("\n"):
        reconstituted += "\n"
    final_content, link_fixes = _fix_absolute_links(reconstituted, doc_path)
    return final_content, fence_fixes + mermaid_fixes + link_fixes


def auto_fix_file(file_path: Path) -> int:
    """Read file, apply automated remediations, and write back if modified."""
    if not file_path.is_file():
        return 0
    content = file_path.read_text(encoding="utf-8")
    fixed_content, total_fixes = auto_fix_content(content, file_path)
    if total_fixes > 0 and fixed_content != content:
        file_path.write_text(fixed_content, encoding="utf-8")
    return total_fixes


class DocsValidator:
    """Markdown documentation syntax, link integrity, and snippet validator for Vibes."""

    def __init__(self) -> None:
        self._local = threading.local()

    @property
    def parser(self) -> MarkdownIt:
        """Thread-isolated parser instance avoiding concurrent state corruption."""
        if not hasattr(self._local, "parser"):
            self._local.parser = MarkdownIt("commonmark")
        return self._local.parser

    def fix_file(self, file_path: Path) -> int:
        """Remediate documentation issues in file in-place."""
        return auto_fix_file(file_path)

    def fix_directory(
        self, dir_path: Path, extensions: Sequence[str] = (".md", ".markdown")
    ) -> int:
        """Remediate documentation issues across directory in-place."""
        md_files = _discover_markdown_files(dir_path, extensions)
        return sum(auto_fix_file(f) for f in md_files)

    @staticmethod
    def _to_lines(content: str | Sequence[str]) -> list[str]:
        return content.splitlines() if isinstance(content, str) else list(content)

    def validate_file(
        self,
        file_path: Path,
        known_anchors: dict[Path, set[str]] | None = None,
    ) -> list[DocFinding]:
        """Run all validation rules on a single markdown documentation file."""
        if not file_path.is_file():
            return [
                DocFinding(
                    file_path=str(file_path),
                    line_number=1,
                    category="file_not_found",
                    message=f"File does not exist: {file_path}",
                )
            ]

        content = file_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        findings: list[DocFinding] = []
        findings.extend(check_code_fences(lines, file_path, self.parser))
        findings.extend(check_mermaid_diagrams(lines, file_path))
        findings.extend(check_markdown_tables(lines, file_path))
        findings.extend(check_markdown_links(lines, file_path, known_anchors))
        findings.extend(check_embedded_snippets(lines, file_path))
        findings.extend(check_html_tags(lines, file_path))
        findings.extend(check_observation_structure(lines, file_path))

        return sorted(findings, key=lambda f: (f.line_number, f.category))

    def validate_content(
        self,
        content: str,
        file_path: Path = Path("document.md"),
        known_anchors: dict[Path, set[str]] | None = None,
    ) -> list[DocFinding]:
        """Validate markdown content directly from string."""
        lines = content.splitlines()
        findings: list[DocFinding] = []
        findings.extend(check_code_fences(lines, file_path, self.parser))
        findings.extend(check_mermaid_diagrams(lines, file_path))
        findings.extend(check_markdown_tables(lines, file_path))
        findings.extend(check_markdown_links(lines, file_path, known_anchors))
        findings.extend(check_embedded_snippets(lines, file_path))
        findings.extend(check_html_tags(lines, file_path))
        findings.extend(check_observation_structure(lines, file_path))
        return sorted(findings, key=lambda f: (f.line_number, f.category))

    def check_code_fences(
        self, content: str | Sequence[str], file_path: Path = Path("document.md")
    ) -> list[DocFinding]:
        """Validate code fences in content or lines."""
        return check_code_fences(self._to_lines(content), file_path, self.parser)

    def check_mermaid_blocks(
        self, content: str | Sequence[str], file_path: Path = Path("document.md")
    ) -> list[DocFinding]:
        """Validate Mermaid diagrams in content or lines."""
        return check_mermaid_diagrams(self._to_lines(content), file_path)

    def check_table_columns(
        self, content: str | Sequence[str], file_path: Path = Path("document.md")
    ) -> list[DocFinding]:
        """Validate markdown tables in content or lines."""
        return check_markdown_tables(self._to_lines(content), file_path)

    def check_links(
        self,
        content: str | Sequence[str],
        file_path: Path = Path("document.md"),
        known_anchors: dict[Path, set[str]] | None = None,
    ) -> list[DocFinding]:
        """Validate markdown links and anchors in content or lines."""
        return check_markdown_links(self._to_lines(content), file_path, known_anchors)

    def check_code_snippets(
        self, content: str | Sequence[str], file_path: Path = Path("document.md")
    ) -> list[DocFinding]:
        """Validate embedded Python, JSON, and YAML code snippets."""
        return check_embedded_snippets(self._to_lines(content), file_path)

    def check_html_tags(
        self, content: str | Sequence[str], file_path: Path = Path("document.md")
    ) -> list[DocFinding]:
        """Validate HTML tag pairing and balance in content or lines."""
        return check_html_tags(self._to_lines(content), file_path)

    def check_observation_structure(
        self, content: str | Sequence[str], file_path: Path = Path("observations/doc.md")
    ) -> list[DocFinding]:
        """Verify mandatory 5-section structure of observation documents."""
        return check_observation_structure(self._to_lines(content), file_path)

    def validate_directory(
        self,
        dir_path: Path,
        extensions: Sequence[str] = (".md", ".markdown"),
    ) -> DocValidationReport:
        """Scan and validate all markdown files in directory recursively."""
        start_time = time.monotonic()
        md_files = _discover_markdown_files(dir_path, extensions)

        known_anchors = {f: extract_heading_anchors(f.read_text(encoding="utf-8")) for f in md_files}
        all_findings: list[DocFinding] = []

        with ThreadPoolExecutor() as executor:
            findings_lists = list(
                executor.map(lambda f: self.validate_file(f, known_anchors=known_anchors), md_files)
            )
        for f_list in findings_lists:
            all_findings.extend(f_list)

        elapsed = time.monotonic() - start_time
        errors = sum(1 for f in all_findings if f.severity == "error")
        warnings = sum(1 for f in all_findings if f.severity == "warning")
        files_with = len({f.file_path for f in all_findings})

        return DocValidationReport(
            total_files=len(md_files),
            files_with_findings=files_with,
            error_count=errors,
            warning_count=warnings,
            is_valid=len(all_findings) == 0,
            findings=all_findings,
            duration_seconds=elapsed,
        )


def _fix_target(target: Path, validator: DocsValidator) -> int:
    """Apply automatic remediation to a single file or directory target."""
    return validator.fix_file(target) if target.is_file() else validator.fix_directory(target)


def _resolve_report(target: Path, validator: DocsValidator) -> DocValidationReport:
    """Generate validation report for single file or directory."""
    if not target.is_file():
        return validator.validate_directory(target)
    findings = validator.validate_file(target)
    return DocValidationReport(
        total_files=1,
        files_with_findings=1 if findings else 0,
        error_count=sum(1 for f in findings if f.severity == "error"),
        warning_count=sum(1 for f in findings if f.severity == "warning"),
        is_valid=len(findings) == 0,
        findings=findings,
        duration_seconds=0.0,
    )


def _merge_reports(reports: Sequence[DocValidationReport]) -> DocValidationReport:
    """Consolidate per-target reports into one aggregate report."""
    merged = DocValidationReport()
    for report in reports:
        merged.total_files += report.total_files
        merged.files_with_findings += report.files_with_findings
        merged.error_count += report.error_count
        merged.warning_count += report.warning_count
        merged.findings.extend(report.findings)
        merged.duration_seconds += report.duration_seconds
    merged.is_valid = len(merged.findings) == 0
    return merged


def _filter_report_by_rule(report: DocValidationReport, rule: str | None) -> None:
    """Filter validation findings by category rule in-place."""
    if not rule:
        return
    filtered = [f for f in report.findings if rule.lower() in f.category.lower()]
    report.findings = filtered
    report.error_count = sum(1 for f in filtered if f.severity == "error")
    report.warning_count = sum(1 for f in filtered if f.severity == "warning")
    report.is_valid = len(filtered) == 0


def _render_console_report(report: DocValidationReport) -> None:
    """Print findings to standard output."""
    if report.is_valid:
        print(f"✓ All {report.total_files} documentation files passed validation.")
        return
    print(f"✗ Found {len(report.findings)} issues in {report.files_with_findings} file(s):")
    for f in report.findings:
        print(f"  {f.file_path}:{f.line_number} [{f.category}] {f.message}")


def _determine_exit_code(report: DocValidationReport, strict: bool) -> int:
    """Determine CLI exit code based on report validity and strict flag."""
    if not report.is_valid:
        return 1
    if strict and report.warning_count > 0:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for standalone vibes doc validator."""
    parser = argparse.ArgumentParser(description="Vibes Documentation Syntax & Link Validator")
    parser.add_argument("paths", nargs="*", default=[], help="File or directory paths to validate")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    parser.add_argument("--fix", action="store_true", help="Automatically remediate fixable documentation issues")
    parser.add_argument("--json", action="store_true", help="Emit report as JSON")
    parser.add_argument("--rule", help="Filter findings by category rule")
    args = parser.parse_args(argv)

    targets = [Path(raw).resolve() for raw in (args.paths or ["."])]
    validator = DocsValidator()

    if args.fix:
        fixes = sum(_fix_target(target, validator) for target in targets)
        if fixes > 0:
            print(f"🔧 Applied {fixes} automatic remediation fix(es) to documentation.")

    report = _merge_reports([_resolve_report(target, validator) for target in targets])
    _filter_report_by_rule(report, args.rule)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 1 if not report.is_valid else 0

    _render_console_report(report)
    return _determine_exit_code(report, args.strict)


if __name__ == "__main__":
    sys.exit(main())
