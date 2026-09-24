#!/usr/bin/env python3
"""Typed Epistemic Seam & Subagent Context Handshake Protocol.

Enforces cryptographic context envelope hashing, negative schema boundaries,
and epistemic loss auditing across hierarchical agent delegation seams.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Final

SCHEMA_SARIF_VERSION: Final[str] = "2.1.0"
SCHEMA_SARIF_URI: Final[str] = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
)

RULE_CODES: Final[dict[str, str]] = {
    "SCHEMA_VIOLATION": "EPI001",
    "CONTEXT_TAMPERING": "EPI002",
    "DROPPED_PRECONDITION": "EPI003",
    "EPISTEMIC_LOSS_HIGH": "EPI004",
}


@dataclass(frozen=True)
class ContextEnvelope:
    """Cryptographically content-addressed context delegation contract."""

    envelope_id: str
    parent_agent: str
    target_subagent: str
    required_symbols: list[str]
    preconditions: list[str]
    allowed_parameters: list[str]
    context_hash: str
    created_at: float


@dataclass(frozen=True)
class SubagentResponse:
    """Structured response payload returned by subagent."""

    envelope_id: str
    subagent_id: str
    returned_symbols: list[str]
    provided_arguments: dict[str, Any]
    satisfied_preconditions: list[str]
    execution_status: str


@dataclass
class HandshakeAuditReport:
    """Telemetry report measuring epistemic integrity and contract compliance."""

    envelope_id: str
    is_valid: bool
    epistemic_loss: float
    violations: list[str] = field(default_factory=list)
    certificate: dict[str, Any] = field(default_factory=dict)


def compute_context_hash(
    symbols: list[str],
    preconditions: list[str],
    parameters: list[str],
) -> str:
    """Calculate deterministic SHA-256 fingerprint over context invariants."""
    serialized = json.dumps(
        {
            "symbols": sorted(symbols),
            "preconditions": sorted(preconditions),
            "parameters": sorted(parameters),
        },
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def create_context_envelope(
    parent_agent: str,
    target_subagent: str,
    symbols: list[str],
    preconditions: list[str],
    allowed_parameters: list[str],
) -> ContextEnvelope:
    """Synthesize a new content-addressed ContextEnvelope."""
    ctx_hash = compute_context_hash(symbols, preconditions, allowed_parameters)
    env_id = f"env-{ctx_hash[:12]}"
    return ContextEnvelope(
        envelope_id=env_id,
        parent_agent=parent_agent,
        target_subagent=target_subagent,
        required_symbols=symbols,
        preconditions=preconditions,
        allowed_parameters=allowed_parameters,
        context_hash=ctx_hash,
        created_at=time.time(),
    )


def verify_envelope_integrity(envelope: ContextEnvelope) -> bool:
    """Verify that envelope contents match the cryptographic hash."""
    expected = compute_context_hash(
        envelope.required_symbols,
        envelope.preconditions,
        envelope.allowed_parameters,
    )
    return expected == envelope.context_hash


def check_negative_schema_bounds(
    envelope: ContextEnvelope,
    arguments: dict[str, Any],
) -> list[str]:
    """Detect hallucinated parameters violating additionalProperties: false."""
    allowed = set(envelope.allowed_parameters)
    violations: list[str] = []
    for param in arguments:
        if param not in allowed:
            violations.append(f"UNRECOGNIZED_PARAMETER: '{param}' not declared in envelope schema")
    return violations


def audit_epistemic_loss(
    envelope: ContextEnvelope,
    response: SubagentResponse,
) -> HandshakeAuditReport:
    """Audit subagent response and calculate epistemic loss metric."""
    violations: list[str] = []
    if envelope.envelope_id != response.envelope_id:
        violations.append("ENVELOPE_ID_MISMATCH")

    if not verify_envelope_integrity(envelope):
        violations.append("CONTEXT_TAMPERING_DETECTED")

    schema_violations = check_negative_schema_bounds(envelope, response.provided_arguments)
    violations.extend(schema_violations)

    dropped_preconditions = [p for p in envelope.preconditions if p not in response.satisfied_preconditions]
    for dp in dropped_preconditions:
        violations.append(f"DROPPED_PRECONDITION: '{dp}'")

    total_checks = max(1, len(envelope.preconditions) + len(envelope.allowed_parameters))
    loss_weight = len(dropped_preconditions) + len(schema_violations)
    epistemic_loss = min(1.0, round(loss_weight / total_checks, 4))

    is_valid = len(violations) == 0
    cert = generate_handshake_certificate(envelope, response, epistemic_loss) if is_valid else {}

    return HandshakeAuditReport(
        envelope_id=envelope.envelope_id,
        is_valid=is_valid,
        epistemic_loss=epistemic_loss,
        violations=violations,
        certificate=cert,
    )


def generate_handshake_certificate(
    envelope: ContextEnvelope,
    response: SubagentResponse,
    loss: float,
) -> dict[str, Any]:
    """Generate an attested cryptographic completion token."""
    raw = f"{envelope.envelope_id}:{response.subagent_id}:{loss}:{time.time()}"
    signature = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return {
        "status": "ATTESTED",
        "envelope_id": envelope.envelope_id,
        "subagent_id": response.subagent_id,
        "epistemic_loss": loss,
        "signature": signature,
        "attested_at": time.time(),
    }


def to_sarif(report: HandshakeAuditReport) -> dict[str, Any]:
    """Format handshake audit as OASIS SARIF 2.1.0 telemetry."""
    rules = [
        {
            "id": code,
            "name": name,
            "shortDescription": {"text": f"Epistemic seam invariant: {name}"},
        }
        for name, code in RULE_CODES.items()
    ]
    results = [
        {
            "ruleId": RULE_CODES.get("SCHEMA_VIOLATION", "EPI001"),
            "level": "error",
            "message": {"text": f"Epistemic seam violation in envelope '{report.envelope_id}': {v}"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f"agent/handshake/{report.envelope_id}"},
                        "region": {"startLine": 1, "startColumn": 1},
                    }
                }
            ],
        }
        for v in report.violations
    ]
    return {
        "$schema": SCHEMA_SARIF_URI,
        "version": SCHEMA_SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "epistemic-context-handshake",
                        "version": "1.0.0",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def to_markdown(report: HandshakeAuditReport) -> str:
    """Format audit report as clean Markdown."""
    verdict = "PASSED" if report.is_valid else "FAILED"
    lines = [
        "# Epistemic Context Handshake Report",
        "",
        f"**Verdict**: {'✅' if report.is_valid else '❌'} {verdict}",
        f"- **Envelope ID**: `{report.envelope_id}`",
        f"- **Epistemic Loss ($E_{{loss}}$)**: {report.epistemic_loss * 100:.1f}%",
        f"- **Violations Detected**: {len(report.violations)}",
        "",
    ]
    if report.violations:
        lines.append("## Detected Violations")
        for v in report.violations:
            lines.append(f"- ❌ {v}")
    elif report.certificate:
        lines.append(f"**Certificate Signature**: `{report.certificate.get('signature', '')[:16]}...`")
    return "\n".join(lines) + "\n"


def parse_args(args: list[str]) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Typed Epistemic Seam & Subagent Context Handshake Protocol",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    env_p = sub.add_parser("envelope", help="Generate a context envelope")
    env_p.add_argument("--parent", default="lead-agent", help="Parent agent ID")
    env_p.add_argument("--target", default="subagent-refactor", help="Target subagent ID")

    audit_p = sub.add_parser("audit", help="Audit sample handshake")
    audit_p.add_argument("--format", choices=["text", "json", "markdown", "sarif"], default="text")

    return parser.parse_args(args)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for standalone execution."""
    opts = parse_args(argv or sys.argv[1:])
    if opts.command == "envelope":
        env = create_context_envelope(
            opts.parent,
            opts.target,
            ["parse_ast", "format_output"],
            ["git_clean", "coverage_floor_met"],
            ["file_path", "line_limit"],
        )
        print(json.dumps(asdict(env), indent=2))
        return 0

    sample_env = create_context_envelope(
        "lead-agent",
        "subagent-refactor",
        ["parse_ast"],
        ["git_clean"],
        ["file_path"],
    )
    sample_res = SubagentResponse(
        envelope_id=sample_env.envelope_id,
        subagent_id="subagent-refactor",
        returned_symbols=["parse_ast"],
        provided_arguments={"file_path": "example.py"},
        satisfied_preconditions=["git_clean"],
        execution_status="SUCCESS",
    )
    report = audit_epistemic_loss(sample_env, sample_res)

    if opts.format == "json":
        print(json.dumps(asdict(report), indent=2))
    elif opts.format == "sarif":
        print(json.dumps(to_sarif(report), indent=2))
    elif opts.format == "markdown":
        print(to_markdown(report))
    else:
        print(f"Handshake: {'VALID' if report.is_valid else 'INVALID'} (Loss: {report.epistemic_loss})")
    return 0 if report.is_valid else 1


if __name__ == "__main__":
    sys.exit(main())
