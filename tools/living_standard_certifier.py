#!/usr/bin/env python3
"""Certified Living Standard for Agentic Engineering.

Writes a compliance report, a SHA-256 digest of it (a checksum, not a signed
attestation), and a Shields.io endpoint badge for seven pillars. The certifier measures
nothing itself. Each pillar runs instruments that this repository already has against
the target root, and passes only when they checked at least one file and found nothing:

- Pillar 1, complexity M <= 10 and nesting depth <= 5: the AST invariant sentinel with
  its `structural_only` preset.
- Pillar 2, no test function with 4 or more linear asserts: the verification pyramid
  auditor, rule VER001.
- Pillar 3, no private host address and no secret token: the AST invariant sentinel with
  its `security_only` preset, which honours its waivers, and the prompt injection
  scanner, rule EGR001.
- Pillar 4, documentation integrity: the docs validator. Every finding fails the pillar,
  as `--strict` does.
- Pillar 5, model and dependency safety: the AIBOM scanner (error findings, as its
  `--strict` does) and the supply chain audit (offline, every finding).
- Pillar 6, prompt injection vectors: the prompt injection scanner, INJ rules. Canary
  leakage needs the canary values, so the certifier does not check it.

The target root is a repository that people review, so the prompt injection scanner
applies the header waivers of its files (`# prompt-scanner: allow[INJ] <justification>`),
as the sentinel applies its own.
- Pillar 7, deprecation contracts against the `[project]` version in pyproject.toml: the
  deprecation lifecycle sentinel. No instrument finds feature flags in a repository, so
  flag expiry is not checked.

A pillar with no evaluator, with instruments that checked no file, or with an instrument
that could not run is "not evaluated". The result is CERTIFIED only when all seven
pillars pass, and FAILED otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from example_loader import load_example
from sarif_report import SARIF_SCHEMA_URI

STANDARD_VERSION: Final[str] = "v1.0.0"

TOOLING_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
MAX_REPORTED_FINDINGS: Final[int] = 20

# Files the prompt injection scanner reads for secret tokens and injection vectors.
SCANNED_TEXT_SUFFIXES: Final[tuple[str, ...]] = (
    ".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".j2", ".jinja", ".prompt",
)
# Files the AIBOM scanner inspects: Python source and the model weight formats it reads.
MODEL_FILE_SUFFIXES: Final[tuple[str, ...]] = (
    ".py", ".safetensors", ".gguf", ".onnx", ".pt", ".bin", ".pkl",
)


class PillarId(StrEnum):
    """The Seven Pillars of the Certified Living Standard for Agentic Engineering."""

    P1_COMPLEXITY_BOUNDS = "P1_COMPLEXITY_BOUNDS"
    P2_ASSERTION_CONSOLIDATION = "P2_ASSERTION_CONSOLIDATION"
    P3_ZERO_TRUST_EGRESS = "P3_ZERO_TRUST_EGRESS"
    P4_DOCS_INTEGRITY = "P4_DOCS_INTEGRITY"
    P5_SUPPLY_CHAIN_AIBOM = "P5_SUPPLY_CHAIN_AIBOM"
    P6_PROMPT_GUARDRAILS = "P6_PROMPT_GUARDRAILS"
    P7_CHANGE_MANAGEMENT = "P7_CHANGE_MANAGEMENT"


class CertificationStatus(StrEnum):
    """Overall certification outcome status."""

    CERTIFIED = "certified"
    FAILED = "failed"


@dataclass(frozen=True)
class PillarEvaluation:
    """Evaluation result for an individual standard pillar.

    `evaluated` is False when no instrument measured the pillar. Such a pillar never
    passes, so a missing measurement cannot read as a clean one.
    """

    pillar_id: PillarId
    name: str
    passed: bool
    score: float
    violations_count: int
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    evaluated: bool = True


@dataclass(frozen=True)
class CertificationResult:
    """Comprehensive compliance certification outcome."""

    standard_version: str
    status: CertificationStatus
    compliance_score: float
    pillars: tuple[PillarEvaluation, ...]
    repository_name: str
    commit_sha: str
    timestamp: str
    attestation_digest: str


@dataclass(frozen=True)
class InstrumentRun:
    """What one instrument checked below the target root, and what it found."""

    instrument: str
    checked: int
    findings: tuple[str, ...] = ()
    unit: str = "file(s)"


class NotEvaluatedError(Exception):
    """An instrument cannot measure the target root, for a reason the certificate states."""


PillarEvaluator = Callable[[Path], tuple[InstrumentRun, ...]]

PILLAR_NAMES: Final[dict[PillarId, str]] = {
    PillarId.P1_COMPLEXITY_BOUNDS: "Architectural Complexity Caps (M<=10, Depth<=5)",
    PillarId.P2_ASSERTION_CONSOLIDATION: "Assertion Sprawl Mitigation & Structural Tuples",
    PillarId.P3_ZERO_TRUST_EGRESS: "Zero-Trust Egress & Sanitization",
    PillarId.P4_DOCS_INTEGRITY: "Executable Documentation & Link Integrity",
    PillarId.P5_SUPPLY_CHAIN_AIBOM: "Supply Chain & AIBOM Safety",
    PillarId.P6_PROMPT_GUARDRAILS: "Adversarial Prompt Guardrails & ASCII Smuggling",
    PillarId.P7_CHANGE_MANAGEMENT: "Enterprise Change Management & Version Protocol",
}

# Parts of a pillar's rule that no instrument of this repository measures. The summary
# states them so that a pass does not claim more than was checked.
PILLAR_GAPS: Final[dict[PillarId, str]] = {
    PillarId.P6_PROMPT_GUARDRAILS: "Not checked: canary leakage, which needs the canary values.",
    PillarId.P7_CHANGE_MANAGEMENT: "Not checked: feature flag expiry, because no instrument finds flags.",
}


def _compute_sha256(content: str) -> str:
    """Return hexadecimal SHA-256 hash digest of string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _source_files(root: Path, suffixes: Sequence[str]) -> list[Path]:
    """Return the repository's own files below root, as `source_tree_policy` defines them."""
    from source_tree_policy import iter_source_files

    return sorted(iter_source_files(root, suffixes))


