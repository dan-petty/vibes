#!/usr/bin/env python3
"""Just-In-Time (JIT) instruction governor and counterfactual ablation engine.

Resolves instruction ratchet bloat and lost-in-the-middle attention decay by:
1. Parsing instruction sets (e.g. AGENTS.md) into discrete sections and token weights.
2. Mapping mechanical gate codes to rule sections (Rule Attribution Matrix).
3. Synthesizing lean, two-tier JIT prompt envelopes scoped to active diff targets.
4. Identifying counterfactual ablation candidates for sections with 100% mechanical gating.
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

DEFAULT_TIER1_MAX_TOKENS: Final[int] = 2000
DEFAULT_TIER2_MAX_TOKENS_PER_DOMAIN: Final[int] = 1500

_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(#{1,3})\s+(.+)$", re.M)
_RULE_CODE_RE: Final[re.Pattern[str]] = re.compile(r"\b([A-Z]{2,6}\d{3})\b")

_EXTENSION_DOMAIN_MAP: Final[dict[str, str]] = {
    ".py": "python",
    ".pyi": "python",
    ".md": "docs",
    ".markdown": "docs",
    ".rs": "polyglot",
    ".go": "polyglot",
    ".cpp": "polyglot",
    ".c": "polyglot",
    ".h": "polyglot",
    ".ts": "polyglot",
    ".yml": "workflows",
    ".yaml": "workflows",
}


class RetentionPriority(StrEnum):
    """Retention priority classification for instruction sections."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    ABLATION_CANDIDATE = "ablation_candidate"


@dataclass(frozen=True)
class InstructionSection:
    """Discrete section parsed from an instruction markdown document."""

    heading: str
    level: int
    token_count: int
    line_start: int
    line_end: int
    rule_codes: tuple[str, ...]
    body: str

    def to_dict(self) -> dict[str, Any]:
        """Convert section metadata to dictionary representation."""
        return {
            "heading": self.heading,
            "level": self.level,
            "token_count": self.token_count,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "rule_codes": list(self.rule_codes),
        }


@dataclass(frozen=True)
class RuleAttribution:
    """Mapping between a mechanical gate code and an instruction section."""

    gate_code: str
    rule_name: str
    section_heading: str
    is_mechanically_enforced: bool
    gate_tool: str

    def to_dict(self) -> dict[str, Any]:
        """Convert attribution to dictionary representation."""
        return {
            "gate_code": self.gate_code,
            "rule_name": self.rule_name,
            "section_heading": self.section_heading,
            "is_mechanically_enforced": self.is_mechanically_enforced,
            "gate_tool": self.gate_tool,
        }


@dataclass(frozen=True)
class JITPromptEnvelope:
    """Synthesized two-tier prompt envelope scoped to target paths."""

    tier1_tokens: int
    tier2_tokens: int
    total_tokens: int
    domains: tuple[str, ...]
    prompt_text: str

    def to_dict(self) -> dict[str, Any]:
        """Convert prompt envelope metadata to dictionary representation."""
        return {
            "tier1_tokens": self.tier1_tokens,
            "tier2_tokens": self.tier2_tokens,
            "total_tokens": self.total_tokens,
            "domains": list(self.domains),
        }


@dataclass(frozen=True)
class AblationCandidate:
    """Instruction section evaluated for counterfactual pruning or compression."""

    heading: str
    token_savings: int
    mechanically_covered: bool
    priority: RetentionPriority
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        """Convert candidate to dictionary representation."""
        return {
            "heading": self.heading,
            "token_savings": self.token_savings,
            "mechanically_covered": self.mechanically_covered,
            "priority": self.priority.value,
            "recommendation": self.recommendation,
        }


# Canonical gate registry mapping codes to oracle tools and names
KNOWN_GATES: Final[tuple[tuple[str, str, str], ...]] = (
    ("CC001", "Cyclomatic Complexity Cap", "sentinel.py"),
    ("ND001", "Nesting Depth Cap", "sentinel.py"),
    ("ZT001", "Zero-Trust RFC Sanitization", "sentinel.py"),
    ("DOC012", "CommonMark Linebreak Hygiene", "docs_validator.py"),
    ("DOC013", "KaTeX Math Hygiene", "docs_validator.py"),
    ("DOC002", "Mermaid Syntax & Node Quotes", "docs_validator.py"),
    ("AIBOM001", "Arbitrary Code Remote Model Loading", "aibom_scanner.py"),
    ("ROT002", "Lost-In-The-Middle Invariant Decay", "context_rot_auditor.py"),
    ("EBPF001", "Unauthorized Process Execution", "ebpf_tracer.py"),
    ("SGM001", "Dangling Symbol Call", "code_memory.py"),
    ("CPP001", "C++ Memory Deallocation Violation", "cpp_lifetime_sentinel.py"),
)


