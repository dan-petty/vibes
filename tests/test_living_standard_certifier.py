"""Unit test suite for Certified Living Standard for Agentic Engineering.

Verifies repository compliance evaluation across the Seven Pillars of Agentic Discipline,
Shields.io Endpoint compliance badge JSON synthesis, in-toto cryptographic attestation
payload generation, and SARIF 2.1.0 report serialization.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from living_standard_certifier import (
    CertificationResult,
    CertificationStatus,
    PillarEvaluation,
    PillarId,
    _calculate_overall_status,
    evaluate_living_standard,
    export_sarif_json,
    format_markdown_certificate,
    generate_attestation_payload,
    generate_shields_badge_json,
    main,
)


def test_evaluate_living_standard_all_pillars() -> None:
    """Verify all Seven Pillars are evaluated and present in certification result."""
    result = evaluate_living_standard(REPO_ROOT, repo_name="vibes_test")
    pillar_ids = {p.pillar_id for p in result.pillars}
    expected_ids = {
        PillarId.P1_COMPLEXITY_BOUNDS,
        PillarId.P2_ASSERTION_CONSOLIDATION,
        PillarId.P3_ZERO_TRUST_EGRESS,
        PillarId.P4_DOCS_INTEGRITY,
        PillarId.P5_SUPPLY_CHAIN_AIBOM,
        PillarId.P6_PROMPT_GUARDRAILS,
        PillarId.P7_CHANGE_MANAGEMENT,
    }
    assert (
        result.standard_version,
        pillar_ids,
        result.status,
    ) == (
        "v1.0.0",
        expected_ids,
        CertificationStatus.CERTIFIED,
    )


def test_calculate_overall_status_thresholds() -> None:
    """Verify certification status calculation rules and grade thresholds."""
    status_clean = _calculate_overall_status(100.0, 0)
    status_conditional = _calculate_overall_status(90.0, 0)
    status_failed_score = _calculate_overall_status(75.0, 0)
    status_failed_pillar = _calculate_overall_status(95.0, 1)

    assert (
        status_clean,
        status_conditional,
        status_failed_score,
        status_failed_pillar,
    ) == (
        CertificationStatus.CERTIFIED,
        CertificationStatus.CONDITIONAL,
        CertificationStatus.FAILED,
        CertificationStatus.FAILED,
    )


def test_shields_badge_json_format() -> None:
    """Verify compliance badge conforms to Shields.io Endpoint specification."""
    dummy_pillar = PillarEvaluation(
        pillar_id=PillarId.P1_COMPLEXITY_BOUNDS,
        name="Bounds",
        passed=True,
        score=100.0,
        violations_count=0,
        summary="Clean",
    )
    result = CertificationResult(
        standard_version="v1.0.0",
        status=CertificationStatus.CERTIFIED,
        compliance_score=100.0,
        pillars=(dummy_pillar,),
        repository_name="test-repo",
        commit_sha="abcd123",
        timestamp="2026-09-24T00:00:00Z",
        attestation_digest="hash123",
    )
    badge = generate_shields_badge_json(result)

    assert (
        badge.get("schemaVersion"),
        badge.get("color"),
        "v1.0.0" in badge.get("label", ""),
        "CERTIFIED" in badge.get("message", ""),
    ) == (
        1,
        "brightgreen",
        True,
        True,
    )


def test_shields_badge_json_failed_color() -> None:
    """Verify failed certification yields red badge color."""
    dummy_pillar = PillarEvaluation(
        pillar_id=PillarId.P1_COMPLEXITY_BOUNDS,
        name="Bounds",
        passed=False,
        score=50.0,
        violations_count=1,
        summary="Failed",
    )
    result = CertificationResult(
        standard_version="v1.0.0",
        status=CertificationStatus.FAILED,
        compliance_score=50.0,
        pillars=(dummy_pillar,),
        repository_name="test-repo",
        commit_sha="abcd123",
        timestamp="2026-09-24T00:00:00Z",
        attestation_digest="hash123",
    )
    badge = generate_shields_badge_json(result)
    assert badge.get("color") == "red"


def test_generate_attestation_payload() -> None:
    """Verify cryptographic in-toto statement attestation payload structure."""
    result = evaluate_living_standard(REPO_ROOT, repo_name="vibes_attest")
    attestation = generate_attestation_payload(result)

    assert (
        attestation.get("type"),
        attestation.get("predicateType"),
        attestation.get("subject", [{}])[0].get("name"),
    ) == (
        "https://in-toto.io/Statement/v1",
        "https://vibes.dev/attestations/living-standard/v1",
        "vibes_attest",
    )


def test_format_markdown_certificate() -> None:
    """Verify Markdown certificate output contains badge link and all 7 pillars."""
    result = evaluate_living_standard(REPO_ROOT, repo_name="vibes_md")
    report = format_markdown_certificate(result)

    assert (
        "Vibes Invariant Gate v1.0.0" in report,
        "Seven Pillars of Agentic Discipline Audit" in report,
        "P1_COMPLEXITY_BOUNDS" in report,
        "P7_CHANGE_MANAGEMENT" in report,
    ) == (
        True,
        True,
        True,
        True,
    )


def test_export_sarif_json() -> None:
    """Verify SARIF 2.1.0 export contains valid driver and rules."""
    result = evaluate_living_standard(REPO_ROOT, repo_name="vibes_sarif")
    sarif = export_sarif_json(result)

    assert (
        sarif.get("version"),
        sarif.get("runs", [{}])[0].get("tool", {}).get("driver", {}).get("name"),
        len(sarif.get("runs", [{}])[0].get("tool", {}).get("driver", {}).get("rules", [])),
    ) == (
        "2.1.0",
        "vibes-living-standard-certifier",
        7,
    )


def test_main_cli_execution_badge(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI main entry point with badge format."""
    ret = main(["--format", "badge"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert (
        ret,
        parsed.get("schemaVersion"),
        parsed.get("color"),
    ) == (
        0,
        1,
        "brightgreen",
    )


def test_main_cli_execution_attestation(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI main entry point with attestation format."""
    ret = main(["--format", "attestation"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert (
        ret,
        parsed.get("type"),
    ) == (
        0,
        "https://in-toto.io/Statement/v1",
    )
