#!/usr/bin/env python3
"""Deprecation Lifecycle Sentinel: mechanical enforcement of post-1.0 removal contracts.

Before 1.0, deletion is free: this repository mandates zero legacy shims and removes
obsolete code on sight. After 1.0 that same reflex breaks downstream callers, and the
opposite reflex — never delete anything — accumulates a permanent maintenance tax that
no one ever pays down.

The resolution is neither reflex but a contract with a deadline. Every deprecation
declares when it started, what replaces it, and the version that removes it. This
sentinel enforces that contract mechanically:

1. A deprecation without a removal version is a promise no one made.
2. A deprecation without a replacement is an instruction to guess.
3. A deprecation that outlives its removal version is debt wearing a warning label.
4. A removal scheduled outside a major bump violates the SemVer contract callers rely on.
5. A deprecation the runtime never announces is a trap: callers learn at deletion.
6. A deprecated symbol still called internally means the owner has not migrated either.
"""

from __future__ import annotations

import argparse
import ast
import functools
import re
import sys
import warnings
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
DEPRECATION_DECORATOR_NAMES = frozenset({"deprecated"})
REQUIRED_DEPRECATION_FIELDS = ("since", "remove_in", "replacement")


class DeprecationDefect(StrEnum):
    """The closed set of deprecation contract violations."""

    MISSING_METADATA = "MissingMetadata"
    OVERDUE_REMOVAL = "OverdueRemoval"
    NON_MAJOR_REMOVAL = "NonMajorRemoval"
    SILENT_DEPRECATION = "SilentDeprecation"
    UNMIGRATED_CALL_SITE = "UnmigratedCallSite"


@dataclass(frozen=True)
class Violation:
    """A breach of the deprecation lifecycle contract."""

    defect: DeprecationDefect
    file_path: str
    line_number: int
    symbol: str
    message: str


@dataclass(frozen=True)
class DeprecationRecord:
    """A declared deprecation and the contract it carries."""

    symbol: str
    file_path: str
    line_number: int
    since: str = ""
    remove_in: str = ""
    replacement: str = ""
    emits_warning: bool = False

    @property
    def metadata_gaps(self) -> tuple[str, ...]:
        """Return the required contract fields this deprecation leaves unspecified."""
        return tuple(name for name in REQUIRED_DEPRECATION_FIELDS if not getattr(self, name))


@dataclass
class DeprecationReport:
    """Inventory of every declared deprecation and every contract breach."""

    current_version: str
    records: list[DeprecationRecord] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """Return True when every deprecation honours the lifecycle contract."""
        return not self.violations

    def due_before(self, version: str) -> list[DeprecationRecord]:
        """Return deprecations scheduled for removal at or before the given version."""
        target = parse_semver(version)
        return sorted(
            (r for r in self.records if r.remove_in and parse_semver(r.remove_in) <= target),
            key=lambda r: parse_semver(r.remove_in),
        )