def estimate_tokens(text: str) -> int:
    """Estimate token count based on whitespace word boundaries."""
    words = text.split()
    return max(1, len(words)) if words else 0


def parse_instruction_sections(markdown_text: str) -> list[InstructionSection]:
    """Parse Markdown headings into discrete instruction sections with token counts."""
    matches = list(_HEADING_RE.finditer(markdown_text))
    if not matches:
        return [_build_root_fallback_section(markdown_text)]

    sections: list[InstructionSection] = []
    line_offsets = _compute_line_starts(markdown_text)

    for i in range(len(matches)):
        sec = _build_section_record(markdown_text, matches, i, line_offsets)
        sections.append(sec)

    return sections


def _build_root_fallback_section(text: str) -> InstructionSection:
    """Construct fallback section for documents without Markdown headings."""
    lines = text.splitlines()
    tokens = estimate_tokens(text)
    codes = tuple(sorted(set(_RULE_CODE_RE.findall(text))))
    return InstructionSection(
        heading="Root",
        level=1,
        token_count=tokens,
        line_start=1,
        line_end=len(lines),
        rule_codes=codes,
        body=text,
    )


def _compute_line_starts(text: str) -> list[int]:
    """Compute character byte offsets for every line start."""
    offsets = [0]
    for m in re.finditer(r"\n", text):
        offsets.append(m.end())
    return offsets


def _offset_to_line(char_offset: int, line_offsets: list[int]) -> int:
    """Find line number (1-indexed) from character offset."""
    return bisect.bisect_right(line_offsets, char_offset)


def _build_section_record(
    text: str,
    matches: list[re.Match[str]],
    idx: int,
    offsets: list[int],
) -> InstructionSection:
    """Construct an InstructionSection from regex match boundaries."""
    match = matches[idx]
    level = len(match.group(1))
    heading = match.group(2).strip()

    start_char = match.start()
    end_char = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
    body = text[start_char:end_char]

    line_start = _offset_to_line(start_char, offsets)
    line_end = _offset_to_line(end_char, offsets)
    tokens = estimate_tokens(body)
    rule_codes = tuple(sorted(set(_RULE_CODE_RE.findall(body))))

    return InstructionSection(
        heading=heading,
        level=level,
        token_count=tokens,
        line_start=line_start,
        line_end=line_end,
        rule_codes=rule_codes,
        body=body,
    )


class RuleAttributionMatrix:
    """Maps mechanical gate codes to instruction sections."""

    def __init__(self, sections: Sequence[InstructionSection]) -> None:
        self.sections: Final[tuple[InstructionSection, ...]] = tuple(sections)

    def build_attributions(self) -> list[RuleAttribution]:
        """Produce full attribution list for all recognized gate codes."""
        attributions: list[RuleAttribution] = []
        for code, name, tool in KNOWN_GATES:
            matched_heading = self._find_matching_section(code, name)
            attributions.append(
                RuleAttribution(
                    gate_code=code,
                    rule_name=name,
                    section_heading=matched_heading,
                    is_mechanically_enforced=bool(tool),
                    gate_tool=tool,
                )
            )
        return attributions

    def _find_matching_section(self, code: str, name: str) -> str:
        """Find the section title containing the rule code or keywords."""
        keywords = {k.lower() for k in name.split() if len(k) > 3}
        for sec in self.sections:
            if self._matches_section(sec, code, keywords):
                return sec.heading
        return "General Invariants"

    @staticmethod
    def _matches_section(sec: InstructionSection, code: str, keywords: set[str]) -> bool:
        """Check if section contains code or any matching keyword."""
        if code in sec.rule_codes:
            return True
        sec_lower = sec.heading.lower()
        return any(kw in sec_lower for kw in keywords)


def _classify_domain(path: Path) -> str | None:
    """Classify a file path into its relevant domain category."""
    if ".github" in path.parts:
        return "workflows"
    return _EXTENSION_DOMAIN_MAP.get(path.suffix.lower())


