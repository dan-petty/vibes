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
import json
import os
import re
import sys
import textwrap
import threading
import time
import tomllib
import urllib.parse
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import unquote

import yaml
from doc_core import (
    DocFinding,
    DocValidationReport,
    MarkdownLink,
    PathOracle,
    fenced_line_flags,
    parse_document,
)
from doc_rules_mermaid import (
    _MERMAID_EDGE_RE,
    _MERMAID_NODE_RE,
    _is_unquoted_parens_node_label,
    _is_unquoted_special_edge_label,
    check_mermaid_diagrams,
    contrast_ratio,
)
from doc_rules_polyglot import (
    POLYGLOT_EXTENSIONS,
    DocumentFormat,
    PolyglotSnippet,
    detect_document_format,
    extract_asciidoc_snippets,
    extract_html_snippets,
    extract_polyglot_anchors,
    extract_polyglot_links,
    extract_rst_snippets,
    validate_ini_snippet,
    validate_polyglot_source_snippet,
    validate_shell_snippet,
    validate_toml_snippet,
    validate_xml_snippet,
)
from doc_rules_structure import (
    OBSERVATION_REQUIRED_SECTION_COUNT,
    check_directory_maps,
    check_documentation_sanitization,
    check_latex_math_hygiene,
    check_linebreak_hygiene,
    check_observation_structure,
    check_pattern_header,
)
from source_tree_policy import iter_source_files

if TYPE_CHECKING:
    from markdown_it import MarkdownIt

# This module is the documentation validator's public face: the rule modules behind it
# are an implementation detail, so callers import the validator, its entry point and the
# two rule constants they assert against from here. Naming that surface explicitly is
# what makes the re-exports deliberate rather than incidental — an automated unused-import
# pass had already deleted them once, because nothing in this file used them.
__all__ = [
    "ALL_DOC_RULES",
    "DOC_PRESETS",
    "OBSERVATION_REQUIRED_SECTION_COUNT",
    "POLYGLOT_EXTENSIONS",
    "RULE_ALIASES",
    "RULE_CODE_MAP",
    "VALIDATOR_RULES",
    "DocConfigOverrides",
    "DocFinding",
    "DocValidationReport",
    "DocsValidator",
    "DocsValidatorConfig",
    "DocumentFormat",
    "RulePreset",
    "auto_fix_content",
    "check_latex_math_hygiene",
    "check_linebreak_hygiene",
    "check_polyglot_documentation",
    "contrast_ratio",
    "detect_document_format",
    "extract_document_anchors",
    "load_doc_config",
    "main",
    "normalize_doc_rule_name",
    "parse_cli_args",
    "render_presets_table",
    "resolve_doc_config",
]

# Supported documentation extensions

# Valid Mermaid diagram declarations

PAIRED_HTML_TAGS: Final[frozenset[str]] = frozenset(
    {"details", "summary", "div", "span", "table", "thead", "tbody", "tr", "th", "td"}
)
VOID_HTML_TAGS: Final[frozenset[str]] = frozenset({"br", "hr", "img", "input"})


@dataclass(frozen=True)
class RulePreset:
    """Preset configuration defining active documentation validation rules."""

    name: str
    description: str
    active_rules: frozenset[str]
    strict: bool = False


VALIDATOR_RULES: Final[dict[str, str]] = {
    "code_fence": "Code fence syntax, matching markers, and unclosed blocks",
    "mermaid": "Mermaid diagram declaration and unquoted node/edge labels",
    "table": "Markdown table column alignment and delimiter syntax",
    "link": "Broken relative links, unresolvable anchors, and scheme safety",
    "code_snippet": "Syntax correctness of embedded Python, JSON, and YAML blocks",
    "html_tag": "Structural paired HTML tag balance (<details>, <summary>, etc.)",
    "structure": "Required numbered 5-section observation structure",
    "directory_map": "Directory map tree synchronization against filesystem",
    "pattern_header": "Architecture pattern header metadata and references",
    "sanitization": "Zero-trust egress and RFC 1918 private address sanitization",
    "multi_language": "Multi-format document parsing and polyglot snippet validation (RST, AsciiDoc, HTML, text)",
    "linebreak": "CommonMark hard linebreaks and trailing whitespace hygiene",
    "math": "LaTeX and KaTeX math syntax, delimiters, and unescaped character hygiene",
}

ALL_DOC_RULES: Final[frozenset[str]] = frozenset(VALIDATOR_RULES.keys())

RULE_CODE_MAP: Final[dict[str, str]] = {
    "DOC001": "code_fence",
    "DOC002": "mermaid",
    "DOC003": "table",
    "DOC004": "link",
    "DOC005": "code_snippet",
    "DOC006": "html_tag",
    "DOC007": "structure",
    "DOC008": "directory_map",
    "DOC009": "pattern_header",
    "DOC010": "sanitization",
    "DOC011": "multi_language",
    "DOC012": "linebreak",
    "DOC013": "math",
}

RULE_ALIASES: Final[dict[str, str]] = {
    "fence": "code_fence",
    "fences": "code_fence",
    "code_fence": "code_fence",
    "mermaid": "mermaid",
    "diagram": "mermaid",
    "diagrams": "mermaid",
    "table": "table",
    "tables": "table",
    "link": "link",
    "links": "link",
    "anchor": "link",
    "anchors": "link",
    "snippet": "code_snippet",
    "snippets": "code_snippet",
    "code": "code_snippet",
    "code_snippet": "code_snippet",
    "html": "html_tag",
    "html_tag": "html_tag",
    "tags": "html_tag",
    "structure": "structure",
    "sections": "structure",
    "observation": "structure",
    "directory_map": "directory_map",
    "dirmap": "directory_map",
    "maps": "directory_map",
    "pattern": "pattern_header",
    "patterns": "pattern_header",
    "pattern_header": "pattern_header",
    "sanitization": "sanitization",
    "security": "sanitization",
    "egress": "sanitization",
    "zero_trust": "sanitization",
    "multi_language": "multi_language",
    "multilang": "multi_language",
    "polyglot": "multi_language",
    "formats": "multi_language",
    "format": "multi_language",
    "linebreak": "linebreak",
    "linebreaks": "linebreak",
    "whitespace": "linebreak",
    "trailing_whitespace": "linebreak",
    "math": "math",
    "latex": "math",
    "katex": "math",
    "formula": "math",
}

