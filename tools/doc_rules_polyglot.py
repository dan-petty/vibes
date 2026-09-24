#!/usr/bin/env python3
"""Multi-language and multi-format documentation integrity validation rules.

Extends documentation validation across formats and languages:
1. Multi-format documents: Markdown (.md), HTML (.html, .htm), reStructuredText (.rst),
   AsciiDoc (.adoc, .asciidoc), and Plain Text (.txt).
2. Polyglot link extraction: Extracts and verifies links, relative paths, and anchor targets
   across Markdown, HTML (<a href>, <link href>, <img src>), reStructuredText
   (`text <url>`_, .. _target: url), AsciiDoc (https://...[text], link:url[text], image::url[alt]),
   and plain text URLs.
3. Polyglot snippet validation: Syntactically audits embedded code blocks across languages:
   - TOML (via standard library tomllib)
   - XML, SVG, XHTML (via standard library xml.etree.ElementTree)
   - INI, Conf, Properties (via standard library configparser)
   - Shell, Bash, Sh, Zsh (via standard library shlex)
   - Polyglot source blocks (Rust, Go, TypeScript, JavaScript)
"""

from __future__ import annotations

import configparser
import re
import shlex
import textwrap
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from doc_core import DocFinding, MarkdownLink

# Supported polyglot documentation extensions
POLYGLOT_EXTENSIONS: Final[tuple[str, ...]] = (
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".rst",
    ".adoc",
    ".asciidoc",
    ".txt",
)


class DocumentFormat(StrEnum):
    """Supported documentation markup and plain text formats."""

    MARKDOWN = "markdown"
    HTML = "html"
    RST = "rst"
    ASCIIDOC = "asciidoc"
    PLAINTEXT = "plaintext"


EXTENSION_FORMAT_MAP: Final[dict[str, DocumentFormat]] = {
    ".md": DocumentFormat.MARKDOWN,
    ".markdown": DocumentFormat.MARKDOWN,
    ".html": DocumentFormat.HTML,
    ".htm": DocumentFormat.HTML,
    ".rst": DocumentFormat.RST,
    ".adoc": DocumentFormat.ASCIIDOC,
    ".asciidoc": DocumentFormat.ASCIIDOC,
    ".txt": DocumentFormat.PLAINTEXT,
}


def detect_document_format(file_path: Path) -> DocumentFormat:
    """Determine document format from file path extension."""
    return EXTENSION_FORMAT_MAP.get(file_path.suffix.lower(), DocumentFormat.MARKDOWN)


# Regex patterns for HTML link and anchor extraction
_HTML_HREF_RE: Final[re.Pattern[str]] = re.compile(
    r"""<(?:a|link|area)\b[^>]*?\bhref=(?P<q>["'])(?P<url>[^"']+?)(?P=q)""",
    re.IGNORECASE,
)
_HTML_SRC_RE: Final[re.Pattern[str]] = re.compile(
    r"""<(?:img|script|source)\b[^>]*?\bsrc=(?P<q>["'])(?P<url>[^"']+?)(?P=q)""",
    re.IGNORECASE,
)
_HTML_ANCHOR_DEF_RE: Final[re.Pattern[str]] = re.compile(
    r"""\b(?:id|name)=(?P<q>["'])(?P<name>[^"']+?)(?P=q)""",
    re.IGNORECASE,
)
_HTML_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"""<h[1-6]\b[^>]*>(?P<text>.*?)</h[1-6]>""",
    re.IGNORECASE | re.DOTALL,
)

# Regex patterns for reStructuredText link and anchor extraction
_RST_INLINE_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"`(?P<text>[^`]+?)\s*<(?P<url>[^>]+?)>`__?",
)
_RST_EXPLICIT_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*\.\.\s+_(?P<target>[^:]+):\s+(?P<url>\S+)",
)
_RST_DIRECTIVE_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*\.\.\s+(?:image|figure)::\s+(?P<url>\S+)",
)
_RST_TARGET_DEF_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*\.\.\s+_(?P<name>[^:]+):\s*$",
)