def _finding(root: Path, file_path: str, line: int, message: str) -> str:
    """Render one finding with its path relative to the target root."""
    path = Path(file_path)
    shown = path.relative_to(root) if path.is_relative_to(root) else path
    return f"{shown}:{line}: {message}"


def _sentinel_run(root: Path, preset_name: str) -> InstrumentRun:
    """Run the AST invariant sentinel with one of its presets over the target root."""
    sentinel = load_example(TOOLING_ROOT, "ast-invariant-sentinel", "sentinel")
    preset = sentinel.PRESETS[preset_name]
    config = sentinel.SentinelConfig(
        preset_name=preset.name,
        max_complexity=preset.max_complexity,
        max_depth=preset.max_depth,
        active_rules=preset.active_rules,
    )
    report = sentinel.audit_targets([root], config=config)
    findings = tuple(_finding(root, v.file_path, v.line_number, v.message) for v in report.violations)
    return InstrumentRun(f"AST invariant sentinel ({preset_name})", report.files_checked, findings)


def _prompt_scan_run(root: Path, rule_prefix: str) -> InstrumentRun:
    """Run the prompt injection scanner and keep the findings of the rules named."""
    from prompt_injection_scanner import scan_paths

    summary = scan_paths(_source_files(root, SCANNED_TEXT_SUFFIXES), honor_waivers=True)
    findings = tuple(
        _finding(root, f.file_path, f.line_number, f.message)
        for f in summary.findings
        if f.rule_id.startswith(rule_prefix)
    )
    return InstrumentRun(f"prompt injection scanner ({rule_prefix})", summary.scanned_targets, findings)


def _aibom_run(root: Path) -> InstrumentRun:
    """Run the AIBOM scanner and keep the error findings, which its `--strict` gate fails on."""
    from aibom_scanner import RuleSeverity, scan_paths

    report = scan_paths(_source_files(root, MODEL_FILE_SUFFIXES))
    findings = tuple(
        _finding(root, f.file_path, f.line_number, f.title)
        for f in report.findings
        if f.severity == RuleSeverity.ERROR
    )
    return InstrumentRun("AIBOM scanner (errors)", report.scanned_files, findings)


def _supply_chain_run(root: Path) -> InstrumentRun:
    """Run the supply chain audit offline, with the upstream snapshot the root keeps."""
    from supply_chain_audit import DEFAULT_SNAPSHOT, audit, build_inventory, enrich_versions
    from upstream_facts import load_snapshot

    inventory = build_inventory(root)
    inventory.requirements = enrich_versions(inventory.requirements, offline=True)
    found = audit(inventory, load_snapshot(root / DEFAULT_SNAPSHOT))
    findings = tuple(_finding(root, f.source, 1, f"{f.risk}: {f.subject}") for f in found)
    return InstrumentRun("supply chain audit (offline)", len(inventory.all()), findings, "inventory item(s)")


