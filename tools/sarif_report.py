#!/usr/bin/env python3
"""Emit every oracle's findings as one SARIF 2.1.0 log, validated against the OASIS schema.

This repository runs five independent oracles and each prints its own bespoke text into a
CI log. A log is where a finding goes to be ignored: nobody opens it unless the build is
already red, and a `note`-level observation never turns the build red. SARIF is how a
finding reaches the diff instead — GitHub code scanning renders it inline on the pull
request that introduced it.

Three decisions worth knowing:

* **The schema is the oracle.** SARIF is an OASIS standard with a published JSON schema,
  committed at `artifacts/schemas/sarif-schema-2.1.0.json`. Every log this module produces
  is validated against it before it is written, so "GitHub rejected our upload" is not a
  thing that can be discovered after a push.
* **`sarif-om` was assessed and rejected.** The obvious dependency scores 48.9 (Active) on
  this repository's own maturity model — last pushed April 2024 — and its contribution is
  constructors for dictionaries the schema already governs. What no library can supply is
  the mapping from *these* oracles' finding types, which is what this module actually is.
  `jsonschema` (91.4, Mature) is adopted for the part that is genuinely hard.
* **Fingerprints deliberately exclude the line number.** A finding that shifts because
  somebody added an import above it is the same finding, and GitHub uses
  `partialFingerprints` to decide whether to re-alert. Hashing the line would make every
  unrelated edit look like a new defect.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

SARIF_VERSION: Final[str] = "2.1.0"
SARIF_SCHEMA_URI: Final[str] = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json"
)
DEFAULT_SCHEMA_PATH: Final[Path] = Path("artifacts/schemas/sarif-schema-2.1.0.json")
DEFAULT_OUTPUT: Final[Path] = Path(".data/findings.sarif")
INFORMATION_URI: Final[str] = "https://github.com/dan-petty/vibes"

# SARIF defines exactly these three, plus "none". Anything else is rejected by the schema.
LEVELS: Final[frozenset[str]] = frozenset({"error", "warning", "note", "none"})


@dataclass(frozen=True)
class Rule:
    """One rule a tool can report, as code scanning will display it."""

    rule_id: str
    name: str
    short_description: str
    level: str = "warning"

    def to_sarif(self) -> dict[str, Any]:
        """Render as a SARIF reportingDescriptor."""
        return {
            "id": self.rule_id,
            "name": self.name,
            "shortDescription": {"text": self.short_description},
            "defaultConfiguration": {"level": self.level},
        }


@dataclass(frozen=True)
class Result:
    """One finding, normalized across the oracles that produce them."""

    rule_id: str
    level: str
    message: str
    file_path: str
    line: int = 1
    subject: str = ""

    def fingerprint(self, root: Path | None = None) -> str:
        """Return a stable identity that survives the finding moving down the file.

        The path is normalised to the same repository-relative URI the result itself
        carries. Hashing the raw filesystem path made the identity depend on where the
        checkout happened to live, so the same finding in the same file scanned as `.` and
        as an absolute path produced two different fingerprints — and a baseline recorded on
        a developer's machine suppressed nothing in CI, which is the one job a baseline has.
        """
        located = _relative_uri(self.file_path, root) if root is not None else self.file_path
        material = f"{self.rule_id}|{located}|{self.subject or self.message}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def to_sarif(self, root: Path) -> dict[str, Any]:
        """Render as a SARIF result, with a repository-relative location."""
        return {
            "ruleId": self.rule_id,
            "level": self.level,
            "message": {"text": self.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": _relative_uri(self.file_path, root)},
                        "region": {"startLine": max(1, self.line)},
                    }
                }
            ],
            "partialFingerprints": {"vibesFindingV1": self.fingerprint(root)},
        }


@dataclass
class Run:
    """One tool's findings, kept separate so code scanning attributes them correctly."""

    tool_name: str
    rules: list[Rule] = field(default_factory=list)
    results: list[Result] = field(default_factory=list)

    def to_sarif(self, root: Path) -> dict[str, Any]:
        """Render as a SARIF run, declaring only the rules that actually fired.

        Declaring the full rule catalogue would list rules with no results, which code
        scanning renders as dormant alerts nobody can act on.
        """
        fired = {result.rule_id for result in self.results}
        return {
            "tool": {
                "driver": {
                    "name": self.tool_name,
                    "informationUri": INFORMATION_URI,
                    "rules": [rule.to_sarif() for rule in self.rules if rule.rule_id in fired],
                }
            },
            "results": [result.to_sarif(root) for result in self.results],
        }