# Regex patterns for AsciiDoc link and anchor extraction
_ASCIIDOC_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"""\b(?P<url>https?://[^\s\[]+)\[(?P<text>[^\]]*?)\]""",
)
_ASCIIDOC_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"""\blink:(?P<url>[^\s\[]+)\[(?P<text>[^\]]*?)\]""",
)
_ASCIIDOC_IMAGE_RE: Final[re.Pattern[str]] = re.compile(
    r"""\bimage::?(?P<url>[^\s\[]+)\[(?P<text>[^\]]*?)\]""",
)
_ASCIIDOC_XREF_RE: Final[re.Pattern[str]] = re.compile(
    r"""\bxref:(?P<url>[^\s\[]+)\[(?P<text>[^\]]*?)\]""",
)
_ASCIIDOC_ANCHOR_DEF_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?:^|\s)(?:\[#(?P<id1>[A-Za-z0-9_-]+)\]|\[\[(?P<id2>[A-Za-z0-9_-]+)\]\])""",
)

# Plain text URL pattern
_PLAINTEXT_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"""\b(?P<url>https?://[^\s)\]>"',;]+)""",
)


def _slugify_anchor(text: str) -> str:
    """Normalize text into anchor identifier."""
    cleaned = re.sub(r"<[^>]+>", "", text).strip().lower()
    return re.sub(r"[^\w-]", "", re.sub(r"\s+", "-", cleaned))


def extract_html_anchors(content: str) -> set[str]:
    """Extract explicit ids, names, and heading slugs from HTML content."""
    anchors: set[str] = set()
    for match in _HTML_ANCHOR_DEF_RE.finditer(content):
        anchors.add(_slugify_anchor(match.group("name")))
    for match in _HTML_HEADING_RE.finditer(content):
        heading_text = match.group("text")
        if heading_text:
            anchors.add(_slugify_anchor(heading_text))
    return anchors


def _is_rst_title_underline(line: str) -> bool:
    """Predicate checking if line is a valid reStructuredText section underline."""
    return len(line) >= 3 and line == line[0] * len(line) and line[0] in "=-~`'^\"*+#"


def _extract_single_rst_anchor(lines: Sequence[str], idx: int) -> str | None:
    """Extract anchor name from target definition or section heading underline."""
    line = lines[idx]
    target_m = _RST_TARGET_DEF_RE.match(line)
    if target_m:
        return _slugify_anchor(target_m.group("name"))
    if idx > 0 and _is_rst_title_underline(line):
        return _slugify_anchor(lines[idx - 1])
    return None


def extract_rst_anchors(lines: Sequence[str]) -> set[str]:
    """Extract target anchors and section titles from reStructuredText."""
    return {
        anchor
        for idx in range(len(lines))
        if (anchor := _extract_single_rst_anchor(lines, idx)) is not None
    }


def _find_asciidoc_ids(line: str) -> list[str]:
    """Extract all explicit anchor IDs from an AsciiDoc line."""
    return [
        target_id
        for m in _ASCIIDOC_ANCHOR_DEF_RE.finditer(line)
        if (target_id := m.group("id1") or m.group("id2"))
    ]


def _extract_asciidoc_title(line: str) -> str | None:
    """Extract heading title from AsciiDoc section line."""
    if line.startswith("=") and not line.startswith("===="):
        return line.lstrip("=").strip() or None
    return None


def extract_asciidoc_anchors(lines: Sequence[str]) -> set[str]:
    """Extract anchor IDs and section heading slugs from AsciiDoc."""
    anchors: set[str] = set()
    for line in lines:
        stripped = line.strip()
        for target_id in _find_asciidoc_ids(stripped):
            anchors.add(_slugify_anchor(target_id))
        title = _extract_asciidoc_title(stripped)
        if title:
            anchors.add(_slugify_anchor(title))
    return anchors


def extract_polyglot_anchors(file_path: Path, content: str) -> set[str]:
    """Extract anchor slugs for any supported document format."""
    fmt = detect_document_format(file_path)
    lines = content.splitlines()
    if fmt == DocumentFormat.HTML:
        return extract_html_anchors(content)
    if fmt == DocumentFormat.RST:
        return extract_rst_anchors(lines)
    if fmt == DocumentFormat.ASCIIDOC:
        return extract_asciidoc_anchors(lines)
    return set()