class JITEnvelopeSynthesizer:
    """Synthesizes lean, two-tier prompt envelopes based on active worktree files."""

    CORE_INVARIANTS: Final[str] = (
        "# Tier 1: Universal Architectural Invariants (Kernel)\n"
        "- High Reliability & Quality First: Defend against regressions with executable contracts.\n"
        "- Zero-Trust Egress: Never output private RFC 1918 IPs (use RFC 5737 / example.com).\n"
        "- Complexity & Nesting: Strictly enforce cyclomatic complexity <= 10 and depth <= 5.\n"
        "- Clean Solutions: Ruthlessly eliminate zombie code, vestigial shims, and backwards hacks.\n"
        "- Grounded Tasks: Ground all WIP actions in GitHub Projects and issue tracking.\n"
    )

    DOMAIN_OVERLAYS: Final[dict[str, str]] = {
        "python": (
            "## Domain: Python & Pytest Contracts\n"
            "- Structural Tuple Equality: Consolidate linear asserts into tuple checks `assert (a, b) == (x, y)`.\n"
            "- AST Invariant Sentinel: Run `examples/ast-invariant-sentinel/sentinel.py` before push.\n"
            "- Headroom Elevation: Optimize functions to M <= 6 and depth <= 3.\n"
        ),
        "docs": (
            "## Domain: Documentation & Markdown\n"
            "- CommonMark Linebreak Hygiene (DOC012): Use proper markdown paragraphs without spurious hard breaks.\n"
            "- KaTeX Math Hygiene (DOC013): No unescaped ampersands in LaTeX formulas.\n"
            "- Mermaid Diagrams: Quote node labels containing brackets/parens; keep aspect ratio 1:3 <= H/W <= 3:1.\n"
        ),
        "polyglot": (
            "## Domain: Polyglot Systems (Rust, Go, C++)\n"
            "- Rust: Enforce affine types and zero-sized marker invariants at compile time.\n"
            "- Go: Guard goroutines with context cancellation; prevent unbuffered channel deadlocks.\n"
            "- C++: Enforce RAII ownership (CPP001) and Rule of Five completeness (CPP003).\n"
        ),
        "workflows": (
            "## Domain: GitHub Actions & CI\n"
            "- Gated CI Mandate: Ensure all quality checks run in dedicated workflow jobs.\n"
            "- Rate Management: Route GitHub queries through rate-limited interfaces; avoid raw API bursts.\n"
        ),
    }

    def resolve_domains(self, paths: Sequence[str]) -> set[str]:
        """Detect relevant domain overlays from target file paths."""
        classified = (_classify_domain(Path(p)) for p in paths)
        return {c for c in classified if c is not None}

    def synthesize(self, paths: Sequence[str]) -> JITPromptEnvelope:
        """Synthesize a lean two-tier instruction envelope for the specified targets."""
        domains = sorted(self.resolve_domains(paths))
        tier1_text = self.CORE_INVARIANTS
        tier1_tokens = estimate_tokens(tier1_text)

        overlay_blocks = [self.DOMAIN_OVERLAYS[d] for d in domains if d in self.DOMAIN_OVERLAYS]
        tier2_text = "\n".join(overlay_blocks)
        tier2_tokens = estimate_tokens(tier2_text)

        full_prompt = f"{tier1_text}\n{tier2_text}".strip()
        total_tokens = tier1_tokens + tier2_tokens

        return JITPromptEnvelope(
            tier1_tokens=tier1_tokens,
            tier2_tokens=tier2_tokens,
            total_tokens=total_tokens,
            domains=tuple(domains),
            prompt_text=full_prompt,
        )


def _is_critical_section(heading_lower: str) -> bool:
    """Check if section heading represents a critical architectural invariant."""
    return "architectural invariants" in heading_lower or "zero-trust" in heading_lower


class AblationAuditor:
    """Evaluates instruction sections for counterfactual pruning candidates."""

    def __init__(self, sections: Sequence[InstructionSection]) -> None:
        self.sections: Final[tuple[InstructionSection, ...]] = tuple(sections)

    def evaluate_candidates(self) -> list[AblationCandidate]:
        """Identify sections eligible for condensation or mechanical ablation."""
        return [self._score_section(sec) for sec in self.sections]

    def _score_section(self, sec: InstructionSection) -> AblationCandidate:
        """Score an individual section's ablation eligibility."""
        has_gate = any(code in sec.rule_codes for code, _, _ in KNOWN_GATES)
        heading_lower = sec.heading.lower()

        if _is_critical_section(heading_lower):
            return AblationCandidate(
                heading=sec.heading,
                token_savings=sec.token_count,
                mechanically_covered=has_gate,
                priority=RetentionPriority.CRITICAL,
                recommendation="Retain in Tier 1 Core Envelope.",
            )

        if has_gate and sec.token_count > 500:
            return AblationCandidate(
                heading=sec.heading,
                token_savings=sec.token_count - 100,
                mechanically_covered=True,
                priority=RetentionPriority.ABLATION_CANDIDATE,
                recommendation="Condense to 1-line invariant reference; gate is fully mechanical.",
            )

        if sec.token_count > 1000:
            return AblationCandidate(
                heading=sec.heading,
                token_savings=sec.token_count // 2,
                mechanically_covered=has_gate,
                priority=RetentionPriority.MEDIUM,
                recommendation="Split into Tier 2 domain overlay or compact verbose examples.",
            )

        return AblationCandidate(
            heading=sec.heading,
            token_savings=0,
            mechanically_covered=has_gate,
            priority=RetentionPriority.HIGH,
            recommendation="Retain within domain context.",
        )