def _relative_uri(file_path: str, root: Path) -> str:
    """Return a repository-relative POSIX URI, which is what code scanning resolves against."""
    path = Path(file_path)
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        relative = path
    return relative.as_posix()


def build_log(runs: Sequence[Run], root: Path) -> dict[str, Any]:
    """Assemble the complete SARIF log, including runs that found nothing.

    A run with zero results is not noise to be filtered out — it is the only way an alert
    gets closed. Code scanning resolves a finding when the tool that raised it reports
    again without it; a tool that simply stops appearing leaves every alert it ever raised
    open forever. Dropping empty runs would have meant fixed defects staying red.
    """
    return {
        "$schema": SARIF_SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [run.to_sarif(root) for run in runs],
    }


def validate_log(log: dict[str, Any], schema_path: Path = DEFAULT_SCHEMA_PATH) -> list[str]:
    """Validate a log against the OASIS schema, returning human-readable errors.

    Returns an empty list when the log conforms. A malformed upload is rejected by GitHub
    with a message that names neither the field nor the run, so this check exists to fail
    at the point the defect was introduced rather than three minutes into a workflow.
    """
    import jsonschema

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(log), key=lambda e: list(e.absolute_path))
    ]


# --- Adapters -------------------------------------------------------------------------
#
# One per oracle. Each owns the mapping from its own finding type to the normalized
# Result, including the severity judgement, because only the oracle knows whether a given
# finding stops a release or merely informs one.


# The oracles live beside this tool, not inside whatever repository is being scanned.
# Resolving them against `--root` meant the sentinel vanished the moment the tool was
# pointed anywhere but its own checkout.
TOOLING_ROOT: Final[Path] = Path(__file__).resolve().parent.parent


def _load_example(root: Path, example: str, module: str) -> Any:
    """Import a module that lives in `examples/`, which is not a package.

    The examples are standalone demonstrations, deliberately runnable on their own, so
    they sit on no import path. Each adapter puts its own example directory on `sys.path`
    the same way the workbench does, rather than the examples being restructured to suit
    one consumer.
    """
    del root
    directory = (TOOLING_ROOT / "examples" / example).resolve()
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return __import__(module)


def _sentinel_level(invariant: str) -> str:
    """Every architectural invariant is gating, so every breach is an error."""
    del invariant
    return "error"


def sentinel_run(paths: Sequence[Path], root: Path) -> Run:
    """Collect AST invariant violations: complexity, nesting depth, and egress sanitization."""
    audit_file = _load_example(root, "ast-invariant-sentinel", "sentinel").audit_file

    rules: dict[str, Rule] = {}
    results: list[Result] = []
    for path in paths:
        for violation in audit_file(path):
            rule_id = f"vibes/invariant/{violation.invariant}"
            rules.setdefault(
                rule_id,
                Rule(
                    rule_id=rule_id,
                    name=violation.invariant,
                    short_description=f"Architectural invariant: {violation.invariant}",
                    level="error",
                ),
            )
            results.append(
                Result(
                    rule_id=rule_id,
                    level=_sentinel_level(violation.invariant),
                    message=violation.message,
                    file_path=violation.file_path,
                    line=violation.line_number,
                    subject=violation.message.split("(")[0].strip(),
                )
            )
    return Run("vibes-ast-invariant-sentinel", list(rules.values()), results)


def smell_run(paths: Sequence[Path], root: Path, include_advisory: bool = False) -> Run:
    """Collect quantified structural decay.

    Advisory smells are excluded by default. They are sound measurements whose response
    needs judgement — the workbench already refuses to put them in a backlog for that
    reason — and 89 of them arriving as code scanning alerts would bury the two findings
    that actually stop a release.
    """
    analyze = _load_example(root, "code-smell-quantifier", "smell_quantifier").analyze

    report = analyze(list(paths), include_advisory=include_advisory)
    gating = {id(finding) for finding in report.gating}
    rules: dict[str, Rule] = {}
    results: list[Result] = []
    for finding in report.findings:
        smell = getattr(finding.smell, "value", str(finding.smell))
        rule_id = f"vibes/smell/{smell}"
        is_gating = id(finding) in gating
        rules.setdefault(
            rule_id,
            Rule(
                rule_id=rule_id,
                name=smell,
                short_description=f"Structural decay: {smell}",
                level="error" if is_gating else "note",
            ),
        )
        results.append(
            Result(
                rule_id=rule_id,
                level="error" if is_gating else "note",
                message=f"{finding.subject}: {finding.detail}",
                file_path=finding.file_path,
                line=finding.line_number,
                subject=finding.subject,
            )
        )
    return Run("vibes-code-smell-quantifier", list(rules.values()), results)