def _extract_line_links(
    line: str, line_no: int, patterns: tuple[re.Pattern[str], ...]
) -> list[MarkdownLink]:
    """Extract MarkdownLinks from one line matching any of the given patterns."""
    return [
        MarkdownLink(href=m.group("url").strip(), title="", line=line_no)
        for pattern in patterns
        for m in pattern.finditer(line)
        if m.group("url").strip()
    ]


def extract_html_links(lines: Sequence[str]) -> list[MarkdownLink]:
    """Extract links and image sources from HTML lines."""
    patterns = (_HTML_HREF_RE, _HTML_SRC_RE)
    return [link for idx, line in enumerate(lines, 1) for link in _extract_line_links(line, idx, patterns)]


def extract_rst_links(lines: Sequence[str]) -> list[MarkdownLink]:
    """Extract hyperlink targets from reStructuredText lines."""
    patterns = (_RST_INLINE_LINK_RE, _RST_EXPLICIT_LINK_RE, _RST_DIRECTIVE_LINK_RE)
    return [link for idx, line in enumerate(lines, 1) for link in _extract_line_links(line, idx, patterns)]


def extract_asciidoc_links(lines: Sequence[str]) -> list[MarkdownLink]:
    """Extract inline links and media references from AsciiDoc lines."""
    patterns = (_ASCIIDOC_URL_RE, _ASCIIDOC_LINK_RE, _ASCIIDOC_IMAGE_RE, _ASCIIDOC_XREF_RE)
    return [link for idx, line in enumerate(lines, 1) for link in _extract_line_links(line, idx, patterns)]


def extract_plaintext_links(lines: Sequence[str]) -> list[MarkdownLink]:
    """Extract web URLs from plain text lines."""
    patterns = (_PLAINTEXT_URL_RE,)
    return [link for idx, line in enumerate(lines, 1) for link in _extract_line_links(line, idx, patterns)]


def extract_polyglot_links(lines: Sequence[str], file_path: Path) -> list[MarkdownLink]:
    """Extract all document links according to file format."""
    fmt = detect_document_format(file_path)
    dispatch: dict[DocumentFormat, Callable[[Sequence[str]], list[MarkdownLink]]] = {
        DocumentFormat.HTML: extract_html_links,
        DocumentFormat.RST: extract_rst_links,
        DocumentFormat.ASCIIDOC: extract_asciidoc_links,
        DocumentFormat.PLAINTEXT: extract_plaintext_links,
    }
    extractor = dispatch.get(fmt)
    return extractor(lines) if extractor else []


# --- Polyglot Code Snippet Syntax Validators ---


def _has_doc_placeholders(code: str) -> bool:
    """Predicate reporting whether code contains illustrative placeholders or templates."""
    return bool(
        re.search(r"^\s*(\.\.\.|…|>>>|\$|<[a-zA-Z0-9_-]+>|\{\{|\}\})", code, re.M)
        or any(line.strip().startswith(("+", "-", "@@")) for line in code.splitlines())
    )


def _has_xml_doc_placeholders(code: str) -> bool:
    """Predicate reporting whether XML code contains illustrative placeholders or templates."""
    return bool(
        re.search(r"^\s*(\.\.\.|…|\{\{|\}\})", code, re.M)
        or any(line.strip().startswith(("+", "-", "@@")) for line in code.splitlines())
    )


def validate_toml_snippet(code: str, start_line: int, file_str: str) -> DocFinding | None:
    """Validate syntax of an embedded TOML code block."""
    dedented = textwrap.dedent(code)
    if _has_doc_placeholders(dedented):
        return None
    try:
        tomllib.loads(dedented)
        return None
    except tomllib.TOMLDecodeError as exc:
        return DocFinding(
            file_path=file_str,
            line_number=start_line,
            category="code_snippet",
            message=f"TOML syntax error: {exc}",
        )


def validate_xml_snippet(code: str, start_line: int, file_str: str) -> DocFinding | None:
    """Validate syntax of an embedded XML or SVG code block."""
    dedented = textwrap.dedent(code)
    if _has_xml_doc_placeholders(dedented) or not dedented.strip().startswith("<"):
        return None
    try:
        ET.fromstring(dedented)
        return None
    except ET.ParseError as exc:
        return DocFinding(
            file_path=file_str,
            line_number=start_line,
            category="code_snippet",
            message=f"XML syntax error: {exc}",
        )