def parse_semver(version: str) -> tuple[int, int, int]:
    """Parse a SemVer string into a comparable tuple, tolerating a leading 'v'."""
    match = SEMVER_RE.match(version.lstrip("v").strip())
    if not match:
        raise ValueError(f"Not a SemVer version: {version!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def deprecated(
    since: str, remove_in: str, replacement: str
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a callable deprecated, announcing the contract at every call.

    The warning is the half callers actually experience; the metadata is the half the
    sentinel enforces. Both are required, which is why they are declared in one place.
    """
    for name, value in zip(
        REQUIRED_DEPRECATION_FIELDS, (since, remove_in, replacement), strict=True
    ):
        if not value:
            raise ValueError(f"@deprecated requires '{name}'")
    parse_semver(since), parse_semver(remove_in)

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            warnings.warn(
                f"{func.__qualname__} is deprecated since {since} and will be removed in "
                f"{remove_in}. Use {replacement} instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            return func(*args, **kwargs)

        wrapper.__deprecation__ = {  # type: ignore[attr-defined]
            "since": since,
            "remove_in": remove_in,
            "replacement": replacement,
        }
        return wrapper

    return decorate


def _decorator_name(node: ast.expr) -> str:
    """Return the callable name of a decorator expression, however it is spelled."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    return target.id if isinstance(target, ast.Name) else ""


def _keyword_strings(call: ast.Call) -> dict[str, str]:
    """Extract literal string keyword arguments from a decorator call."""
    return {
        kw.arg: kw.value.value
        for kw in call.keywords
        if kw.arg and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
    }


def _emits_deprecation_warning(node: ast.AST) -> bool:
    """Detect whether a function body raises a DeprecationWarning at runtime."""
    names = (
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and child.id == "DeprecationWarning"
    )
    return any(names)


def _deprecation_decorator(node: ast.AST) -> ast.expr | None:
    """Return the deprecation decorator applied to a definition, if any."""
    decorators = getattr(node, "decorator_list", [])
    matches = (d for d in decorators if _decorator_name(d) in DEPRECATION_DECORATOR_NAMES)
    return next(matches, None)


def _record_for(node: ast.AST, decorator: ast.expr, file_path: Path) -> DeprecationRecord:
    """Build a deprecation record from a decorated definition."""
    fields = _keyword_strings(decorator) if isinstance(decorator, ast.Call) else {}
    return DeprecationRecord(
        symbol=getattr(node, "name", "<unknown>"),
        file_path=str(file_path),
        line_number=getattr(node, "lineno", 1),
        since=fields.get("since", ""),
        remove_in=fields.get("remove_in", ""),
        replacement=fields.get("replacement", ""),
        emits_warning=_emits_deprecation_warning(node) or isinstance(decorator, ast.Call),
    )


def _definitions(tree: ast.AST) -> Iterator[ast.AST]:
    """Yield every function, method, and class definition in a module."""
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return (node for node in ast.walk(tree) if isinstance(node, kinds))


def collect_deprecations(file_path: Path) -> list[DeprecationRecord]:
    """Collect every declared deprecation in a Python module."""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    decorated = ((node, _deprecation_decorator(node)) for node in _definitions(tree))
    return [_record_for(node, dec, file_path) for node, dec in decorated if dec is not None]


def _called_names(file_path: Path) -> set[str]:
    """Return every simple name invoked as a call within a module."""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    calls = (node.func for node in ast.walk(tree) if isinstance(node, ast.Call))
    return {func.id for func in calls if isinstance(func, ast.Name)}


def _check_metadata(record: DeprecationRecord) -> Violation | None:
    """A deprecation missing its contract fields cannot be planned around."""
    gaps = record.metadata_gaps
    if not gaps:
        return None
    return Violation(
        defect=DeprecationDefect.MISSING_METADATA,
        file_path=record.file_path,
        line_number=record.line_number,
        symbol=record.symbol,
        message=(
            f"Deprecation of '{record.symbol}' omits {', '.join(gaps)}. A deprecation without a "
            "removal version is a promise no one made, and one without a replacement asks callers to guess."
        ),
    )


def _check_overdue(record: DeprecationRecord, current: tuple[int, int, int]) -> Violation | None:
    """A deprecation that outlived its own deadline is debt wearing a warning label."""
    if not record.remove_in or parse_semver(record.remove_in) > current:
        return None
    return Violation(
        defect=DeprecationDefect.OVERDUE_REMOVAL,
        file_path=record.file_path,
        line_number=record.line_number,
        symbol=record.symbol,
        message=(
            f"'{record.symbol}' was scheduled for removal in {record.remove_in}; the current version "
            "has reached it. Delete the symbol and its shim, or move the deadline deliberately."
        ),
    )


def _check_major_boundary(record: DeprecationRecord) -> Violation | None:
    """Post-1.0, a breaking removal belongs to a major bump and nowhere else."""
    if not (record.since and record.remove_in):
        return None
    since, remove_in = parse_semver(record.since), parse_semver(record.remove_in)
    if since[0] == 0 or remove_in[0] > since[0]:
        return None
    return Violation(
        defect=DeprecationDefect.NON_MAJOR_REMOVAL,
        file_path=record.file_path,
        line_number=record.line_number,
        symbol=record.symbol,
        message=(
            f"'{record.symbol}' is deprecated in {record.since} but scheduled for removal in "
            f"{record.remove_in}, within the same major. SemVer promises callers that no minor or "
            "patch release removes what they depend on."
        ),
    )


def _check_runtime_signal(record: DeprecationRecord) -> Violation | None:
    """A deprecation the runtime never announces is discovered at deletion."""
    if record.emits_warning:
        return None
    return Violation(
        defect=DeprecationDefect.SILENT_DEPRECATION,
        file_path=record.file_path,
        line_number=record.line_number,
        symbol=record.symbol,
        message=(
            f"'{record.symbol}' is marked deprecated but emits no DeprecationWarning. Callers who "
            "never read the changelog learn about it when their build breaks."
        ),
    )


_RECORD_CHECKS: tuple[Callable[[DeprecationRecord], Violation | None], ...] = (
    _check_metadata,
    _check_major_boundary,
    _check_runtime_signal,
)


def audit_record(record: DeprecationRecord, current_version: str) -> list[Violation]:
    """Apply every contract check to a single declared deprecation."""
    findings = [check(record) for check in _RECORD_CHECKS]
    findings.append(_check_overdue(record, parse_semver(current_version)))
    return [violation for violation in findings if violation is not None]


def _unmigrated_call_sites(
    records: Sequence[DeprecationRecord], files: Sequence[Path]
) -> list[Violation]:
    """Report internal callers still using a symbol their own project deprecated."""
    deprecated_symbols = {record.symbol: record for record in records}
    declared_in = {record.symbol: record.file_path for record in records}
    violations: list[Violation] = []
    for file_path in files:
        stale = _called_names(file_path) & set(deprecated_symbols)
        violations.extend(
            _call_site_violation(deprecated_symbols[symbol], file_path)
            for symbol in sorted(stale)
            if declared_in[symbol] != str(file_path)
        )
    return violations


def _call_site_violation(record: DeprecationRecord, caller: Path) -> Violation:
    """Build a violation for an internal call site that has not migrated."""
    return Violation(
        defect=DeprecationDefect.UNMIGRATED_CALL_SITE,
        file_path=str(caller),
        line_number=1,
        symbol=record.symbol,
        message=(
            f"'{record.symbol}' is deprecated (removal in {record.remove_in or 'unscheduled'}) but "
            f"still called here. Migrate to {record.replacement or 'the replacement'} before asking "
            "downstream callers to."
        ),
    )


def _python_files(paths: Sequence[Path]) -> list[Path]:
    """Expand file and directory targets into a sorted list of Python modules."""
    expanded = (
        [path] if path.is_file() and path.suffix == ".py" else sorted(path.rglob("*.py"))
        for path in paths
        if path.exists()
    )
    return [module for group in expanded for module in group]


def audit_paths(paths: Sequence[Path], current_version: str) -> DeprecationReport:
    """Audit every supplied target for deprecation lifecycle contract breaches."""
    report = DeprecationReport(current_version=current_version)
    report.violations.extend(
        Violation(
            defect=DeprecationDefect.MISSING_METADATA,
            file_path=str(path),
            line_number=1,
            symbol="<target>",
            message="Audit target does not exist; the gate would otherwise certify nothing as clean.",
        )
        for path in paths
        if not path.exists()
    )
    files = _python_files(paths)
    for file_path in files:
        report.records.extend(collect_deprecations(file_path))
    for record in report.records:
        report.violations.extend(audit_record(record, current_version))
    report.violations.extend(_unmigrated_call_sites(report.records, files))
    return report


def render_inventory(report: DeprecationReport, horizon: str | None) -> list[str]:
    """Render the deprecation inventory ordered by removal deadline."""
    if not report.records:
        return ["No declared deprecations."]
    scheduled = report.due_before(horizon) if horizon else report.records
    header = f"Deprecation inventory ({len(report.records)} declared, current {report.current_version}):"
    return [header] + [
        f"  {record.remove_in or 'unscheduled':>10}  {record.symbol}"
        f"  ->  {record.replacement or '<none>'}  ({record.file_path}:{record.line_number})"
        for record in scheduled
    ]


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser for the deprecation lifecycle sentinel."""
    parser = argparse.ArgumentParser(description="Deprecation Lifecycle Sentinel")
    parser.add_argument("paths", nargs="*", default=[], help="File or directory paths to audit")
    parser.add_argument(
        "--current-version",
        required=True,
        help="The version being released; deprecations due at or before it must be removed",
    )
    parser.add_argument("--due-before", help="List deprecations scheduled at or before this version")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI runner for the deprecation lifecycle sentinel."""
    args = build_arg_parser().parse_args(list(argv[1:]) if argv is not None else None)
    targets = [Path(raw) for raw in args.paths] or [Path(".")]
    report = audit_paths(targets, args.current_version)

    print("\n".join(render_inventory(report, args.due_before)))
    if report.is_clean:
        print(f"\n✅ Deprecation contracts honoured across {len(report.records)} declaration(s).")
        return 0

    print(f"\n❌ Found {len(report.violations)} deprecation contract violation(s):")
    for violation in report.violations:
        print(f"  [{violation.defect.value}] {violation.file_path}:{violation.line_number} — {violation.message}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