def docs_run(paths: Sequence[Path], root: Path) -> Run:
    """Collect documentation defects: fences, diagrams, links, tables, structure, egress."""
    from docs_validator import DocsValidator

    report = DocsValidator().validate_directory(root)
    rules: dict[str, Rule] = {}
    results: list[Result] = []
    for finding in report.findings:
        rule_id = f"vibes/docs/{finding.category}"
        level = "error" if finding.severity == "error" else "warning"
        rules.setdefault(
            rule_id,
            Rule(
                rule_id=rule_id,
                name=finding.category,
                short_description=f"Documentation integrity: {finding.category}",
                level=level,
            ),
        )
        results.append(
            Result(
                rule_id=rule_id,
                level=level,
                message=finding.message,
                file_path=finding.file_path,
                line=finding.line_number,
                subject=finding.message[:80],
            )
        )
    del paths
    return Run("vibes-docs-validator", list(rules.values()), results)


_SUPPLY_LEVELS: Final[dict[str, str]] = {"high": "error", "medium": "warning", "low": "note"}


def supply_run(paths: Sequence[Path], root: Path) -> Run:
    """Collect supply-chain risk: drifted floors, mutable action refs, unpinned installs."""
    from supply_chain_audit import audit, build_inventory, enrich_versions
    from upstream_facts import load_snapshot

    inventory = build_inventory(root)
    inventory.requirements = enrich_versions(inventory.requirements, offline=True)
    rules: dict[str, Rule] = {}
    results: list[Result] = []
    for finding in audit(inventory, load_snapshot(root / "docs" / "landscape" / "snapshot.json")):
        rule_id = f"vibes/supply-chain/{finding.risk}"
        level = _SUPPLY_LEVELS.get(finding.severity, "warning")
        rules.setdefault(
            rule_id,
            Rule(
                rule_id=rule_id,
                name=finding.risk,
                short_description=f"Supply chain risk: {finding.risk}",
                level=level,
            ),
        )
        results.append(
            Result(
                rule_id=rule_id,
                level=level,
                message=f"{finding.subject}: {finding.detail}",
                file_path=finding.source,
                line=1,
                subject=finding.subject,
            )
        )
    del paths
    return Run("vibes-supply-chain-audit", list(rules.values()), results)


def fuzz_run(paths: Sequence[Path], root: Path, corpus_dir: Path | None = None) -> Run:
    """Collect regression-corpus failures: crashes, hash-seed divergence, non-convergent repairs.

    Replays the committed corpus rather than exploring. Exploration is a random search
    whose result depends on how long it was allowed to run, and a code scanning alert that
    appears and disappears with the seed is worse than none: it trains the reader to
    dismiss the category. The corpus is deterministic and only grows.

    The location is the instrument, not the input. A fixture in `artifacts/fuzz-corpus/`
    is working as intended; the file that needs editing is the parser it broke.
    """
    from fuzz_harness import TARGETS, Plan, replay

    corpus = corpus_dir or TOOLING_ROOT / "artifacts" / "fuzz-corpus"
    rules: dict[str, Rule] = {}
    results: list[Result] = []
    with tempfile.TemporaryDirectory(prefix="vibes-fuzz-sarif-") as scratch:
        findings = replay(list(TARGETS.values()), Path(scratch), Plan(corpus_dir=corpus)).failures
    for finding in findings:
        rule_id = f"vibes/fuzz/{finding.prop}"
        level = "error" if finding.severity == "error" else "warning"
        rules.setdefault(
            rule_id,
            Rule(
                rule_id=rule_id,
                name=finding.prop,
                short_description=f"Fuzzing property violated: {finding.prop}",
                level=level,
            ),
        )
        results.append(
            Result(
                rule_id=rule_id,
                level=level,
                message=f"{finding.target} on {finding.digest}: {finding.detail}",
                file_path=str(TOOLING_ROOT / TARGETS[finding.target].module),
                line=1,
                subject=f"{finding.target}/{finding.prop}",
            )
        )
    del paths, root
    return Run("vibes-instrument-fuzzer", list(rules.values()), results)


ADAPTERS: Final[dict[str, Any]] = {
    "sentinel": sentinel_run,
    "smells": smell_run,
    "docs": docs_run,
    "supply": supply_run,
    "fuzz": fuzz_run,
}


# --- CLI ------------------------------------------------------------------------------


def collect_runs(
    sources: Iterable[str], paths: Sequence[Path], root: Path, include_advisory: bool = False
) -> Iterator[Run]:
    """Run each requested adapter, letting one oracle's failure not silence the rest."""
    for name in sources:
        run = _run_adapter(name, paths, root, include_advisory)
        if run is not None:
            yield run