DOC_PRESETS: Final[dict[str, RulePreset]] = {
    "standard": RulePreset(
        name="standard",
        description="Standard full-suite documentation validation across all rules",
        active_rules=ALL_DOC_RULES,
        strict=False,
    ),
    "strict": RulePreset(
        name="strict",
        description="Full-suite validation treating warnings and suggestions as errors",
        active_rules=ALL_DOC_RULES,
        strict=True,
    ),
    "polyglot": RulePreset(
        name="polyglot",
        description="Comprehensive polyglot and multi-format validation across all rules",
        active_rules=ALL_DOC_RULES,
        strict=False,
    ),
    "structure_only": RulePreset(
        name="structure_only",
        description="Structural layout, observation sections, tables, fences, and directory maps",
        active_rules=frozenset({
            "code_fence",
            "table",
            "structure",
            "pattern_header",
            "directory_map",
            "html_tag",
            "linebreak",
            "math",
        }),
        strict=False,
    ),
    "links_only": RulePreset(
        name="links_only",
        description="Fast link, URI, and anchor integrity validation only",
        active_rules=frozenset({"link"}),
        strict=False,
    ),
    "code_only": RulePreset(
        name="code_only",
        description="Embedded code snippets (Python, JSON, YAML), Mermaid diagrams, and code fences",
        active_rules=frozenset({"code_fence", "code_snippet", "mermaid"}),
        strict=False,
    ),
    "sanitization_only": RulePreset(
        name="sanitization_only",
        description="Zero-trust documentation sanitization and private IP egress check only",
        active_rules=frozenset({"sanitization"}),
        strict=False,
    ),
}


def normalize_doc_rule_name(rule_input: str) -> str:
    """Normalize a rule code or ergonomic alias to canonical rule name."""
    stripped = rule_input.strip()
    upper = stripped.upper()
    if upper in RULE_CODE_MAP:
        return RULE_CODE_MAP[upper]
    lower = stripped.lower()
    return RULE_ALIASES.get(lower, lower)


@dataclass(frozen=True)
class DocsValidatorConfig:
    """Configuration for documentation validation rules and execution modes."""

    preset_name: str = "standard"
    active_rules: frozenset[str] = ALL_DOC_RULES
    strict: bool = False
    selected_rules: frozenset[str] = field(default_factory=frozenset)
    ignored_rules: frozenset[str] = field(default_factory=frozenset)
    extended_rules: frozenset[str] = field(default_factory=frozenset)
    extensions: tuple[str, ...] = (".md", ".markdown")

    def is_rule_active(self, rule_name: str) -> bool:
        """Report whether a documentation rule is enabled under this configuration."""
        canonical = normalize_doc_rule_name(rule_name)
        return canonical in self.active_rules


@dataclass(frozen=True)
class DocConfigOverrides:
    """Explicit CLI or caller overrides for documentation configuration resolution."""

    preset_name: str | None = None
    strict: bool | None = None
    select: Sequence[str] | None = None
    ignore: Sequence[str] | None = None
    extend_select: Sequence[str] | None = None
    config_file: Path | None = None
    extensions: tuple[str, ...] | None = None


def _find_default_doc_config() -> Path | None:
    """Locate pyproject.toml, docs_validator.toml, or .markdownlint.json in cwd or parents."""
    candidates = (
        "pyproject.toml",
        "docs_validator.toml",
        ".docs_validator.toml",
        ".markdownlint.json",
    )
    cwd = Path.cwd()
    search_dirs = (cwd, *cwd.parents)
    all_targets = (d / name for d in search_dirs for name in candidates)
    return next((p for p in all_targets if p.is_file()), None)


def _parse_toml_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Extract tool.docs_validator or tool.doclint settings from parsed TOML table."""
    tool_section = data.get("tool", {})
    if not isinstance(tool_section, dict):
        return {}
    config = tool_section.get("docs_validator") or tool_section.get("doclint")
    return dict(config) if isinstance(config, dict) else {}


def _parse_markdownlint_json(path: Path) -> dict[str, Any]:
    """Parse .markdownlint.json into documentation validator settings."""
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(content, dict):
        return {}
    ignored = [normalize_doc_rule_name(k) for k, v in content.items() if v is False]
    return {"ignore": ignored} if ignored else {}


def _load_toml_file(path: Path) -> dict[str, Any]:
    """Load and parse a TOML file."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return {}
    if path.name == "pyproject.toml":
        return _parse_toml_settings(data)
    section = data.get("docs_validator")
    return dict(section) if isinstance(section, dict) else dict(data)


def load_doc_config(path: Path) -> dict[str, Any]:
    """Load documentation validator configuration from a TOML or JSON file."""
    if not path.is_file():
        return {}
    if path.suffix == ".json":
        return _parse_markdownlint_json(path)
    return _load_toml_file(path)


def _normalize_rule_sequence(raw: Sequence[str] | None) -> frozenset[str]:
    """Normalize a sequence of rule codes or aliases to canonical set."""
    if not raw:
        return frozenset()
    tokens = (token.strip() for item in raw for token in item.split(","))
    return frozenset(normalize_doc_rule_name(t) for t in tokens if t)


def _compute_active_rules(
    preset_rules: frozenset[str],
    selected: frozenset[str],
    extended: frozenset[str],
    ignored: frozenset[str],
) -> frozenset[str]:
    """Calculate active rules set from base preset, selections, and exclusions."""
    base = selected if selected else preset_rules
    return frozenset((base | extended) - ignored)


def _pick_rule_list(
    override: Sequence[str] | None,
    file_cfg: dict[str, Any],
    key: str,
) -> frozenset[str]:
    """Extract and normalize rule list from CLI override or file configuration."""
    raw = override if override is not None else file_cfg.get(key, [])
    return _normalize_rule_sequence(raw)


