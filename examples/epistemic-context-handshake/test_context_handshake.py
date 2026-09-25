"""Unit tests for the Typed Epistemic Seam & Subagent Context Handshake Protocol."""

from __future__ import annotations

import json
from typing import Any

from context_handshake import (
    ContextEnvelope,
    HandshakeAuditReport,
    SubagentResponse,
    audit_epistemic_loss,
    check_negative_schema_bounds,
    compute_context_hash,
    create_context_envelope,
    generate_handshake_certificate,
    main,
    to_markdown,
    to_sarif,
    verify_envelope_integrity,
)


def test_compute_context_hash_deterministic() -> None:
    """Verify hash computation is deterministic and sorted."""
    h1 = compute_context_hash(["symB", "symA"], ["pre1"], ["paramX"])
    h2 = compute_context_hash(["symA", "symB"], ["pre1"], ["paramX"])
    assert (h1 == h2, len(h1) == 64) == (True, True)


def test_create_envelope_and_verify_integrity() -> None:
    """Verify envelope synthesis and cryptographic integrity checking."""
    env = create_context_envelope("p1", "s1", ["symA"], ["preA"], ["pA"])
    ok = verify_envelope_integrity(env)
    assert (ok, env.parent_agent, env.target_subagent) == (True, "p1", "s1")

    tampered = ContextEnvelope(
        envelope_id=env.envelope_id,
        parent_agent="tampered",
        target_subagent=env.target_subagent,
        required_symbols=["injected_symbol"],
        preconditions=env.preconditions,
        allowed_parameters=env.allowed_parameters,
        context_hash=env.context_hash,
        created_at=env.created_at,
    )
    assert (verify_envelope_integrity(tampered), False) == (False, False)


def test_check_negative_schema_bounds() -> None:
    """Verify negative schema violation detection."""
    env = create_context_envelope("p1", "s1", ["symA"], ["preA"], ["valid_arg"])
    clean_args = {"valid_arg": 123}
    hallucinated_args = {"valid_arg": 123, "hallucinated_param": True}

    v_clean = check_negative_schema_bounds(env, clean_args)
    v_hallucinated = check_negative_schema_bounds(env, hallucinated_args)

    assert (len(v_clean), len(v_hallucinated)) == (0, 1)


def test_audit_epistemic_loss_clean_handshake() -> None:
    """Verify clean handshake execution with zero epistemic loss."""
    env = create_context_envelope("p1", "s1", ["symA"], ["pre1"], ["arg1"])
    res = SubagentResponse(
        envelope_id=env.envelope_id,
        subagent_id="s1",
        returned_symbols=["symA"],
        provided_arguments={"arg1": "value"},
        satisfied_preconditions=["pre1"],
        execution_status="OK",
    )
    report = audit_epistemic_loss(env, res)
    assert (report.is_valid, report.epistemic_loss, len(report.violations)) == (True, 0.0, 0)
    assert (report.certificate.get("status"), True) == ("ATTESTED", True)


def test_audit_epistemic_loss_dropped_preconditions() -> None:
    """Verify detection of dropped preconditions and epistemic loss calculation."""
    env = create_context_envelope("p1", "s1", ["symA"], ["pre1", "pre2"], ["arg1"])
    res = SubagentResponse(
        envelope_id=env.envelope_id,
        subagent_id="s1",
        returned_symbols=["symA"],
        provided_arguments={"arg1": "value"},
        satisfied_preconditions=["pre1"],  # dropped pre2
        execution_status="OK",
    )
    report = audit_epistemic_loss(env, res)
    assert (report.is_valid, report.epistemic_loss > 0.0, len(report.violations) > 0) == (False, True, True)


def test_generate_handshake_certificate() -> None:
    """Verify certificate generation and signature format."""
    env = create_context_envelope("p1", "s1", ["symA"], ["pre1"], ["arg1"])
    res = SubagentResponse(env.envelope_id, "s1", ["symA"], {}, ["pre1"], "OK")
    cert = generate_handshake_certificate(env, res, 0.0)
    assert (cert["status"], len(cert["signature"]) == 64) == ("ATTESTED", True)


def test_sarif_and_markdown_formatting() -> None:
    """Verify SARIF 2.1.0 and Markdown formatting output."""
    report = HandshakeAuditReport(
        envelope_id="env-test",
        is_valid=False,
        epistemic_loss=0.5,
        violations=["UNRECOGNIZED_PARAMETER: 'bad'"],
    )
    sarif = to_sarif(report)
    md = to_markdown(report)

    assert (sarif["version"], len(sarif["runs"][0]["results"])) == ("2.1.0", 1)
    assert ("# Epistemic Context Handshake Report" in md, "FAILED" in md) == (True, True)


def test_main_cli_envelope(capsys: Any) -> None:
    """Verify CLI envelope command."""
    rc = main(["envelope", "--parent", "root-agent", "--target", "sub-worker"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert (rc, payload["parent_agent"]) == (0, "root-agent")


def test_main_cli_audit_formats(capsys: Any) -> None:
    """Verify CLI audit command across output formats."""
    rc_json = main(["audit", "--format", "json"])
    captured_json = capsys.readouterr()
    payload = json.loads(captured_json.out)
    assert (rc_json, "epistemic_loss" in payload) == (0, True)

    rc_sarif = main(["audit", "--format", "sarif"])
    captured_sarif = capsys.readouterr()
    sarif_data = json.loads(captured_sarif.out)
    assert (rc_sarif, sarif_data["version"]) == (0, "2.1.0")

    rc_md = main(["audit", "--format", "markdown"])
    captured_md = capsys.readouterr()
    assert (rc_md, "# Epistemic Context Handshake Report" in captured_md.out) == (0, True)

