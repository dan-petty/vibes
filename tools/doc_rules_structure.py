#!/usr/bin/env python3
"""Structural rules: required observation sections and embedded directory maps.

Both rules validate a document's shape against something outside the text — a required
five-section skeleton, or the filesystem the document claims to describe. Neither can be
checked by reading the prose, which is why a text tree drifts silently until an oracle
diffs it.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from doc_core import DocFinding, PathOracle, extract_fenced_blocks
from sanitization_policy import is_documentable

_TREE_ENTRY_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<indent>(?:[\u2502]   |    )*)(?:\u251c\u2500\u2500|\u2514\u2500\u2500) (?P<name>\S+)"
)

# Filesystem entries a directory map is never expected to enumerate: caches and build
# artifacts that exist only because someone ran a tool. Dot-prefixed entries are skipped
# separately. A map should describe what a reader navigates, not what a build produced.
TREE_IGNORED_NAMES: Final[frozenset[str]] = frozenset({"__pycache__", "node_modules", "build", "dist"})
TREE_IGNORED_SUFFIXES: Final[tuple[str, ...]] = (".egg-info",)
# A tree line consisting of an ellipsis marks the listing as deliberately partial.
TREE_ELLIPSIS: Final[frozenset[str]] = frozenset({"...", "\u2026"})

_OBSERVATION_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"^\d+-.+\.md$")
_OBSERVATION_NUMBERED_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^##\s+(\d+)\.")

# A pattern declares this metadata block so a reader can judge applicability before
# reading the body. Three competing vocabularies (Pattern Type, Category, prose subtitle)
# had accumulated across 19 files before this was made mechanical.
_HOST_IPV4_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b(?!/\d)")
# Closed domain of addresses documentation is expected to name. Python's ipaddress marks
# the RFC 5737 documentation blocks as private, so without this the rule would flag the
# exact ranges the sanitization policy mandates. The link-local metadata address is a
# well-known public constant, not anybody's machine, and every SSRF discussion names it.
_DOCUMENTABLE_NETWORKS: Final[tuple[ipaddress.IPv4Network, ...]] = (
    ipaddress.IPv4Network("192.0.2.0/24"),
    ipaddress.IPv4Network("198.51.100.0/24"),
    ipaddress.IPv4Network("203.0.113.0/24"),
    ipaddress.IPv4Network("169.254.169.254/32"),
)
_INTERNAL_HOSTNAME_RE: Final[re.Pattern[str]] = re.compile(r"\b[a-z0-9-]+\.(?:lan|local|internal|home)\b", re.I)
# A waiver for documents that must quote the thing they warn about. The justification is
# required, not decorative: an unexplained waiver is how a real leak gets silenced.
_SANITIZATION_WAIVER_RE: Final[re.Pattern[str]] = re.compile(
    r"<!--\s*docs:\s*allow\[sanitization\]\s*[-—:]*\s*(?P<reason>\S[^>]*?)\s*-->"
)
MIN_SANITIZATION_WAIVER_CHARS: Final[int] = 12

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


def _is_directory_map(entries: Sequence[TreeEntry], oracle: PathOracle) -> bool:
    """Distinguish a filesystem map from other art drawn with the same box characters.

    Trace waterfalls and AST dumps use the identical `\u251c\u2500\u2500` glyphs. A map is
    identified structurally: it marks at least one child as a directory with a trailing
    slash, and most of what it names exists on disk. A map whose every single entry has
    vanished is indistinguishable from unrelated art without guessing, and is not reported.
    """
    if not entries or not any(entry.is_directory for entry in entries):
        return False
    resolved = sum(1 for entry in entries if oracle.kind(entry.path) is not None)
    return resolved * 2 >= len(entries)


def _check_tree_entry_exists(
    entry: TreeEntry, root: Path, file_str: str, oracle: PathOracle
) -> DocFinding | None:
    """Verify a mapped path still exists on disk with the declared kind."""
    expected = "dir" if entry.is_directory else "file"
    if oracle.kind(entry.path) == expected:
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


def _undeclared_children(
    parent: Path, declared: set[str], kinds: set[bool], oracle: PathOracle
) -> list[str]:
    """Return on-disk children of the kinds the map enumerates that the map omits."""
    listing = oracle.listing(parent)
    candidates = (
        name
        for name, kind in sorted(listing.items())
        if not name.startswith(".")
        and name not in TREE_IGNORED_NAMES
        and not name.endswith(TREE_IGNORED_SUFFIXES)
        and (kind == "dir") in kinds
    )
    return [name for name in candidates if name not in declared]


def _check_tree_completeness(
    entries: Sequence[TreeEntry], root: Path, file_str: str, oracle: PathOracle
) -> list[DocFinding]:
    """Report children absent from a map that already enumerates that kind of sibling."""
    children: dict[Path, list[TreeEntry]] = {}
    for entry in entries:
        children.setdefault(entry.path.parent, []).append(entry)
    findings: list[DocFinding] = []
    for parent, listed in children.items():
        if not oracle.listing(parent):
            continue
        declared = {entry.path.name for entry in listed}
        missing = _undeclared_children(
            parent, declared, {entry.is_directory for entry in listed}, oracle
        )
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


def check_directory_maps(
    lines: Sequence[str], file_path: Path, oracle: PathOracle | None = None
) -> list[DocFinding]:
    """Diff every embedded text directory tree against the filesystem it describes."""
    blocks = _extract_text_blocks(lines)
    if not blocks:
        # Most documents draw no tree at all. Resolving the root first asked the
        # filesystem about every document to serve the few that embed a map.
        return []
    oracle = oracle if oracle is not None else PathOracle()
    root = file_path.parent.resolve()
    if oracle.kind(root) != "dir":
        return []
    findings: list[DocFinding] = []
    for block in blocks:
        entries = _tree_entries(block, root)
        if not _is_directory_map(entries, oracle):
            continue
        findings.extend(
            finding
            for finding in (
                _check_tree_entry_exists(entry, root, str(file_path), oracle) for entry in entries
            )
            if finding
        )
        findings.extend(_check_tree_completeness(entries, root, str(file_path), oracle))
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


def _private_host_addresses(text: str) -> list[str]:
    """Return concrete RFC 1918 host addresses, ignoring CIDR ranges.

    A CIDR range is how the sanitization rule itself is written down; a bare host address
    is someone's actual machine. Only the second leaks, so only the second is reported.
    """
    candidates = (_parse_address(m.group(0)) for m in _HOST_IPV4_RE.finditer(text))
    return [
        str(address)
        for address in candidates
        if address is not None
        and address.is_private
        and not address.is_loopback
        and not is_documentable(address)
    ]


def _parse_address(token: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse a dotted-quad token, or return None when it is not an address."""
    try:
        return ipaddress.ip_address(token)
    except ValueError:
        return None


def check_documentation_sanitization(lines: Sequence[str], file_path: Path) -> list[DocFinding]:
    """Flag concrete private addresses and internal hostnames in prose.

    The AST sentinel enforces this for Python and always has. Markdown was never checked,
    which is how a concrete LAN address and port survived in an observation *about* egress
    sanitization, and another in the taxonomy entry defining information leakage.
    """
    content = "\n".join(lines)
    waiver = _SANITIZATION_WAIVER_RE.search(content)
    if waiver and len(waiver.group("reason")) >= MIN_SANITIZATION_WAIVER_CHARS:
        return []
    findings: list[DocFinding] = []
    for number, line in enumerate(lines, 1):
        leaks = _private_host_addresses(line) + _INTERNAL_HOSTNAME_RE.findall(line)
        findings.extend(
            DocFinding(
                file_path=str(file_path),
                line_number=number,
                category="sanitization",
                message=(
                    f"Concrete private address or internal hostname '{leak}' in documentation. "
                    "Use an RFC 5737 range, `example.com`, or an abstract placeholder. A CIDR "
                    "range stating the rule is fine; a host address is somebody's machine."
                ),
            )
            for leak in leaks
        )
    return findings


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