def _resolve_strict_flag(
    opts: DocConfigOverrides,
    file_cfg: dict[str, Any],
    default: bool,
) -> bool:
    """Resolve strict mode setting considering overrides and configuration."""
    if opts.strict is not None:
        return opts.strict
    return bool(file_cfg.get("strict", default))


def _resolve_extensions(
    opts: DocConfigOverrides,
    file_cfg: dict[str, Any],
) -> tuple[str, ...]:
    """Resolve file extensions to validate from overrides, file configuration, or default."""
    if opts.extensions is not None:
        return opts.extensions
    cfg_exts = file_cfg.get("extensions")
    if isinstance(cfg_exts, (list, tuple)):
        return tuple(str(e) for e in cfg_exts)
    return (".md", ".markdown")


def resolve_doc_config(overrides: DocConfigOverrides | None = None) -> DocsValidatorConfig:
    """Resolve documentation configuration by merging preset defaults, TOML config, and overrides."""
    opts = overrides or DocConfigOverrides()
    config_path = opts.config_file or _find_default_doc_config()
    file_cfg = load_doc_config(config_path) if config_path else {}

    preset_key = opts.preset_name or file_cfg.get("preset", "standard")
    preset = DOC_PRESETS.get(preset_key, DOC_PRESETS["standard"])

    strict_flag = _resolve_strict_flag(opts, file_cfg, preset.strict)
    selected = _pick_rule_list(opts.select, file_cfg, "select")
    ignored = _pick_rule_list(opts.ignore, file_cfg, "ignore")
    extended = _pick_rule_list(opts.extend_select, file_cfg, "extend_select")

    active = _compute_active_rules(preset.active_rules, selected, extended, ignored)

    return DocsValidatorConfig(
        preset_name=preset.name,
        active_rules=active,
        strict=strict_flag,
        selected_rules=selected,
        ignored_rules=ignored,
        extended_rules=extended,
        extensions=_resolve_extensions(opts, file_cfg),
    )


def render_presets_table() -> str:
    """Render a GitHub-Flavored Markdown table of all documentation validator presets."""
    lines = [
        "Available Documentation Validator Presets:",
        "",
        "| Preset Name | Active Rules Count | Strict Mode | Active Rule Categories | Description |",
        "|---|---|---|---|---|",
    ]
    for name, preset in sorted(DOC_PRESETS.items()):
        rules_desc = (
            f"All {len(ALL_DOC_RULES)} rules"
            if len(preset.active_rules) == len(ALL_DOC_RULES)
            else ", ".join(sorted(preset.active_rules))
        )
        strict_str = "Yes" if preset.strict else "No"
        lines.append(f"| `{name}` | {len(preset.active_rules)} | {strict_str} | {rules_desc} | {preset.description} |")
    return "\n".join(lines)

# Observations must have numbered sections 1–5 (## 1. ... through ## 5. ...)