def _project_version(root: Path) -> str:
    """Return the `[project]` version that pyproject.toml declares below the target root."""
    pyproject = root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8") if pyproject.is_file() else ""
    try:
        version = tomllib.loads(text).get("project", {}).get("version")
    except tomllib.TOMLDecodeError as err:
        raise NotEvaluatedError(f"pyproject.toml does not parse ({err})") from err
    if not isinstance(version, str):
        raise NotEvaluatedError("pyproject.toml declares no [project] version to check deadlines against")
    return version


def _evaluate_p1_complexity(root: Path) -> tuple[InstrumentRun, ...]:
    """Audit Pillar 1: cyclomatic complexity and nesting depth bounds."""
    return (_sentinel_run(root, "structural_only"),)


def _evaluate_p2_assertions(root: Path) -> tuple[InstrumentRun, ...]:
    """Audit Pillar 2: test functions with linear assertion sprawl."""
    from verification_pyramid_auditor import VerificationRule, audit_test_suite

    summary = audit_test_suite([root])
    sprawl = VerificationRule.ASSERTION_DENSITY_SPRAWL.value
    findings = tuple(
        _finding(root, f.file_path, f.line_number, f.message)
        for f in summary.findings
        if f.rule_code == sprawl
    )
    return (InstrumentRun(f"verification pyramid auditor ({sprawl})", summary.total_files, findings),)


def _evaluate_p3_egress(root: Path) -> tuple[InstrumentRun, ...]:
    """Audit Pillar 3: private host addresses and secret tokens."""
    return (_sentinel_run(root, "security_only"), _prompt_scan_run(root, "EGR001"))


def _evaluate_p4_docs(root: Path) -> tuple[InstrumentRun, ...]:
    """Audit Pillar 4: fences, Mermaid, tables, links and snippets in documentation."""
    from docs_validator import DocConfigOverrides, DocsValidator, resolve_doc_config

    config = resolve_doc_config(DocConfigOverrides(config_file=root / "pyproject.toml"))
    report = DocsValidator(config).validate_directory(root)
    findings = tuple(_finding(root, f.file_path, f.line_number, f.message) for f in report.findings)
    return (InstrumentRun("docs validator", report.total_files, findings),)


def _evaluate_p5_supply_chain(root: Path) -> tuple[InstrumentRun, ...]:
    """Audit Pillar 5: unsafe model loads and dependency risks."""
    return (_aibom_run(root), _supply_chain_run(root))


def _evaluate_p6_prompts(root: Path) -> tuple[InstrumentRun, ...]:
    """Audit Pillar 6: prompt injection vectors such as Unicode Tag smuggling."""
    return (_prompt_scan_run(root, "INJ"),)


def _evaluate_p7_change_management(root: Path) -> tuple[InstrumentRun, ...]:
    """Audit Pillar 7: deprecation contracts against the project version."""
    lifecycle = load_example(TOOLING_ROOT, "deprecation-lifecycle-sentinel", "deprecation_sentinel")
    version = _project_version(root)
    try:
        lifecycle.parse_semver(version)
    except ValueError as err:
        raise NotEvaluatedError(f"the [project] version is not SemVer ({err})") from err
    files = _source_files(root, (".py",))
    report = lifecycle.audit_paths(files, version)
    findings = tuple(_finding(root, v.file_path, v.line_number, v.message) for v in report.violations)
    return (InstrumentRun("deprecation lifecycle sentinel", len(files), findings),)


PILLAR_EVALUATORS: Final[dict[PillarId, PillarEvaluator | None]] = {
    PillarId.P1_COMPLEXITY_BOUNDS: _evaluate_p1_complexity,
    PillarId.P2_ASSERTION_CONSOLIDATION: _evaluate_p2_assertions,
    PillarId.P3_ZERO_TRUST_EGRESS: _evaluate_p3_egress,
    PillarId.P4_DOCS_INTEGRITY: _evaluate_p4_docs,
    PillarId.P5_SUPPLY_CHAIN_AIBOM: _evaluate_p5_supply_chain,
    PillarId.P6_PROMPT_GUARDRAILS: _evaluate_p6_prompts,
    PillarId.P7_CHANGE_MANAGEMENT: _evaluate_p7_change_management,
}


def _not_evaluated(pillar_id: PillarId, reason: str) -> PillarEvaluation:
    """Report a pillar that no instrument measured. It fails, with the reason stated."""
    return PillarEvaluation(
        pillar_id=pillar_id,
        name=PILLAR_NAMES[pillar_id],
        passed=False,
        score=0.0,
        violations_count=0,
        summary=f"Not evaluated: {reason}.",
        details={"reason": reason},
        evaluated=False,
    )


