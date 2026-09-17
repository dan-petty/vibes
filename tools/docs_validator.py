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
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import sys
import textwrap
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

_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^(`{3,}|~{3,})(.*)$")
_SPACE_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[([^\]]+)\]\s+\(([^)]+)\)")
_MARKDOWN_LINK_RE: Final[re.Pattern[str]] = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")
_MERMAID_NODE_RE: Final[re.Pattern[str]] = re.compile(r"([A-Za-z0-9_]+)\[([^\]]+)\]")
_MERMAID_EDGE_RE: Final[re.Pattern[str]] = re.compile(r"(-->|-\.->|==>)\|([^|]+)\|")
_HTML_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<(/)?([a-zA-Z0-9]+)(?:\s+[^>]*)?>")


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


def extract_heading_anchors(content: str) -> set[str]:
    """Extract all heading anchor slugs and explicit HTML anchors from markdown."""
    anchors: set[str] = set()
    in_fence = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            slug = slugify_heading(m.group(2).strip())
            if slug:
                anchors.add(slug)
        for anchor_match in re.finditer(r'<a\s+[^>]*(?:name|id)=["\']([^"\']+)["\']', line, re.I):
            anchors.add(anchor_match.group(1))
    return anchors


def _check_token_fences(tokens: Sequence[Any], file_str: str) -> list[DocFinding]:
    """Scan parsed tokens for malformed or empty code fences."""
    findings: list[DocFinding] = []
    for token in tokens:
        if token.type == "fence" and token.map:
            start_l, end_l = token.map
            if not token.info and (end_l - start_l <= 1):
                findings.append(
                    DocFinding(
                        file_path=file_str,
                        line_number=start_l + 1,
                        category="code_fence",
                        message=f"Suspicious unclosed or empty code fence at lines {start_l+1}-{end_l}",
                    )
                )
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
    elif char_type == fence_char and info and curr_len >= fence_len:
        findings.append(
            DocFinding(
                file_path=file_str,
                line_number=idx,
                category="code_fence",
                message=f"Nested code fence at line {idx} inside a {fence_len}-backtick block requires 4+ backticks",
            )
        )
    elif char_type == fence_char and curr_len >= fence_len:
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


def _check_mermaid_node_label(line: str, line_no: int, file_str: str) -> list[DocFinding]:
    """Validate that node shape labels with parentheses are properly quoted."""
    findings: list[DocFinding] = []
    for match in _MERMAID_NODE_RE.finditer(line):
        node_id, label = match.group(1), match.group(2)
        if not (label.startswith('"') and label.endswith('"')) and ("(" in label or ")" in label):
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


def _check_mermaid_edge_label(line: str, line_no: int, file_str: str) -> list[DocFinding]:
    """Validate that edge labels with parentheses or special characters are properly quoted."""
    findings: list[DocFinding] = []
    for match in _MERMAID_EDGE_RE.finditer(line):
        label = match.group(2).strip()
        if not (label.startswith('"') and label.endswith('"')) and any(c in label for c in ("(", ")", ">", "<")):
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


def check_mermaid_diagrams(lines: Sequence[str], file_path: Path) -> list[DocFinding]:
    """Inspect all mermaid diagrams for syntax structure and unquoted characters."""
    file_str = str(file_path)
    findings: list[DocFinding] = []
    in_mermaid, m_start, m_lines = False, 0, []

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```mermaid"):
            in_mermaid, m_start, m_lines = True, idx, []
            continue
        if in_mermaid and stripped.startswith("```"):
            in_mermaid = False
            first = next((line_text.strip() for line_text in m_lines if line_text.strip() and not line_text.strip().startswith("%%")), "")
            if first and not any(first.lower().startswith(vt) for vt in VALID_MERMAID_TYPES):
                findings.append(
                    DocFinding(
                        file_path=file_str,
                        line_number=m_start,
                        category="mermaid",
                        message=f"Unrecognized Mermaid diagram type: '{first}'",
                    )
                )
            continue
        if in_mermaid:
            m_lines.append(line)
            findings.extend(_check_mermaid_node_label(line, idx, file_str))
            findings.extend(_check_mermaid_edge_label(line, idx, file_str))
    return findings


def _split_table_row(line: str) -> list[str]:
    """Split a markdown table row by pipes while ignoring pipes inside backticks."""
    masked: list[str] = []
    in_code = False
    for ch in line:
        if ch == "`":
            in_code = not in_code
            masked.append(ch)
        elif ch == "|" and in_code:
            masked.append("\x00")
        else:
            masked.append(ch)
    parts = re.split(r"(?<!\\)\|", "".join(masked))
    if len(parts) >= 2 and parts[0].strip() == "" and parts[-1].strip() == "":
        parts = parts[1:-1]
    return [p.replace("\x00", "|").strip() for p in parts]


def check_markdown_tables(lines: Sequence[str], file_path: Path) -> list[DocFinding]:
    """Verify table row column counts match the table header definition."""
    file_str = str(file_path)
    findings: list[DocFinding] = []
    in_fence, in_table, header_cols = False, False, 0

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence, in_table = not in_fence, False
            continue
        if in_fence or "|" not in stripped:
            in_table = False
            continue

        raw_cells = _split_table_row(stripped)
        is_delimiter = len(raw_cells) > 0 and all(re.match(r"^:?-+:?$", c) for c in raw_cells)
        if is_delimiter:
            in_table, header_cols = True, len(raw_cells)
        elif in_table and len(raw_cells) != header_cols:
            findings.append(
                DocFinding(
                    file_path=file_str,
                    line_number=idx,
                    category="table",
                    message=f"Table row column count mismatch: expected {header_cols}, found {len(raw_cells)}",
                )
            )
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
    resolved = (file_path.parent / path_part).resolve()
    if not resolved.exists():
        # Fallback relative to repository root if testing from subdirectories
        repo_root_fallback = (file_path.cwd() / path_part.lstrip("./")).resolve()
        if repo_root_fallback.exists():
            return None
        return DocFinding(
            file_path=str(file_path),
            line_number=line_no,
            category="link",
            message=f"Target path does not exist: '{path_part}'",
        )
    return None


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


def check_markdown_links(
    lines: Sequence[str],
    file_path: Path,
    known_anchors: dict[Path, set[str]] | None = None,
) -> list[DocFinding]:
    """Inspect all markdown links for broken paths, missing anchors, and host-specific URIs."""
    file_str = str(file_path)
    findings: list[DocFinding] = []
    in_fence = False

    effective_anchors = dict(known_anchors) if known_anchors is not None else {}
    if file_path not in effective_anchors:
        effective_anchors[file_path] = extract_heading_anchors("\n".join(lines))

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        if _SPACE_LINK_RE.search(line):
            findings.append(
                DocFinding(
                    file_path=file_str,
                    line_number=idx,
                    category="link",
                    message=f"Malformed link with whitespace between brackets: '{line.strip()}'",
                )
            )

        for match in _MARKDOWN_LINK_RE.finditer(line):
            finding = _validate_link_target(match.group(2).strip(), file_path, idx, effective_anchors)
            if finding:
                findings.append(finding)
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


def _validate_json_snippet(code: str, start_line: int, file_str: str) -> DocFinding | None:
    """Validate syntax of an embedded JSON code block."""
    dedented = textwrap.dedent(code).strip()
    if re.search(r"//|/\*|#|\.\.\.|<[a-zA-Z0-9_-]+>|\$\{", dedented):
        return None
    if not (
        (dedented.startswith("{") and dedented.endswith("}"))
        or (dedented.startswith("[") and dedented.endswith("]"))
    ):
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


def check_embedded_snippets(lines: Sequence[str], file_path: Path) -> list[DocFinding]:
    """Syntactically parse embedded code blocks in supported languages."""
    file_str = str(file_path)
    findings: list[DocFinding] = []
    in_fence, fence_lang, start_line, block_lines = False, "", 0, []

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        m_fence = _FENCE_RE.match(stripped)
        if m_fence and not in_fence:
            in_fence, start_line, block_lines = True, idx, []
            info = m_fence.group(2).strip().split()
            fence_lang = info[0].lower() if info else ""
            continue
        if m_fence and in_fence:
            in_fence = False
            finding = _validate_single_snippet(fence_lang, block_lines, start_line, file_str)
            if finding:
                findings.append(finding)
            continue
        if in_fence:
            block_lines.append(line)
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


def check_html_tags(lines: Sequence[str], file_path: Path) -> list[DocFinding]:
    """Check pairing and nesting of structural HTML tags outside code blocks."""
    file_str = str(file_path)
    findings: list[DocFinding] = []
    in_fence, stack = False, []

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        clean = re.sub(r"`[^`]*`", "", re.sub(r"<!--.*?-->", "", line))
        for match in _HTML_TAG_RE.finditer(clean):
            is_closing, tag = bool(match.group(1)), match.group(2).lower()
            if tag not in VOID_HTML_TAGS and tag in PAIRED_HTML_TAGS:
                finding = _process_html_tag(is_closing, tag, idx, file_str, stack)
                if finding:
                    findings.append(finding)

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


class DocsValidator:
    """Markdown documentation syntax, link integrity, and snippet validator for Vibes."""

    def __init__(self) -> None:
        self.parser = MarkdownIt("commonmark")

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

    def validate_directory(
        self,
        dir_path: Path,
        extensions: Sequence[str] = (".md", ".markdown"),
    ) -> DocValidationReport:
        """Scan and validate all markdown files in directory recursively."""
        start_time = time.monotonic()
        md_files = [
            f for f in sorted(dir_path.rglob("*"))
            if f.is_file() and f.suffix.lower() in extensions and not any(p.startswith(".") for p in f.parts)
        ]

        known_anchors = {f: extract_heading_anchors(f.read_text(encoding="utf-8")) for f in md_files}
        all_findings: list[DocFinding] = []

        for f in md_files:
            all_findings.extend(self.validate_file(f, known_anchors=known_anchors))

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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for standalone vibes doc validator."""
    parser = argparse.ArgumentParser(description="Vibes Documentation Syntax & Link Validator")
    parser.add_argument("path", nargs="?", default=".", help="File or directory path to validate")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    parser.add_argument("--json", action="store_true", help="Emit report as JSON")
    parser.add_argument("--rule", help="Filter findings by category rule")
    args = parser.parse_args(argv)

    report = _resolve_report(Path(args.path).resolve(), DocsValidator())
    _filter_report_by_rule(report, args.rule)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 1 if not report.is_valid else 0

    _render_console_report(report)
    if not report.is_valid or (args.strict and report.warning_count > 0):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