_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^(`{3,}|~{3,})(.*)$")
_SPACE_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[([^\]]+)\]\s+\(([^)]+)\)")
_HTML_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<(/)?([a-zA-Z0-9]+)(?:\s+[^>]*)?>")
_FILE_URI_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[([^\]]*)\]\(file://(/[^)#\s]+)(#[^)\s]*)?\)")

# Filesystem entries a directory map is never expected to enumerate.
# A tree line consisting of an ellipsis marks the listing as deliberately partial.

# WCAG 2.1 AA contrast floor for normal text; mermaid renders node labels at body size.


def slugify_heading(heading_text: str) -> str:
    """Generate the anchor GitHub actually emits for a heading.

    `github-slugger` lowercases, strips the characters its pattern rejects, then replaces
    **each** remaining space with a single hyphen. Two divergences mattered here:

    * underscores survive — `re.sub(r"[\\s_]+", "-")` folded them to hyphens, so every
      anchor for a heading containing `snake_case` was reported broken;
    * runs collapse — `Setup & Install` loses the `&` and keeps both spaces, giving
      `setup--install`, the anchor GitHub emits and this validator rejected.
    """
    clean = re.sub(r"`([^`]+)`", r"\1", heading_text)
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
    clean = clean.lower()
    clean = re.sub(r"[^\w\s-]", "", clean)
    return clean.replace(" ", "-").strip("-")


def _extract_heading_slug(line: str) -> str | None:
    """Extract and slugify markdown heading text if line matches heading grammar."""
    m = re.match(r"^(#{1,6})\s+(.*)$", line)
    return slugify_heading(m.group(2).strip()) if m else None


def _extract_html_anchors(line: str) -> list[str]:
    """Extract explicit HTML anchor name and id attributes from line."""
    return [m.group(1) for m in re.finditer(r'<a\s+[^>]*(?:name|id)=["\']([^"\']+)["\']', line, re.I)]


def _linting_parser_cls() -> type:
    """Return parser class that accepts every link scheme for linting."""
    from markdown_it import MarkdownIt

    class _LintingParser(MarkdownIt):
        """A CommonMark parser that keeps every link, including the ones a renderer would drop."""

        # Spelled as upstream spells it; this overrides a method markdown-it defines.
        def validateLink(self, url: str) -> bool:
            """Accept every scheme, because nothing here is rendered."""
            return True

    return _LintingParser


_PARSERS: Final[threading.local] = threading.local()


def commonmark_parser() -> MarkdownIt:
    """Return this thread's CommonMark parser, configured for linting rather than rendering.

    `validateLink` is markdown-it's renderer-safety filter: by default it refuses `file:`,
    `data:`, `javascript:` and `vbscript:` destinations, dropping them so a hostile document
    cannot emit them into HTML. That is right for a renderer and wrong for a linter — this
    validator has a rule that exists *specifically* to report `file:///` links, and the
    parser was silently removing them before the rule could see them.

    Accepting every scheme is safe here because nothing is rendered; the tokens are read and
    discarded. A library's defaults are part of its contract, and this one is tuned for a
    job we are not doing.
    """
    if not hasattr(_PARSERS, "parser"):
        parser_cls = _linting_parser_cls()
        _PARSERS.parser = parser_cls("commonmark")
    return _PARSERS.parser


def extract_heading_anchors(content: str) -> set[str]:
    """Extract all heading anchor slugs and explicit HTML anchors from markdown.

    Headings come from the parser, so a setext heading — underlined with `===` or `---`
    rather than prefixed with `#` — produces an anchor like any other. The line scan only
    recognised the ATX form, so every in-page link to a setext heading was reported broken
    against an anchor GitHub emits perfectly well.
    """
    document = parse_document(commonmark_parser(), content)
    anchors = {slug for heading in document.headings if (slug := slugify_heading(heading.text))}
    lines = content.splitlines()
    for number, line in enumerate(lines, 1):
        if number not in document.code_lines:
            anchors.update(_extract_html_anchors(line))
    return anchors


def extract_document_anchors(file_path: Path, content: str | None = None) -> set[str]:
    """Extract heading anchors and explicit targets from any documentation format."""
    text = content if content is not None else file_path.read_text(encoding="utf-8", errors="replace")
    if detect_document_format(file_path) == DocumentFormat.MARKDOWN:
        return extract_heading_anchors(text)
    return extract_polyglot_anchors(file_path, text)


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
            message=f"Suspicious unclosed or empty code fence at lines {start_l + 1}-{end_l}",
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


@dataclass
class _FenceScan:
    """Scanner state while walking fenced blocks.

    These fields travelled as an untyped `list[Any]` alongside parallel scalars, which is
    why two of these functions carried six and seven parameters: the state was passed by
    spreading it. Naming it collapses both signatures and makes the positional unpacking
    that read `in_fence, fence_char, fence_len, _ = state` unnecessary.
    """

    in_fence: bool = False
    fence_char: str = ""
    fence_len: int = 0
    start_line: int = 0
    language: str = ""
    block_lines: list[str] = field(default_factory=list)


def _process_fence_line(
    m_fence: re.Match[str],
    idx: int,
    file_str: str,
    scan: _FenceScan,
    findings: list[DocFinding],
) -> None:
    """Process a single code fence line against active state."""
    chars, info = m_fence.group(1), m_fence.group(2).strip()
    char_type, curr_len = chars[0], len(chars)
    if not scan.in_fence:
        scan.in_fence, scan.fence_char, scan.fence_len, scan.start_line = True, char_type, curr_len, idx
        return
    if char_type != scan.fence_char or curr_len < scan.fence_len:
        return
    if info:
        findings.append(
            DocFinding(
                file_path=file_str,
                line_number=idx,
                category="code_fence",
                message=f"Nested code fence at line {idx} inside a {scan.fence_len}-backtick block requires 4+ backticks",
            )
        )
    else:
        scan.in_fence = False


def _check_line_fences(lines: Sequence[str], file_str: str) -> list[DocFinding]:
    """Inspect lines for unclosed code fences and nested blocks."""
    findings: list[DocFinding] = []
    scan = _FenceScan()
    for idx, line in enumerate(lines, 1):
        m_fence = _FENCE_RE.match(line.strip())
        if m_fence:
            _process_fence_line(m_fence, idx, file_str, scan, findings)
    if scan.in_fence:
        findings.append(
            DocFinding(
                file_path=file_str,
                line_number=scan.start_line,
                category="code_fence",
                message=f"Unclosed code fence opened at line {scan.start_line}",
            )
        )
    return findings


def check_code_fences(lines: Sequence[str], file_path: Path, parser: MarkdownIt) -> list[DocFinding]:
    """Verify code fences for unclosed blocks and premature termination via inner fences."""
    if detect_document_format(file_path) != DocumentFormat.MARKDOWN:
        return []
    file_str = str(file_path)
    try:
        tokens = parser.parse("\n".join(lines))
    except Exception as exc:
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
        return (
            True,
            header_cols,
            DocFinding(
                file_path=file_str,
                line_number=line_no,
                category="table",
                message=f"Table row column count mismatch: expected {header_cols}, found {len(raw_cells)}",
            ),
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
    if detect_document_format(file_path) != DocumentFormat.MARKDOWN:
        return []
    file_str = str(file_path)
    findings: list[DocFinding] = []
    in_table, header_cols = False, 0

    for idx, (line, fenced) in enumerate(zip(lines, fenced_line_flags(lines), strict=True), 1):
        stripped = line.strip()
        if fenced:
            in_table = False
            continue
        in_table, header_cols, finding = _evaluate_table_line(stripped, in_table, header_cols, idx, file_str)
        if finding:
            findings.append(finding)
    return findings


def _check_host_or_web_target(target: str, file_str: str, line_no: int) -> tuple[bool, DocFinding | None]:
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
    """Validate internal document heading anchor reference.

    Compared for equality. The substring test accepted `#install` against a page whose only
    heading is `Installation`, and `#setup-and-install-the-thing` against `#setup` — in both
    directions — so a link GitHub cannot resolve passed the gate whose whole job is
    resolving links. Now that headings come from the parser, setext forms produce anchors
    too, which removes the one class of false negative the loose comparison was hiding.
    """
    if file_path not in known_anchors:
        return None
    target_anchors = known_anchors[file_path]
    clean_frag = re.sub(r"[^\w-]", "", fragment.lower())
    if clean_frag not in target_anchors:
        return DocFinding(
            file_path=str(file_path),
            line_number=line_no,
            category="link",
            message=f"Local anchor '#{fragment}' not found in {file_path.name}",
        )
    return None


def _check_path_target(
    path_part: str, file_path: Path, line_no: int, oracle: PathOracle
) -> DocFinding | None:
    """Validate relative target path on filesystem.

    Percent-decoded first. RFC 3986 §2.1 makes `%20` the encoding of a space, and a link to
    a file whose name contains one must write it that way — so resolving the raw text
    reported a file that exists as missing, and the author's only remedy was to break the
    link. `unquote` is the inverse the standard defines.
    """
    path_part = unquote(path_part)
    target_path = file_path.parent / path_part
    if oracle.exists(target_path):
        return None
    # Fallback relative to repository root if testing from subdirectories
    repo_root_fallback = file_path.cwd() / path_part.lstrip("./")
    if oracle.exists(repo_root_fallback):
        return None
    return DocFinding(
        file_path=str(file_path),
        line_number=line_no,
        category="link",
        message=f"Target path does not exist: '{path_part}'",
    )


@dataclass(frozen=True)
class LinkContext:
    """What a link rule needs beyond the line itself: heading anchors and a path oracle.

    Bundled rather than passed separately because the oracle has to reach the bottom of
    the call chain, and threading a sixth positional argument through each level is the
    long-parameter-list smell this repository's own detector reports.
    """

    anchors: dict[Path, set[str]]
    paths: PathOracle


def _validate_link_target(
    target: str,
    file_path: Path,
    line_no: int,
    context: LinkContext,
) -> DocFinding | None:
    """Validate a single markdown link destination against filesystem and heading anchors."""
    file_str = str(file_path)
    is_terminal, finding = _check_host_or_web_target(target, file_str, line_no)
    if is_terminal:
        return finding
    parsed = urllib.parse.urlparse(target)
    if not parsed.path and parsed.fragment:
        return _check_local_anchor(parsed.fragment, file_path, line_no, context.anchors)
    if parsed.path:
        return _check_path_target(parsed.path, file_path, line_no, context.paths)
    return None


# CommonMark opens a code span with a run of backticks and closes it with a run of exactly
# the same length. Masking rather than deleting keeps every other column where it was, so a
# finding still points at the right place in the line.
_CODE_SPAN_RE: Final[re.Pattern[str]] = re.compile(r"(?P<ticks>`+)(?P<body>.+?)(?P=ticks)")


def mask_code_spans(line: str) -> str:
    """Blank out inline code spans so their contents are not read as markup.

    A link inside backticks is not a link. Without this, any document *about* Markdown
    reports its own examples as broken links — `[text](url)` in prose resolved `url` as a
    path and said it did not exist. The rule is the one this repository keeps arriving at
    from other directions: text inside a quoting construct is data, and a scanner that
    ignores the quoting turns it back into syntax.
    """
    return _CODE_SPAN_RE.sub(lambda match: " " * len(match.group(0)), line)


def _resolve_effective_anchors(
    file_path: Path,
    lines: Sequence[str],
    known_anchors: dict[Path, set[str]] | None,
) -> dict[Path, set[str]]:
    """Resolve known anchors map, populating local document anchors if missing."""
    anchors = dict(known_anchors) if known_anchors is not None else {}
    if file_path not in anchors:
        anchors[file_path] = extract_document_anchors(file_path, "\n".join(lines))
    return anchors


def _collect_parsed_link_findings(
    links: Sequence[MarkdownLink],
    file_path: Path,
    skip: frozenset[int],
    context: LinkContext,
) -> list[DocFinding]:
    """Validate target destinations for non-skipped parsed markdown links."""
    findings: list[DocFinding] = []
    for link in links:
        if link.line in skip:
            continue
        finding = _validate_link_target(link.href.strip(), file_path, link.line, context)
        if finding:
            findings.append(finding)
    return findings


def _collect_whitespace_link_findings(
    lines: Sequence[str],
    file_str: str,
    skip: frozenset[int],
) -> list[DocFinding]:
    """Scan raw lines for malformed links containing whitespace between brackets."""
    findings: list[DocFinding] = []
    for idx, line in enumerate(lines, 1):
        if idx not in skip and _SPACE_LINK_RE.search(mask_code_spans(line)):
            findings.append(
                DocFinding(
                    file_path=file_str,
                    line_number=idx,
                    category="link",
                    message=f"Malformed link with whitespace between brackets: '{line.strip()}'",
                )
            )
    return findings


def check_markdown_links(
    lines: Sequence[str],
    file_path: Path,
    known_anchors: dict[Path, set[str]] | None = None,
    oracle: PathOracle | None = None,
) -> list[DocFinding]:
    """Inspect all documentation links for broken paths, missing anchors, and host-specific URIs."""
    fmt = detect_document_format(file_path)
    context = LinkContext(
        anchors=_resolve_effective_anchors(file_path, lines, known_anchors),
        paths=oracle if oracle is not None else PathOracle(),
    )
    if fmt != DocumentFormat.MARKDOWN:
        poly_links = extract_polyglot_links(lines, file_path)
        findings = _collect_parsed_link_findings(poly_links, file_path, frozenset(), context)
        return sorted(findings, key=lambda f: (f.line_number, f.message))

    content = "\n".join(lines)
    document = parse_document(commonmark_parser(), content)
    skip = document.code_lines | document.html_lines
    findings = _collect_parsed_link_findings(document.links, file_path, skip, context)
    findings.extend(_collect_whitespace_link_findings(lines, str(file_path), skip))
    return sorted(findings, key=lambda f: (f.line_number, f.message))


def _check_asciidoc_delimiters(lines: Sequence[str], file_str: str) -> list[DocFinding]:
    """Audit AsciiDoc listing delimiter pairing to prevent unclosed source blocks."""
    in_block = False
    block_start = 0
    for idx, line in enumerate(lines, 1):
        if line.strip() == "----":
            in_block = not in_block
            block_start = idx if in_block else 0
    if in_block:
        return [
            DocFinding(
                file_path=file_str,
                line_number=block_start,
                category="multi_language",
                message=f"Unclosed AsciiDoc source block starting at line {block_start}",
            )
        ]
    return []


def _check_rst_directives(lines: Sequence[str], file_str: str) -> list[DocFinding]:
    """Audit reStructuredText directive syntax for single-colon mistakes."""
    findings: list[DocFinding] = []
    for idx, line in enumerate(lines, 1):
        if re.match(r"^\s*\.\.\s+[a-zA-Z0-9_-]+:[^:]", line):
            findings.append(
                DocFinding(
                    file_path=file_str,
                    line_number=idx,
                    category="multi_language",
                    message="Malformed reStructuredText directive: missing double colon '::'",
                )
            )
    return findings


def check_polyglot_documentation(
    lines: Sequence[str],
    file_path: Path,
    known_anchors: dict[Path, set[str]] | None = None,
    oracle: PathOracle | None = None,
) -> list[DocFinding]:
    """Validate multi-format document syntax, directives, and format-specific structures."""
    fmt = detect_document_format(file_path)
    if fmt == DocumentFormat.MARKDOWN:
        return []
    file_str = str(file_path)
    if fmt == DocumentFormat.ASCIIDOC:
        return _check_asciidoc_delimiters(lines, file_str)
    if fmt == DocumentFormat.RST:
        return _check_rst_directives(lines, file_str)
    return []


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


def _validate_rust_snippet(code: str, start_line: int, file_str: str) -> DocFinding | None:
    return validate_polyglot_source_snippet("Rust", code, start_line, file_str)


def _validate_go_snippet(code: str, start_line: int, file_str: str) -> DocFinding | None:
    return validate_polyglot_source_snippet("Go", code, start_line, file_str)


def _validate_ts_snippet(code: str, start_line: int, file_str: str) -> DocFinding | None:
    return validate_polyglot_source_snippet("TypeScript", code, start_line, file_str)


def _validate_js_snippet(code: str, start_line: int, file_str: str) -> DocFinding | None:
    return validate_polyglot_source_snippet("JavaScript", code, start_line, file_str)


_SNIPPET_VALIDATORS: Final[dict[str, Callable[[str, int, str], DocFinding | None]]] = {
    "python": _validate_python_snippet,
    "py": _validate_python_snippet,
    "json": _validate_json_snippet,
    "yaml": _validate_yaml_snippet,
    "yml": _validate_yaml_snippet,
    "toml": validate_toml_snippet,
    "xml": validate_xml_snippet,
    "svg": validate_xml_snippet,
    "xhtml": validate_xml_snippet,
    "html": validate_xml_snippet,
    "ini": validate_ini_snippet,
    "cfg": validate_ini_snippet,
    "conf": validate_ini_snippet,
    "properties": validate_ini_snippet,
    "sh": validate_shell_snippet,
    "bash": validate_shell_snippet,
    "shell": validate_shell_snippet,
    "zsh": validate_shell_snippet,
    "rust": _validate_rust_snippet,
    "rs": _validate_rust_snippet,
    "go": _validate_go_snippet,
    "ts": _validate_ts_snippet,
    "typescript": _validate_ts_snippet,
    "js": _validate_js_snippet,
    "javascript": _validate_js_snippet,
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
    m_fence: re.Match[str], scan: _FenceScan, line_no: int, file_str: str
) -> DocFinding | None:
    """Enter or leave a fenced block, updating the scan and returning any finding."""
    if not scan.in_fence:
        info = m_fence.group(2).strip().split()
        scan.in_fence, scan.language = True, info[0].lower() if info else ""
        scan.start_line, scan.block_lines = line_no, []
        return None
    finding = _validate_single_snippet(scan.language, scan.block_lines, scan.start_line, file_str)
    scan.in_fence, scan.language, scan.start_line, scan.block_lines = False, "", 0, []
    return finding


def _process_snippet_line(
    line: str, idx: int, file_str: str, scan: _FenceScan, findings: list[DocFinding]
) -> None:
    """Process a single line for embedded code snippets."""
    m_fence = _FENCE_RE.match(line.strip())
    if m_fence:
        finding = _handle_fence_transition(m_fence, scan, idx, file_str)
        if finding:
            findings.append(finding)
    elif scan.in_fence:
        scan.block_lines.append(line)


def _collect_polyglot_snippets(lines: Sequence[str], fmt: DocumentFormat) -> list[PolyglotSnippet]:
    """Extract snippets for non-markdown documentation formats."""
    if fmt == DocumentFormat.RST:
        return extract_rst_snippets(lines)
    if fmt == DocumentFormat.ASCIIDOC:
        return extract_asciidoc_snippets(lines)
    if fmt == DocumentFormat.HTML:
        return extract_html_snippets(lines)
    return []


def check_embedded_snippets(lines: Sequence[str], file_path: Path) -> list[DocFinding]:
    """Syntactically parse embedded code blocks in supported languages."""
    file_str = str(file_path)
    fmt = detect_document_format(file_path)
    if fmt != DocumentFormat.MARKDOWN:
        snippets = _collect_polyglot_snippets(lines, fmt)
        return [
            finding
            for snip in snippets
            if (finding := _validate_single_snippet(snip.language, snip.code.splitlines(), snip.start_line, file_str))
        ]

    findings: list[DocFinding] = []
    scan = _FenceScan()
    for idx, line in enumerate(lines, 1):
        _process_snippet_line(line, idx, file_str, scan, findings)
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
    stack: list[tuple[str, int]] = []

    for idx, (line, fenced) in enumerate(zip(lines, fenced_line_flags(lines), strict=True), 1):
        if not fenced:
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


def _discover_markdown_files(dir_path: Path, extensions: Sequence[str]) -> list[Path]:
    """Discover the repository's own markdown files."""
    return sorted(iter_source_files(dir_path, extensions))


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


def _process_mermaid_line(line: str, in_mermaid: bool) -> tuple[str, bool, int]:
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


def _fix_linebreak_hygiene(lines: list[str]) -> tuple[list[str], int]:
    """Trim accidental single trailing spaces while preserving intentional 2-space linebreaks."""
    flags = fenced_line_flags(lines)
    fixes = 0
    result: list[str] = []
    for line, fenced in zip(lines, flags, strict=True):
        if not fenced and line.endswith(" ") and not line.endswith("  "):
            result.append(line[:-1])
            fixes += 1
        else:
            result.append(line)
    return result, fixes


def auto_fix_content(content: str, doc_path: Path = Path("document.md")) -> tuple[str, int]:
    """Remediate fixable documentation issues in markdown string."""
    lines = content.splitlines()
    lines, fence_fixes = _fix_unclosed_fences(lines)
    lines, mermaid_fixes = _fix_mermaid_blocks(lines)
    lines, linebreak_fixes = _fix_linebreak_hygiene(lines)
    # `splitlines()` discards the final terminator, so rejoining must always restore it
    # when the original had one. The guard that used to stand here — append only if the
    # join did not already end in a newline — looked equivalent and was not: a document
    # ending in two newlines splits to a trailing empty element, the join therefore ends
    # in a newline already, and the terminator was dropped. Each call removed one more
    # blank line while reporting zero repairs, so the text changed and the count said it
    # had not. Found by `tools/fuzz_harness.py`; the input is kept as a regression case.
    reconstituted = "\n".join(lines)
    if content.endswith("\n"):
        reconstituted += "\n"
    final_content, link_fixes = _fix_absolute_links(reconstituted, doc_path)
    return final_content, fence_fixes + mermaid_fixes + linebreak_fixes + link_fixes


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

    def __init__(self, config: DocsValidatorConfig | None = None) -> None:
        self.config = config or resolve_doc_config()
        self._local = threading.local()

    @property
    def parser(self) -> MarkdownIt:
        """Thread-isolated parser instance avoiding concurrent state corruption."""
        if not hasattr(self._local, "parser"):
            from markdown_it import MarkdownIt

            self._local.parser = MarkdownIt("commonmark")
        return self._local.parser

    def fix_file(self, file_path: Path) -> int:
        """Remediate documentation issues in file in-place."""
        return auto_fix_file(file_path)

    def fix_directory(self, dir_path: Path, extensions: Sequence[str] = (".md", ".markdown")) -> int:
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
        oracle: PathOracle | None = None,
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
        return self._run_all_checks(content.splitlines(), file_path, known_anchors, oracle)

    def validate_content(
        self,
        content: str,
        file_path: Path = Path("document.md"),
        known_anchors: dict[Path, set[str]] | None = None,
    ) -> list[DocFinding]:
        """Validate markdown content directly from string."""
        return self._run_all_checks(content.splitlines(), file_path, known_anchors)

    def _run_all_checks(
        self,
        lines: Sequence[str],
        file_path: Path,
        known_anchors: dict[Path, set[str]] | None,
        oracle: PathOracle | None = None,
    ) -> list[DocFinding]:
        """Run every validation rule. The single place a new rule is registered.

        `validate_file` and `validate_content` previously kept parallel lists, so a rule
        added to one silently did not run on the other — including the CLI path.
        """
        paths = oracle if oracle is not None else PathOracle()
        rule_map: tuple[tuple[str, Callable[[], list[DocFinding]]], ...] = (
            ("code_fence", lambda: check_code_fences(lines, file_path, self.parser)),
            ("mermaid", lambda: check_mermaid_diagrams(lines, file_path)),
            ("table", lambda: check_markdown_tables(lines, file_path)),
            ("link", lambda: check_markdown_links(lines, file_path, known_anchors, paths)),
            ("code_snippet", lambda: check_embedded_snippets(lines, file_path)),
            ("html_tag", lambda: check_html_tags(lines, file_path)),
            ("structure", lambda: check_observation_structure(lines, file_path)),
            ("directory_map", lambda: check_directory_maps(lines, file_path, paths)),
            ("pattern_header", lambda: check_pattern_header(lines, file_path)),
            ("sanitization", lambda: check_documentation_sanitization(lines, file_path)),
            ("multi_language", lambda: check_polyglot_documentation(lines, file_path, known_anchors, paths)),
            ("linebreak", lambda: check_linebreak_hygiene(lines, file_path)),
            ("math", lambda: check_latex_math_hygiene(lines, file_path)),
        )
        findings = [
            finding
            for rule_name, check_fn in rule_map
            if self.config.is_rule_active(rule_name)
            for finding in check_fn()
        ]
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

    def check_polyglot(
        self,
        content: str | Sequence[str],
        file_path: Path = Path("document.rst"),
        known_anchors: dict[Path, set[str]] | None = None,
        oracle: PathOracle | None = None,
    ) -> list[DocFinding]:
        """Validate multi-format document syntax, links, and code snippets."""
        return check_polyglot_documentation(self._to_lines(content), file_path, known_anchors, oracle)

    def check_linebreak_hygiene(
        self, content: str | Sequence[str], file_path: Path = Path("document.md")
    ) -> list[DocFinding]:
        """Validate CommonMark hard linebreaks and trailing whitespace hygiene."""
        return check_linebreak_hygiene(self._to_lines(content), file_path)

    def check_latex_math_hygiene(
        self, content: str | Sequence[str], file_path: Path = Path("document.md")
    ) -> list[DocFinding]:
        """Validate LaTeX and KaTeX math expressions for unescaped characters."""
        return check_latex_math_hygiene(self._to_lines(content), file_path)

    def validate_directory(
        self,
        dir_path: Path,
        extensions: Sequence[str] | None = None,
    ) -> DocValidationReport:
        """Scan and validate all documentation files in directory recursively."""
        start_time = time.monotonic()
        target_exts = extensions if extensions is not None else self.config.extensions
        doc_files = _discover_markdown_files(dir_path, target_exts)

        known_anchors = (
            {f: extract_document_anchors(f, f.read_text(encoding="utf-8")) for f in doc_files}
            if self.config.is_rule_active("link")
            else {}
        )
        all_findings = _scan_files_parallel(self, doc_files, known_anchors)
        elapsed = time.monotonic() - start_time
        return _build_directory_report(doc_files, all_findings, elapsed)


def _scan_files_parallel(
    validator: DocsValidator,
    md_files: Sequence[Path],
    anchors: dict[Path, set[str]],
) -> list[DocFinding]:
    """Execute validation across files concurrently."""
    oracle = PathOracle()
    all_findings: list[DocFinding] = []
    with ThreadPoolExecutor() as executor:
        findings_lists = executor.map(
            lambda f: validator.validate_file(f, known_anchors=anchors, oracle=oracle),
            md_files,
        )
        for f_list in findings_lists:
            all_findings.extend(f_list)
    return all_findings


def _build_directory_report(
    md_files: Sequence[Path],
    findings: Sequence[DocFinding],
    elapsed: float,
) -> DocValidationReport:
    """Assemble final validation report for directory scan."""
    errors, warnings = _tally_severities(findings)
    files_with = len({f.file_path for f in findings})
    return DocValidationReport(
        total_files=len(md_files),
        files_with_findings=files_with,
        error_count=errors,
        warning_count=warnings,
        is_valid=not findings,
        findings=list(findings),
        duration_seconds=elapsed,
    )


def _fix_target(target: Path, validator: DocsValidator) -> int:
    """Apply automatic remediation to a single file or directory target."""
    return validator.fix_file(target) if target.is_file() else validator.fix_directory(target)


def _file_report(file_path: Path, validator: DocsValidator) -> DocValidationReport:
    """Construct report for a single file validation."""
    findings = validator.validate_file(file_path)
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")
    return DocValidationReport(
        total_files=1,
        files_with_findings=int(bool(findings)),
        error_count=errors,
        warning_count=warnings,
        is_valid=not findings,
        findings=findings,
        duration_seconds=0.0,
    )


def _resolve_report(target: Path, validator: DocsValidator) -> DocValidationReport:
    """Generate validation report for single file or directory."""
    if target.is_file():
        return _file_report(target, validator)
    return validator.validate_directory(target)


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


def _matches_rule(finding: DocFinding, canonical: str, raw_rule: str) -> bool:
    """Predicate determining if finding matches canonical or raw rule identifier."""
    category = finding.category.lower()
    return canonical in category or raw_rule in category


def _tally_severities(findings: Sequence[DocFinding]) -> tuple[int, int]:
    """Tally error and warning severities in a single pass."""
    counts = Counter(f.severity for f in findings)
    return counts["error"], counts["warning"]


def _filter_report_by_rule(report: DocValidationReport, rule: str | None) -> None:
    """Filter validation findings by category rule in-place."""
    if not rule:
        return
    canonical = normalize_doc_rule_name(rule)
    lowered = rule.lower()
    filtered = [f for f in report.findings if _matches_rule(f, canonical, lowered)]
    report.findings = filtered
    report.error_count, report.warning_count = _tally_severities(filtered)
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


def _apply_fixes(targets: Sequence[Path], validator: DocsValidator) -> None:
    """Run autofix remediation on targets and notify console."""
    fixes = sum(_fix_target(target, validator) for target in targets)
    if fixes > 0:
        print(f"🔧 Applied {fixes} automatic remediation fix(es) to documentation.")


def _cli_select_arg(args: argparse.Namespace) -> list[str] | None:
    """Extract selected rules from select or rule CLI options."""
    if args.select:
        return [args.select]
    if args.rule:
        return [args.rule]
    return None


def _cli_extensions_arg(args: argparse.Namespace) -> tuple[str, ...] | None:
    """Extract extensions override from CLI flags."""
    if args.all_formats:
        return POLYGLOT_EXTENSIONS
    if args.extensions:
        return tuple(ext.strip() for ext in args.extensions.split(",") if ext.strip())
    return None


def parse_cli_args(
    argv: Sequence[str] | None = None,
) -> tuple[argparse.Namespace, DocsValidatorConfig, bool]:
    """Parse command line arguments and resolve validator configuration."""
    parser = argparse.ArgumentParser(description="Vibes Documentation Syntax & Link Validator")
    parser.add_argument("paths", nargs="*", default=[], help="File or directory paths to validate")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    parser.add_argument(
        "--fix", action="store_true", help="Automatically remediate fixable documentation issues"
    )
    parser.add_argument("--json", action="store_true", help="Emit report as JSON")
    parser.add_argument("--rule", help="Filter findings by category rule (legacy option)")
    parser.add_argument(
        "--preset",
        help="Select calibrated rule preset (standard, strict, polyglot, structure_only, links_only, code_only, sanitization_only)",
    )
    parser.add_argument("--select", help="Comma-separated rules or codes to activate (e.g. DOC001,DOC004)")
    parser.add_argument("--ignore", help="Comma-separated rules or codes to exclude (e.g. DOC008)")
    parser.add_argument("--extend-select", help="Comma-separated additional rules to activate")
    parser.add_argument("--config", type=Path, help="Path to configuration file (pyproject.toml or .markdownlint.json)")
    parser.add_argument("--list-presets", action="store_true", help="Display all available presets in markdown table format")
    parser.add_argument("--extensions", help="Comma-separated file extensions to validate (e.g. .md,.rst,.html)")
    parser.add_argument(
        "--all-formats",
        action="store_true",
        help="Validate all supported documentation formats (.md, .rst, .adoc, .html, .txt)",
    )
    args = parser.parse_args(argv)

    if args.list_presets:
        return args, resolve_doc_config(), True

    overrides = DocConfigOverrides(
        preset_name=args.preset,
        strict=True if args.strict else None,
        select=_cli_select_arg(args),
        ignore=[args.ignore] if args.ignore else None,
        extend_select=[args.extend_select] if args.extend_select else None,
        config_file=args.config,
        extensions=_cli_extensions_arg(args),
    )
    return args, resolve_doc_config(overrides), False


def _handle_json_output(report: DocValidationReport) -> int:
    """Print report as JSON and return corresponding exit code."""
    print(json.dumps(report.to_dict(), indent=2))
    return int(not report.is_valid)


def _collect_targets(raw_paths: Sequence[str] | None) -> list[Path]:
    """Resolve CLI target paths into absolute Path instances."""
    paths = raw_paths or ["."]
    return [Path(raw).resolve() for raw in paths]


def _execute_validation(
    targets: Sequence[Path],
    validator: DocsValidator,
    fix: bool,
) -> DocValidationReport:
    """Run autofix if requested and return consolidated validation report."""
    if fix:
        _apply_fixes(targets, validator)
    reports = [_resolve_report(target, validator) for target in targets]
    return _merge_reports(reports)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for standalone vibes doc validator."""
    args, config, is_list = parse_cli_args(argv)
    if is_list:
        print(render_presets_table())
        return 0

    validator = DocsValidator(config=config)
    targets = _collect_targets(args.paths)
    report = _execute_validation(targets, validator, args.fix)

    if args.rule:
        _filter_report_by_rule(report, args.rule)
    if args.json:
        return _handle_json_output(report)

    _render_console_report(report)
    return _determine_exit_code(report, config.strict or args.strict)


if __name__ == "__main__":
    sys.exit(main())