def _measured_summary(pillar_id: PillarId, runs: Sequence[InstrumentRun]) -> str:
    """Say what each instrument checked and found, and what no instrument checks."""
    sentences = [f"{r.instrument} checked {r.checked} {r.unit} and found {len(r.findings)}." for r in runs]
    return " ".join([*sentences, PILLAR_GAPS.get(pillar_id, "")]).strip()


def _measured(pillar_id: PillarId, runs: Sequence[InstrumentRun]) -> PillarEvaluation:
    """Report a pillar from what its instruments checked and found."""
    findings = [finding for run in runs for finding in run.findings]
    passed = not findings
    return PillarEvaluation(
        pillar_id=pillar_id,
        name=PILLAR_NAMES[pillar_id],
        passed=passed,
        score=100.0 if passed else 0.0,
        violations_count=len(findings),
        summary=_measured_summary(pillar_id, runs),
        details={
            "checked": {run.instrument: run.checked for run in runs},
            "findings": findings[:MAX_REPORTED_FINDINGS],
        },
    )


def _evaluate_pillar(pillar_id: PillarId, root: Path) -> PillarEvaluation:
    """Run the evaluator of one pillar. A pillar that nothing measured is not evaluated."""
    evaluator = PILLAR_EVALUATORS.get(pillar_id)
    if evaluator is None:
        return _not_evaluated(pillar_id, "no evaluator measures this pillar")
    try:
        runs = evaluator(root)
    except NotEvaluatedError as err:
        return _not_evaluated(pillar_id, str(err))
    except (ImportError, OSError) as err:
        return _not_evaluated(pillar_id, f"an instrument could not run ({type(err).__name__}: {err})")
    if not any(run.checked for run in runs):
        return _not_evaluated(pillar_id, "its instruments checked no file below the target root")
    return _measured(pillar_id, runs)


def _calculate_overall_status(pillars: Sequence[PillarEvaluation]) -> CertificationStatus:
    """Certify only when every pillar was evaluated and passed."""
    if all(p.evaluated and p.passed for p in pillars):
        return CertificationStatus.CERTIFIED
    return CertificationStatus.FAILED


def evaluate_living_standard(
    repo_root: Path,
    repo_name: str = "vibes",
    commit_sha: str = "HEAD",
) -> CertificationResult:
    """Run the instruments of the Seven Pillars against the repository and certify it."""
    root = repo_root.resolve()
    pillars = tuple(_evaluate_pillar(pillar_id, root) for pillar_id in PillarId)
    total_score = sum(p.score for p in pillars) / len(pillars)

    status = _calculate_overall_status(pillars)
    ts = datetime.now(UTC).isoformat()
    digest_source = f"{repo_name}:{commit_sha}:{total_score:.1f}:{ts}"
    attestation_digest = _compute_sha256(digest_source)

    return CertificationResult(
        standard_version=STANDARD_VERSION,
        status=status,
        compliance_score=round(total_score, 1),
        pillars=pillars,
        repository_name=repo_name,
        commit_sha=commit_sha,
        timestamp=ts,
        attestation_digest=attestation_digest,
    )


def generate_shields_badge_json(result: CertificationResult) -> dict[str, Any]:
    """Format compliance badge JSON adhering to Shields.io Endpoint specification."""
    color_map = {
        CertificationStatus.CERTIFIED: "brightgreen",
        CertificationStatus.FAILED: "red",
    }
    label = f"Vibes Invariant Gate {result.standard_version}"
    message = f"{result.status.value.upper()} ({result.compliance_score}%)"
    return {
        "schemaVersion": 1,
        "label": label,
        "message": message,
        "color": color_map.get(result.status, "lightgrey"),
    }


def generate_attestation_payload(result: CertificationResult) -> dict[str, Any]:
    """Generate cryptographic attestation JSON certifying standard compliance."""
    return {
        "$schema": "https://in-toto.io/Statement/v1",
        "type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": result.repository_name,
                "digest": {"sha256": result.attestation_digest},
            }
        ],
        "predicateType": "https://vibes.dev/attestations/living-standard/v1",
        "predicate": {
            "standard_version": result.standard_version,
            "status": result.status.value,
            "compliance_score": result.compliance_score,
            "evaluated_at": result.timestamp,
            "pillars_evaluated": [
                {
                    "pillar": p.pillar_id.value,
                    "name": p.name,
                    "evaluated": p.evaluated,
                    "passed": p.passed,
                    "score": p.score,
                    "violations_count": p.violations_count,
                }
                for p in result.pillars
            ],
        },
    }