def _run_adapter(name: str, paths: Sequence[Path], root: Path, include_advisory: bool) -> Run | None:
    """Run one adapter, reporting rather than raising when an oracle is unavailable."""
    adapter = ADAPTERS[name]
    try:
        if name == "smells":
            return adapter(paths, root, include_advisory)
        return adapter(paths, root)
    except (OSError, ValueError, ImportError, SyntaxError) as err:
        print(f"⚠️  {name} adapter unavailable: {err}", file=sys.stderr)
        return None


def _source_paths(root: Path) -> list[Path]:
    """Return the repository's own Python sources, using the one shared corpus definition."""
    from source_tree_policy import iter_source_files

    return sorted(iter_source_files(root, (".py",)))


def _apply_baseline(
    runs: Sequence[Run], baseline_path: Path | None, root: Path | None = None
) -> tuple[list[Run], int]:
    """Drop findings a committed baseline already accepts, keeping every run.

    A run emptied by the baseline is still published. Code scanning resolves an alert only
    when the tool that raised it reports again without it, so a run that disappears because
    everything in it was accepted would leave its old alerts open forever — the same reason
    `build_log` never filters empty runs.
    """
    if baseline_path is None:
        return list(runs), 0
    from finding_baseline import load_baseline, partition

    baseline = load_baseline(baseline_path)
    filtered: list[Run] = []
    suppressed = 0
    for run in runs:
        split = partition(run.results, baseline, root)
        suppressed += len(split.known)
        filtered.append(Run(run.tool_name, run.rules, split.new))
    return filtered, suppressed


def _report_schema_errors(errors: Sequence[str]) -> bool:
    """Print the first few schema errors and report whether any existed."""
    if not errors:
        return False
    print(f"SARIF log failed schema validation ({len(errors)} error(s)):", file=sys.stderr)
    for message in errors[:10]:
        print(f"  {message}", file=sys.stderr)
    return True


def _record_baseline_if_requested(args: argparse.Namespace, runs: Sequence[Run]) -> None:
    """Save baseline findings if requested by CLI arguments."""
    if args.write_baseline is None:
        return
    from finding_baseline import save_baseline

    all_results = [r for run in runs for r in run.results]
    recorded = save_baseline(args.write_baseline, all_results, root=args.root)
    print(f"Recorded {recorded} finding(s) into {args.write_baseline}.")


def _write_log_and_summarize(
    log: dict[str, Any], runs: Sequence[Run], out_path: Path, suppressed: int
) -> None:
    """Write SARIF log to disk and render console breakdown."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")
    total = sum(len(run.results) for run in runs)
    accepted = f", {suppressed} accepted by baseline" if suppressed else ""
    print(f"Wrote {out_path}: {len(log['runs'])} run(s), {total} result(s){accepted}, schema valid.")
    for run in runs:
        levels = _level_counts(run)
        print(f"  {run.tool_name:<34} {len(run.results):>4} " + levels)


def _handle_report(args: argparse.Namespace) -> int:
    """Build, validate and write the SARIF log."""
    paths = _source_paths(args.root)
    runs = list(collect_runs(args.sources, paths, args.root, args.include_advisory))
    _record_baseline_if_requested(args, runs)
    runs, suppressed = _apply_baseline(runs, args.baseline, args.root)
    log = build_log(runs, args.root)
    if _report_schema_errors(validate_log(log, args.schema)):
        return 1
    _write_log_and_summarize(log, runs, args.out, suppressed)
    return 1 if args.fail_on_error and _has_error(runs) else 0


def _level_counts(run: Run) -> str:
    """Render one run's results broken down by severity."""
    counts = {level: sum(1 for r in run.results if r.level == level) for level in LEVELS}
    return " ".join(f"{level}={counts[level]}" for level in ("error", "warning", "note"))


def _has_error(runs: Sequence[Run]) -> bool:
    """Report whether any run produced an error-level result."""
    return any(result.level == "error" for run in runs for result in run.results)


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser for SARIF reporting."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    parser.add_argument(
        "--sources",
        nargs="*",
        default=sorted(ADAPTERS),
        choices=sorted(ADAPTERS),
        help="Which oracles to include (default: all)",
    )
    parser.add_argument(
        "--include-advisory",
        action="store_true",
        help="Include advisory smells, which are informative rather than gating",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit non-zero when any result is error level",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Suppress findings this baseline already accepts (see tools/finding_baseline.py)",
    )
    parser.add_argument(
        "--write-baseline",
        type=Path,
        default=None,
        help="Record every current finding as accepted, then report against it",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for SARIF reporting."""
    return _handle_report(build_arg_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
