#!/usr/bin/env python3
"""C++ RAII & Lifetime Invariant Sentinel.

Provides automated inspection of C++ headers and source files to enforce modern RAII,
ownership semantics, and lifetime safety invariants (C++ Core Guidelines, Rule of Five).
Detects manual memory management (delete/free), dangling views (std::string_view),
unmanaged raw pointers, missing special member functions, and use-after-move hazards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

SENTINEL_VERSION: Final[str] = "v1.0.0"

CPP_EXTENSIONS: Final[tuple[str, ...]] = (
    ".cpp",
    ".cc",
    ".cxx",
    ".c++",
    ".h",
    ".hpp",
    ".hxx",
    ".h++",
)

SARIF_RULES: Final[dict[str, dict[str, Any]]] = {
    "CPP001": {
        "id": "CPP001",
        "name": "ManualMemoryManagement",
        "shortDescription": {"text": "Manual deallocation via delete or free violates RAII discipline."},
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "CPP002": {
        "id": "CPP002",
        "name": "DanglingViewLifetime",
        "shortDescription": {
            "text": "Returning non-owning view over local temporary causes dangling reference."
        },
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "CPP003": {
        "id": "CPP003",
        "name": "RuleOfFiveIncomplete",
        "shortDescription": {
            "text": "Class with custom destructor fails to define or delete copy/move operations."
        },
        "defaultConfiguration": {"level": "warning"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "CPP004": {
        "id": "CPP004",
        "name": "UnmanagedRawPointer",
        "shortDescription": {
            "text": "Raw pointer member or allocation lacks explicit smart pointer ownership."
        },
        "defaultConfiguration": {"level": "warning"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "CPP005": {
        "id": "CPP005",
        "name": "UseAfterMove",
        "shortDescription": {
            "text": "Access or method call on moved-from identifier constitutes use-after-move."
        },
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
}


class CppSeverity(StrEnum):
    """Severity classification for C++ lifetime findings."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SentinelPreset(StrEnum):
    """Audit preset defining enforcement threshold."""

    STRICT = "strict"
    STANDARD = "standard"
    RELAXED = "relaxed"


@dataclass(frozen=True)
class CppFinding:
    """Individual C++ lifetime or RAII contract violation."""

    rule_id: str
    severity: CppSeverity
    message: str
    location: str
    line_number: int
    recommendation: str
    finding_hash: str


@dataclass(frozen=True)
class CppAuditSummary:
    """Comprehensive summary of C++ lifetime inspection."""

    total_files: int
    total_lines: int
    total_violations: int
    is_compliant: bool
    findings: tuple[CppFinding, ...]