def validate_ini_snippet(code: str, start_line: int, file_str: str) -> DocFinding | None:
    """Validate syntax of an embedded INI, Conf, or Properties code block."""
    dedented = textwrap.dedent(code)
    if _has_doc_placeholders(dedented):
        return None
    to_parse = dedented if "[" in dedented else f"[default]\n{dedented}"
    parser = configparser.ConfigParser()
    try:
        parser.read_string(to_parse)
        return None
    except configparser.Error as exc:
        return DocFinding(
            file_path=file_str,
            line_number=start_line,
            category="code_snippet",
            message=f"INI syntax error: {exc}",
        )


def _validate_shell_line(pline: str, line_no: int, file_str: str) -> DocFinding | None:
    """Validate single shell line for unmatched quotes and tokenizer errors."""
    try:
        shlex.split(pline, comments=True)
        return None
    except ValueError as exc:
        return DocFinding(
            file_path=file_str,
            line_number=line_no,
            category="code_snippet",
            message=f"Shell syntax error: {exc}",
        )


def _collect_shell_continuation(
    line: str, idx: int, current_line: str, current_start: int
) -> tuple[str, int, tuple[int, str] | None]:
    """Process line continuation, returning (new_current_line, new_current_start, completed_command)."""
    start = current_start if current_line else idx
    if line.endswith("\\"):
        return current_line + line[:-1].rstrip() + " ", start, None
    full_cmd = (current_line + line).strip()
    completed = (start, full_cmd) if full_cmd else None
    return "", 0, completed


def _join_shell_continuations(raw_lines: Sequence[str]) -> list[tuple[int, str]]:
    """Join shell lines that end with a line continuation backslash."""
    joined: list[tuple[int, str]] = []
    current_line = ""
    current_start = 0

    for idx, raw_line in enumerate(raw_lines, 0):
        current_line, current_start, completed = _collect_shell_continuation(
            raw_line.strip(), idx, current_line, current_start
        )
        if completed:
            joined.append(completed)
    if current_line.strip():
        joined.append((current_start, current_line.strip()))
    return joined


def validate_shell_snippet(code: str, start_line: int, file_str: str) -> DocFinding | None:
    """Validate syntax of an embedded Bash or POSIX shell code block."""
    dedented = textwrap.dedent(code)
    commands = _join_shell_continuations(dedented.splitlines())
    for offset, raw_cmd in commands:
        cmd = raw_cmd.strip()
        if cmd.startswith("$ "):
            cmd = cmd[2:].strip()
        if not cmd or cmd.startswith(("#", "...")):
            continue
        cleaned = re.sub(r"<[a-zA-Z0-9_-]+>", "placeholder", cmd)
        finding = _validate_shell_line(cleaned, start_line + offset, file_str)
        if finding:
            return finding
    return None


_OPEN_BRACKETS: Final[frozenset[str]] = frozenset({"{", "[", "("})
_CLOSE_BRACKETS: Final[dict[str, str]] = {"}": "{", "]": "[", ")": "("}
_STRING_OR_COMMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"""//.*?$|/\*.*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`""",
    re.MULTILINE | re.DOTALL,
)


def _check_bracket_char(char: str, stack: list[str]) -> str | None:
    """Check a single character against the bracket stack, returning error if mismatched."""
    if char in _OPEN_BRACKETS:
        stack.append(char)
        return None
    if char not in _CLOSE_BRACKETS:
        return None
    if not stack or stack[-1] != _CLOSE_BRACKETS[char]:
        return f"Unmatched '{char}'"
    stack.pop()
    return None


def _check_bracket_pairs(clean_code: str) -> str | None:
    """Validate paired bracket nesting in string-stripped code."""
    stack: list[str] = []
    for char in clean_code:
        err = _check_bracket_char(char, stack)
        if err:
            return err
    return "Unclosed delimiter" if stack else None


def _check_delimiter_balance(code: str) -> str | None:
    """Check balanced pairs for braces, brackets, and parentheses."""
    cleaned = _STRING_OR_COMMENT_RE.sub(" ", code)
    if any(q in cleaned for q in ('"', "'", "`")):
        return "Unclosed string literal"
    return _check_bracket_pairs(cleaned)