def _verdict(pillar: PillarEvaluation) -> str:
    """Return the certificate verdict of one pillar."""
    if not pillar.evaluated:
        return "NOT EVALUATED"
    return "✓ PASS" if pillar.passed else "✗ FAIL"


def format_markdown_certificate(result: CertificationResult) -> str:
    """Render formal Markdown certification report and badge embedding snippets."""
    color = generate_shields_badge_json(result)["color"]
    badge_md = (
        f"[![Vibes Invariant Gate {result.standard_version}]"
        f"(https://img.shields.io/badge/Vibes_Invariant_Gate_{result.standard_version}-"
        f"{result.status.value.upper()}_{result.compliance_score}%25-{color})]"
        f"(https://github.com/dan-petty/vibes)"
    )

    lines = [
        f"# Certified Living Standard for Agentic Engineering ({result.standard_version})",
        "",
        badge_md,
        "",
        f"- **Repository**: `{result.repository_name}`",
        f"- **Certification Verdict**: **{result.status.value.upper()}**",
        f"- **Composite Compliance**: **{result.compliance_score}%**",
        f"- **Attestation SHA-256**: `{result.attestation_digest}`",
        f"- **Timestamp**: `{result.timestamp}`",
        "",
        "## Seven Pillars of Agentic Discipline Audit",
        "",
        "| Pillar | Metric / Goal | Verdict | Score | Details |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for p in result.pillars:
        summary = p.summary.replace("|", "\\|")
        lines.append(
            f"| `{p.pillar_id.value}` | **{p.name}** | {_verdict(p)} | {p.score:.1f}% | {summary} |"
        )
    lines.append("")
    return "\n".join(lines)


def export_sarif_json(result: CertificationResult) -> dict[str, Any]:
    """Export standard certification outcome in OASIS SARIF 2.1.0 schema format."""
    rules = [
        {
            "id": p.pillar_id.value,
            "name": p.name,
            "shortDescription": {"text": p.name},
            "defaultConfiguration": {"level": "note" if p.passed else "error"},
        }
        for p in result.pillars
    ]
    results = [
        {
            "ruleId": p.pillar_id.value,
            "level": "note" if p.passed else "error",
            "message": {"text": f"{p.name}: {p.summary} (Score: {p.score}%)"},
        }
        for p in result.pillars
    ]
    return {
        "$schema": SARIF_SCHEMA_URI,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "vibes-living-standard-certifier",
                        "informationUri": "https://github.com/dan-petty/vibes",
                        "version": result.standard_version,
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def _build_argument_parser() -> argparse.ArgumentParser:
    """Construct command line interface argument parser."""
    parser = argparse.ArgumentParser(
        prog="living_standard_certifier.py",
        description="Certified Living Standard for Agentic Engineering.",
    )
    parser.add_argument("repo_path", nargs="?", type=Path, default=Path("."), help="Path to repository root")
    parser.add_argument("--repo-name", type=str, default="vibes", help="Repository identifier")
    parser.add_argument(
        "--format",
        choices=["markdown", "badge", "attestation", "sarif", "json"],
        default="markdown",
        help="Output serialization format",
    )
    parser.add_argument("--output", type=Path, default=None, help="Write output to file")
    return parser


def _dispatch_output(result: CertificationResult, fmt: str, out_path: Path | None) -> int:
    """Serialize and write output to stdout or file."""
    formatters = {
        "markdown": format_markdown_report,
        "badge": lambda r: json.dumps(generate_shields_badge_json(r), indent=2),
        "attestation": lambda r: json.dumps(generate_attestation_payload(r), indent=2),
        "sarif": lambda r: json.dumps(export_sarif_json(r), indent=2),
        "json": lambda r: json.dumps(generate_attestation_payload(r), indent=2),
    }
    formatter = formatters.get(fmt, format_markdown_certificate)
    content = formatter(result)

    if out_path:
        out_path.write_text(content, encoding="utf-8")
        print(f"Certification written to {out_path}")
    else:
        print(content)

    return 0 if result.status == CertificationStatus.CERTIFIED else 1


def format_markdown_report(result: CertificationResult) -> str:
    """Alias for format_markdown_certificate."""
    return format_markdown_certificate(result)


def main(argv: Sequence[str] | None = None) -> int:
    """Execute command line interface for Living Standard Certifier."""
    parser = _build_argument_parser()
    args = parser.parse_args(argv)

    result = evaluate_living_standard(args.repo_path, repo_name=args.repo_name)
    return _dispatch_output(result, args.format, args.output)


if __name__ == "__main__":
    sys.exit(main())