def _compute_hash(content: str) -> str:
    """Return SHA-256 digest of finding signature."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def strip_cpp_comments(source: str) -> str:
    """Strip C and C++ comments while preserving line breaks."""

    def _replacer(match: re.Match[str]) -> str:
        s = match.group(0)
        if s.startswith("/"):
            return "\n" * s.count("\n")
        return s

    pattern = re.compile(
        r'//.*?$|/\*.*?\*/|\'(?:\\.|[^\\\'])*\'|"(?:\\.|[^\\"])*"',
        re.DOTALL | re.MULTILINE,
    )
    return re.sub(pattern, _replacer, source)


def _check_cpp001_line(line: str, line_no: int, filename: str) -> CppFinding | None:
    """Check line for direct calls to delete, delete[], free, or malloc."""
    pattern = re.compile(r"\b(delete\s*\[\s*\]|delete\b|free\s*\(|malloc\s*\(|calloc\s*\(|realloc\s*\()")
    m = pattern.search(line)
    if not m:
        return None
    token = m.group(1).split()[0].split("(")[0]
    loc = f"{filename}:{line_no}"
    h = _compute_hash(f"CPP001:{loc}:{token}")
    return CppFinding(
        rule_id="CPP001",
        severity=CppSeverity.HIGH,
        message=f"Manual memory deallocation '{token}' violates RAII invariant",
        location=loc,
        line_number=line_no,
        recommendation="Replace manual allocation with RAII container (std::vector, std::string) or std::make_unique<T>()",
        finding_hash=h,
    )


def _brace_delta(char: str) -> int:
    """Return stack depth delta for brace character."""
    if char == "{":
        return 1
    if char == "}":
        return -1
    return 0


def _find_matching_brace(source: str, open_brace_idx: int) -> int:
    """Find index of matching closing brace, accounting for nested braces."""
    depth = 0
    for idx in range(open_brace_idx, len(source)):
        depth += _brace_delta(source[idx])
        if depth == 0:
            return idx
    return -1


def _extract_view_functions(source: str) -> list[tuple[str, str, str, int]]:
    """Extract view-returning function (return_type, func_name, body, start_line) tuples."""
    results: list[tuple[str, str, str, int]] = []
    pattern = re.compile(r"\b(std::(?:string_view|span<[^>]+>))\s+([A-Za-z0-9_:]+)\s*\([^)]*\)\s*\{")
    for m in pattern.finditer(source):
        close_idx = _find_matching_brace(source, m.end() - 1)
        if close_idx == -1:
            continue
        body = source[m.end() : close_idx]
        start_line = source[: m.start()].count("\n") + 1
        results.append((m.group(1), m.group(2), body, start_line))
    return results


def _inspect_dangling_func_return(
    func_meta: tuple[str, str, int], body: str, filename: str
) -> list[CppFinding]:
    """Inspect return statements inside a single view-returning function."""
    ret_type, func_name, start_line = func_meta
    ret_pattern = re.compile(r"\breturn\s+([A-Za-z0-9_]+)\s*;")
    findings: list[CppFinding] = []
    for ret_m in ret_pattern.finditer(body):
        ret_var = ret_m.group(1)
        decl_pattern = re.compile(rf"\bstd::string\s+{ret_var}\b")
        if not decl_pattern.search(body):
            continue
        line_no = start_line + body[: ret_m.start()].count("\n")
        loc = f"{filename}:{line_no}"
        h = _compute_hash(f"CPP002:{loc}:{func_name}")
        findings.append(
            CppFinding(
                rule_id="CPP002",
                severity=CppSeverity.CRITICAL,
                message=f"Function '{func_name}' returns '{ret_type}' over local temporary '{ret_var}'",
                location=loc,
                line_number=line_no,
                recommendation="Return std::string by value or annotate parameter with [[clang::lifetimebound]]",
                finding_hash=h,
            )
        )
    return findings


def _check_cpp002_dangling_views(source: str, filename: str) -> list[CppFinding]:
    """Detect functions returning std::string_view or std::span over local temporaries."""
    findings: list[CppFinding] = []
    for ret_type, func_name, body, start_line in _extract_view_functions(source):
        func_meta = (ret_type, func_name, start_line)
        findings.extend(_inspect_dangling_func_return(func_meta, body, filename))
    return findings


def _check_rule_of_five_class(class_name: str, body: str, line_no: int, filename: str) -> CppFinding | None:
    """Audit class body for Rule of Five completeness when custom destructor is present."""
    has_dtor = bool(re.search(rf"~{class_name}\s*\(", body))
    if not has_dtor:
        return None

    has_copy_ctor = bool(re.search(rf"\b{class_name}\s*\(\s*const\s+{class_name}\s*&", body))
    has_move_ctor = bool(re.search(rf"\b{class_name}\s*\(\s*{class_name}\s*&&", body))
    has_copy_assign = bool(re.search(rf"\boperator\s*=\s*\(\s*const\s+{class_name}\s*&", body))
    has_move_assign = bool(re.search(rf"\boperator\s*=\s*\(\s*{class_name}\s*&&", body))

    if has_copy_ctor and has_move_ctor and has_copy_assign and has_move_assign:
        return None

    loc = f"{filename}:{line_no}"
    h = _compute_hash(f"CPP003:{loc}:{class_name}")
    return CppFinding(
        rule_id="CPP003",
        severity=CppSeverity.MEDIUM,
        message=f"Class '{class_name}' defines custom destructor but violates Rule of Five",
        location=loc,
        line_number=line_no,
        recommendation="Explicitly define or = delete copy/move constructors and assignment operators",
        finding_hash=h,
    )


def _extract_class_blocks(source: str) -> list[tuple[str, str, int]]:
    """Extract (class_name, body, start_line) tuples from source text."""
    blocks: list[tuple[str, str, int]] = []
    class_decl = re.compile(r"\b(?:class|struct)\s+([A-Za-z0-9_]+)\b[^{]*\{")
    for match in class_decl.finditer(source):
        close_idx = _find_matching_brace(source, match.end() - 1)
        if close_idx == -1:
            continue
        body = source[match.end() : close_idx]
        line_no = source[: match.start()].count("\n") + 1
        blocks.append((match.group(1), body, line_no))
    return blocks


def _check_cpp003_rule_of_five(source: str, filename: str) -> list[CppFinding]:
    """Detect classes violating the Rule of Five."""
    findings: list[CppFinding] = []
    for class_name, body, line_no in _extract_class_blocks(source):
        finding = _check_rule_of_five_class(class_name, body, line_no, filename)
        if finding is not None:
            findings.append(finding)
    return findings


def _check_cpp004_line(line: str, line_no: int, filename: str) -> CppFinding | None:
    """Check line for raw pointer member variables or raw new allocations."""
    line_s = line.strip()
    # Check raw pointer member variable pattern
    mem_pattern = re.compile(r"^\s*(?:[A-Za-z0-9_:]+)\s*\*\s*([A-Za-z0-9_]+_?)\s*;\s*$")
    # Check new allocation assigned to raw pointer
    new_pattern = re.compile(r"\b(?:[A-Za-z0-9_:]+)\s*\*\s*([A-Za-z0-9_]+)\s*=\s*new\s+")

    match = mem_pattern.search(line_s) or new_pattern.search(line_s)
    if not match:
        return None

    # Exclude C-style string constants
    if "char*" in line_s or "char *" in line_s:
        return None

    loc = f"{filename}:{line_no}"
    var_name = match.group(1)
    h = _compute_hash(f"CPP004:{loc}:{var_name}")
    return CppFinding(
        rule_id="CPP004",
        severity=CppSeverity.MEDIUM,
        message=f"Unmanaged raw pointer '{var_name}' lacks explicit smart pointer ownership",
        location=loc,
        line_number=line_no,
        recommendation="Wrap resource in std::unique_ptr<T> or std::shared_ptr<T>",
        finding_hash=h,
    )


def _check_moved_var_usage(
    raw_line: str, line_no: int, moved_info: tuple[str, int], filename: str
) -> CppFinding | None:
    """Check if variable is used after move."""
    var, moved_at = moved_info
    if line_no <= moved_at:
        return None
    use_pattern = re.compile(rf"\b{var}\b(?:\s*->|\s*\.)")
    if not use_pattern.search(raw_line):
        return None
    loc = f"{filename}:{line_no}"
    h = _compute_hash(f"CPP005:{loc}:{var}")
    return CppFinding(
        rule_id="CPP005",
        severity=CppSeverity.HIGH,
        message=f"Use-after-move hazard on variable '{var}' (moved at line {moved_at})",
        location=loc,
        line_number=line_no,
        recommendation="Avoid referencing moved-from object; reset or reassign variable before reuse",
        finding_hash=h,
    )


def _record_moves(raw_line: str, line_no: int, moved_vars: dict[str, int]) -> None:
    """Record variables passed to std::move in the given line."""
    for m in re.finditer(r"\bstd::move\s*\(\s*([A-Za-z0-9_]+)\s*\)", raw_line):
        moved_vars[m.group(1)] = line_no


def _drain_moved_var_findings(
    raw_line: str, line_no: int, moved_vars: dict[str, int], filename: str
) -> list[CppFinding]:
    """Check moved variables against line usage and pop any triggered variables."""
    findings: list[CppFinding] = []
    for var, moved_at in list(moved_vars.items()):
        finding = _check_moved_var_usage(raw_line, line_no, (var, moved_at), filename)
        if finding:
            findings.append(finding)
            moved_vars.pop(var, None)
    return findings


def _scan_block_for_use_after_move(lines: Sequence[str], start_line: int, filename: str) -> list[CppFinding]:
    """Scan a sequence of lines within a block for use-after-move violations."""
    findings: list[CppFinding] = []
    moved_vars: dict[str, int] = {}
    for idx, raw_line in enumerate(lines):
        line_no = start_line + idx
        _record_moves(raw_line, line_no, moved_vars)
        findings.extend(_drain_moved_var_findings(raw_line, line_no, moved_vars, filename))
    return findings


def _extract_function_blocks(source: str) -> list[tuple[str, int]]:
    """Extract (body, start_line) tuples for functions in source text."""
    blocks: list[tuple[str, int]] = []
    func_decl = re.compile(r"\b[A-Za-z0-9_:]+\s+[A-Za-z0-9_:]+\s*\([^)]*\)\s*\{")
    for match in func_decl.finditer(source):
        close_idx = _find_matching_brace(source, match.end() - 1)
        if close_idx == -1:
            continue
        body = source[match.end() : close_idx]
        start_line = source[: match.end()].count("\n") + 1
        blocks.append((body, start_line))
    return blocks


def _check_cpp005_use_after_move(source: str, filename: str) -> list[CppFinding]:
    """Detect use-after-move hazards within functions."""
    findings: list[CppFinding] = []
    for body, start_line in _extract_function_blocks(source):
        lines = body.splitlines()
        findings.extend(_scan_block_for_use_after_move(lines, start_line, filename))
    return findings


class CppLifetimeSentinel:
    """Audits C++ source code for lifetime, ownership, and RAII invariants."""

    def __init__(self, preset: SentinelPreset = SentinelPreset.STRICT) -> None:
        """Initialize sentinel with audit sensitivity preset."""
        self.preset = preset

    def _audit_line_invariants(self, line: str, idx: int, filename: str) -> list[CppFinding]:
        """Audit single line against per-line rules CPP001 and CPP004."""
        findings: list[CppFinding] = []
        f1 = _check_cpp001_line(line, idx, filename)
        if f1 is not None:
            findings.append(f1)

        if self.preset != SentinelPreset.RELAXED:
            f4 = _check_cpp004_line(line, idx, filename)
            if f4 is not None:
                findings.append(f4)
        return findings

    def audit_source(self, source: str, filename: str = "main.cpp") -> list[CppFinding]:
        """Audit single C++ source string for all lifetime rules."""
        findings: list[CppFinding] = []
        clean_source = strip_cpp_comments(source)
        lines = clean_source.splitlines()

        for idx, line in enumerate(lines, start=1):
            findings.extend(self._audit_line_invariants(line, idx, filename))

        findings.extend(_check_cpp002_dangling_views(clean_source, filename))
        findings.extend(_check_cpp003_rule_of_five(clean_source, filename))
        findings.extend(_check_cpp005_use_after_move(clean_source, filename))
        return findings

    def audit_string(self, source: str, filename: str = "main.cpp") -> CppAuditSummary:
        """Audit single C++ source string and compile summary."""
        findings = self.audit_source(source, filename)
        is_compliant = not any(f.severity in (CppSeverity.CRITICAL, CppSeverity.HIGH) for f in findings)
        return CppAuditSummary(
            total_files=1,
            total_lines=len(source.splitlines()),
            total_violations=len(findings),
            is_compliant=is_compliant,
            findings=tuple(findings),
        )

    def audit_files(self, paths: Sequence[Path]) -> CppAuditSummary:
        """Audit multiple C++ source files and compile summary."""
        all_findings: list[CppFinding] = []
        total_lines = 0
        inspected_count = 0

        for p in paths:
            if not p.is_file() or p.suffix.lower() not in CPP_EXTENSIONS:
                continue
            text = p.read_text(encoding="utf-8")
            total_lines += len(text.splitlines())
            inspected_count += 1
            all_findings.extend(self.audit_source(text, str(p)))

        is_compliant = not any(f.severity in (CppSeverity.CRITICAL, CppSeverity.HIGH) for f in all_findings)
        return CppAuditSummary(
            total_files=inspected_count,
            total_lines=total_lines,
            total_violations=len(all_findings),
            is_compliant=is_compliant,
            findings=tuple(all_findings),
        )


def export_sarif(summary: CppAuditSummary, target_uri: str) -> dict[str, Any]:
    """Export C++ lifetime findings as schema-valid OASIS SARIF 2.1.0 dictionary."""
    results: list[dict[str, Any]] = []
    for f in summary.findings:
        rule_def = SARIF_RULES.get(
            f.rule_id,
            {
                "id": f.rule_id,
                "name": "CppLifetimeFinding",
                "shortDescription": {"text": f.message},
                "defaultConfiguration": {"level": "warning"},
            },
        )
        results.append(
            {
                "ruleId": f.rule_id,
                "level": rule_def["defaultConfiguration"]["level"],
                "message": {"text": f.message},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f.location.split(":")[0]},
                            "region": {"startLine": max(1, f.line_number)},
                        }
                    }
                ],
                "properties": {
                    "severity": f.severity.value,
                    "recommendation": f.recommendation,
                    "findingHash": f.finding_hash,
                },
            }
        )

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "vibes-cpp-lifetime-sentinel",
                        "informationUri": "https://github.com/dan-petty/vibes",
                        "version": SENTINEL_VERSION,
                        "rules": list(SARIF_RULES.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def export_json(summary: CppAuditSummary) -> str:
    """Export audit summary and telemetry as JSON string."""
    data = {
        "sentinel_version": SENTINEL_VERSION,
        "total_files": summary.total_files,
        "total_lines": summary.total_lines,
        "total_violations": summary.total_violations,
        "is_compliant": summary.is_compliant,
        "findings": [
            {
                "rule_id": f.rule_id,
                "severity": f.severity.value,
                "message": f.message,
                "location": f.location,
                "line_number": f.line_number,
                "recommendation": f.recommendation,
            }
            for f in summary.findings
        ],
    }
    return json.dumps(data, indent=2)


def format_markdown_report(summary: CppAuditSummary) -> str:
    """Format human-readable Markdown summary report."""
    status = "PASSED" if summary.is_compliant else "FAILED"
    lines = [
        f"# C++ RAII & Lifetime Invariant Audit Report: {status}",
        "",
        f"- **Audited Files**: {summary.total_files}",
        f"- **Total Lines Analyzed**: {summary.total_lines}",
        f"- **Total Violations**: {summary.total_violations}",
        f"- **Compliance Gate**: `{'PASSED' if summary.is_compliant else 'FAILED'}`",
        "",
    ]
    if not summary.findings:
        lines.append("✓ **Zero lifetime or RAII hazards detected.** All C++ contracts strictly enforced.\n")
        return "\n".join(lines)

    lines.extend(
        [
            "## Lifetime & RAII Findings",
            "",
            "| Rule | Severity | Location | Finding | Recommendation |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]
    )
    for f in summary.findings:
        lines.append(
            f"| `{f.rule_id}` | **{f.severity.value.upper()}** | `{f.location}` | {f.message} | {f.recommendation} |"
        )
    lines.append("")
    return "\n".join(lines)


def _expand_path_target(p: Path) -> list[Path]:
    """Expand a single file or directory path into C++ target files."""
    if p.is_file() and p.suffix.lower() in CPP_EXTENSIONS:
        return [p]
    if p.is_dir():
        return [f for ext in CPP_EXTENSIONS for f in p.glob(f"**/*{ext}")]
    return []


def _collect_target_paths(targets: Sequence[str]) -> list[Path]:
    """Collect unique C++ file paths from directory or file arguments."""
    paths: list[Path] = []
    for t in targets:
        paths.extend(_expand_path_target(Path(t)))
    return sorted(set(paths))


def _handle_cli_exports(
    summary: CppAuditSummary,
    target_uri: str,
    export_paths: dict[str, str | None],
) -> None:
    """Write audit outputs to requested file formats."""
    sarif_p = export_paths.get("sarif")
    if sarif_p:
        sarif_data = export_sarif(summary, target_uri)
        Path(sarif_p).write_text(json.dumps(sarif_data, indent=2), encoding="utf-8")

    json_p = export_paths.get("json")
    if json_p:
        Path(json_p).write_text(export_json(summary), encoding="utf-8")

    md_p = export_paths.get("md")
    if md_p:
        Path(md_p).write_text(format_markdown_report(summary), encoding="utf-8")


def _build_argument_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="cpp_lifetime_sentinel.py",
        description="C++ RAII & Lifetime Invariant Sentinel.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        default=["."],
        help="C++ source files or directories to audit.",
    )
    parser.add_argument(
        "--preset",
        choices=[p.value for p in SentinelPreset],
        default=SentinelPreset.STRICT.value,
        help="Audit sensitivity preset (default: strict).",
    )
    parser.add_argument(
        "--export-sarif",
        metavar="PATH",
        help="Export findings as SARIF 2.1.0 to PATH.",
    )
    parser.add_argument(
        "--export-json",
        metavar="PATH",
        help="Export audit summary as JSON to PATH.",
    )
    parser.add_argument(
        "--export-md",
        metavar="PATH",
        help="Export Markdown report to PATH.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute main CLI routine."""
    parser = _build_argument_parser()
    args = parser.parse_args(argv)

    paths = _collect_target_paths(args.targets)
    if not paths:
        print("No C++ source files found for inspection.", file=sys.stderr)
        return 0

    preset = SentinelPreset(args.preset)
    sentinel = CppLifetimeSentinel(preset)
    summary = sentinel.audit_files(paths)

    _handle_cli_exports(
        summary,
        args.targets[0] if args.targets else ".",
        {
            "sarif": args.export_sarif,
            "json": args.export_json,
            "md": args.export_md,
        },
    )
    print(format_markdown_report(summary))
    return 0 if summary.is_compliant else 1


if __name__ == "__main__":
    sys.exit(main())