def _render_audit_text(path: Path, sections: list[InstructionSection], total_tokens: int) -> None:
    """Render human-readable text audit report."""
    print(f"Instruction Audit for {path}:")
    print(f"Total Sections: {len(sections)} | Total Estimated Tokens: {total_tokens}")
    for sec in sections[:15]:
        print(f"  [{sec.level}] {sec.heading} ({sec.token_count} tokens)")
    if len(sections) > 15:
        print(f"  ... and {len(sections) - 15} more sections.")


def _cmd_audit(args: argparse.Namespace) -> int:
    """Execute the audit subcommand."""
    path = Path(args.path)
    if not path.exists():
        sys.stderr.write(f"File not found: {path}\n")
        return 1
    content = path.read_text(encoding="utf-8")
    sections = parse_instruction_sections(content)
    total_tokens = sum(s.token_count for s in sections)

    if args.json:
        report = {
            "file": str(path),
            "total_tokens": total_tokens,
            "section_count": len(sections),
            "sections": [s.to_dict() for s in sections],
        }
        print(json.dumps(report, indent=2))
        return 0

    _render_audit_text(path, sections, total_tokens)
    return 0


def _render_attribute_text(attrs: list[RuleAttribution]) -> None:
    """Render human-readable rule attribution report."""
    print("Rule Attribution Matrix:")
    for a in attrs:
        status = "MECHANICAL" if a.is_mechanically_enforced else "PROSE-ONLY"
        print(f"  [{a.gate_code}] {a.rule_name} -> '{a.section_heading}' ({status} via {a.gate_tool})")


def _cmd_attribute(args: argparse.Namespace) -> int:
    """Execute the attribute subcommand."""
    path = Path(args.path)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    sections = parse_instruction_sections(content) if content else []
    matrix = RuleAttributionMatrix(sections)
    attrs = matrix.build_attributions()

    if args.json:
        print(json.dumps([a.to_dict() for a in attrs], indent=2))
        return 0

    _render_attribute_text(attrs)
    return 0


def _cmd_synthesize(args: argparse.Namespace) -> int:
    """Execute the synthesize subcommand."""
    synthesizer = JITEnvelopeSynthesizer()
    envelope = synthesizer.synthesize(args.targets)

    if args.json:
        print(json.dumps(envelope.to_dict(), indent=2))
        return 0

    print(envelope.prompt_text)
    return 0


def _render_ablate_text(path: Path, candidates: list[AblationCandidate]) -> None:
    """Render human-readable ablation candidates report."""
    print(f"Ablation Candidates for {path}:")
    for c in candidates:
        if c.priority == RetentionPriority.ABLATION_CANDIDATE:
            print(f"  [ABLATE] {c.heading}: Save ~{c.token_savings} tokens. {c.recommendation}")


def _cmd_ablate(args: argparse.Namespace) -> int:
    """Execute the ablate subcommand."""
    path = Path(args.path)
    if not path.exists():
        sys.stderr.write(f"File not found: {path}\n")
        return 1
    content = path.read_text(encoding="utf-8")
    sections = parse_instruction_sections(content)
    auditor = AblationAuditor(sections)
    candidates = auditor.evaluate_candidates()

    if args.json:
        print(json.dumps([c.to_dict() for c in candidates], indent=2))
        return 0

    _render_ablate_text(path, candidates)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser."""
    parser = argparse.ArgumentParser(description="JIT Instruction Governor and Ablation Engine")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    audit_parser = subparsers.add_parser("audit", help="Audit instruction file token weights")
    audit_parser.add_argument("--path", default="AGENTS.md", help="Path to instruction markdown")
    audit_parser.add_argument("--json", action="store_true", help="Output JSON format")

    attr_parser = subparsers.add_parser("attribute", help="Map mechanical gates to rule sections")
    attr_parser.add_argument("--path", default="AGENTS.md", help="Path to instruction markdown")
    attr_parser.add_argument("--json", action="store_true", help="Output JSON format")

    synth_parser = subparsers.add_parser("synthesize", help="Synthesize JIT prompt for targets")
    synth_parser.add_argument("targets", nargs="+", help="Target worktree file paths")
    synth_parser.add_argument("--json", action="store_true", help="Output envelope metadata as JSON")

    ablate_parser = subparsers.add_parser("ablate", help="Evaluate counterfactual ablation candidates")
    ablate_parser.add_argument("--path", default="AGENTS.md", help="Path to instruction markdown")
    ablate_parser.add_argument("--json", action="store_true", help="Output JSON format")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for instruction governor CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        "audit": _cmd_audit,
        "attribute": _cmd_attribute,
        "synthesize": _cmd_synthesize,
        "ablate": _cmd_ablate,
    }
    handler = dispatch.get(args.subcommand)
    if not handler:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())