def validate_polyglot_source_snippet(
    lang: str, code: str, start_line: int, file_str: str
) -> DocFinding | None:
    """Validate delimiter balance and basic syntax for Rust, Go, and TypeScript blocks."""
    dedented = textwrap.dedent(code)
    if _has_doc_placeholders(dedented):
        return None
    err = _check_delimiter_balance(dedented)
    if err:
        return DocFinding(
            file_path=file_str,
            line_number=start_line,
            category="code_snippet",
            message=f"{lang.capitalize()} syntax error: {err}",
        )
    return None


@dataclass(frozen=True)
class PolyglotSnippet:
    """Extracted code snippet from any document format."""

    language: str
    code: str
    start_line: int


def _is_rst_code_directive(line: str) -> str | None:
    """Check if line opens an RST code directive, returning language tag."""
    m = re.match(r"^\s*\.\.\s+(?:code-block|code)::\s*([a-zA-Z0-9_-]+)?", line)
    return m.group(1).lower() if (m and m.group(1)) else None


def _collect_rst_block(lines: Sequence[str], start_idx: int) -> tuple[list[str], int]:
    """Collect indented lines for an RST directive block, returning (lines, next_idx)."""
    idx = start_idx
    total = len(lines)
    block_lines: list[str] = []
    while idx < total and (not lines[idx].strip() or lines[idx].startswith("   ") or lines[idx].startswith("\t")):
        block_lines.append(lines[idx])
        idx += 1
    return block_lines, idx


def extract_rst_snippets(lines: Sequence[str]) -> list[PolyglotSnippet]:
    """Extract code snippets declared via RST code-block directives."""
    snippets: list[PolyglotSnippet] = []
    idx = 0
    total = len(lines)

    while idx < total:
        lang = _is_rst_code_directive(lines[idx])
        if not lang:
            idx += 1
            continue
        start_line = idx + 1
        block_lines, idx = _collect_rst_block(lines, idx + 1)
        if block_lines:
            snippets.append(PolyglotSnippet(lang, "\n".join(block_lines), start_line))
    return snippets


def _collect_asciidoc_block(lines: Sequence[str], start_idx: int) -> tuple[list[str], int]:
    """Collect lines inside an AsciiDoc delimiter block, returning (lines, next_idx)."""
    idx = start_idx
    total = len(lines)
    block_lines: list[str] = []
    while idx < total and not lines[idx].strip().startswith("----"):
        block_lines.append(lines[idx])
        idx += 1
    return block_lines, min(idx + 1, total)


def extract_asciidoc_snippets(lines: Sequence[str]) -> list[PolyglotSnippet]:
    """Extract code snippets declared via AsciiDoc source blocks."""
    snippets: list[PolyglotSnippet] = []
    idx = 0
    total = len(lines)

    while idx < total:
        line = lines[idx].strip()
        m = re.match(r"^\[source\s*,\s*([a-zA-Z0-9_-]+)\]", line)
        if not m or idx + 1 >= total or not lines[idx + 1].strip().startswith("----"):
            idx += 1
            continue
        lang = m.group(1).lower()
        start_line = idx + 1
        block_lines, idx = _collect_asciidoc_block(lines, idx + 2)
        snippets.append(PolyglotSnippet(lang, "\n".join(block_lines), start_line))
    return snippets


def extract_html_snippets(lines: Sequence[str]) -> list[PolyglotSnippet]:
    """Extract code snippets declared in HTML <pre><code class="language-..."> blocks."""
    content = "\n".join(lines)
    snippets: list[PolyglotSnippet] = []
    pattern = re.compile(
        r"""<pre><code\b[^>]*?class=["'](?:language-)?(?P<lang>[a-zA-Z0-9_-]+)["'][^>]*?>(?P<code>.*?)</code></pre>""",
        re.DOTALL | re.IGNORECASE,
    )
    for match in pattern.finditer(content):
        lang = match.group("lang").lower()
        code = match.group("code")
        start_offset = content[: match.start()].count("\n") + 1
        snippets.append(PolyglotSnippet(lang, code, start_offset))
    return snippets
