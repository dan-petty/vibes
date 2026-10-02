"""Unit tests for Formal Tool Contract Verification Gate."""

from __future__ import annotations

from typing import Any

import pytest
from verifier import (
    ParameterContract,
    ParameterType,
    ToolContract,
    ToolContractVerifier,
    ViolationKind,
    main,
)


@pytest.fixture
def test_verifier() -> ToolContractVerifier:
    """Fixture providing a configured verifier with a sample deployment tool."""
    verifier = ToolContractVerifier()
    contract = ToolContract(
        tool_name="deploy_service",
        description="Deploy a containerized microservice to target namespace.",
        parameters={
            "service_name": ParameterContract(
                name="service_name",
                param_type=ParameterType.STRING,
                required=True,
                max_length=64,
                description="Name of the service to deploy.",
            ),
            "replicas": ParameterContract(
                name="replicas",
                param_type=ParameterType.INTEGER,
                required=False,
                min_value=1,
                max_value=10,
                default_value=1,
                description="Number of pod replicas (1-10).",
            ),
            "dry_run": ParameterContract(
                name="dry_run",
                param_type=ParameterType.BOOLEAN,
                required=False,
                default_value=False,
                description="Execute dry-run without mutating cluster state.",
            ),
        },
        return_type=dict,
    )

    def mock_deploy(service_name: str, replicas: int = 1, dry_run: bool = False) -> dict[str, Any]:
        return {"status": "deployed", "service": service_name, "replicas": replicas, "dry_run": dry_run}

    verifier.register_tool(contract, mock_deploy)
    return verifier


def test_verifier_valid_tool_call_succeeds(test_verifier: ToolContractVerifier) -> None:
    """Verify compliant tool call passes validation and executes cleanly."""
    args = {"service_name": "payment-api", "replicas": 3, "dry_run": True}
    res, report = test_verifier.execute_call("deploy_service", args)

    assert (
        report.is_valid,
        len(report.violations),
        res,
    ) == (True, 0, {"status": "deployed", "service": "payment-api", "replicas": 3, "dry_run": True})


def test_verifier_hallucinated_parameter_rejected(test_verifier: ToolContractVerifier) -> None:
    """Verify extra hallucinated parameters trigger negative assertion in strict mode."""
    args = {"service_name": "auth-gateway", "cluster_override": "unauthorized-prod", "replicas": 2}
    res, report = test_verifier.execute_call("deploy_service", args)
    hallucinated = next((v for v in report.violations if v.kind == ViolationKind.HALLUCINATED_PARAM), None)
    assert (
        report.is_valid,
        res,
        hallucinated is not None,
        hallucinated.param_name if hallucinated else None,
        "cluster_override" in report.feedback_message,
    ) == (False, None, True, "cluster_override", True)


def test_verifier_missing_required_parameter(test_verifier: ToolContractVerifier) -> None:
    """Verify omitting a required parameter produces corrective validation error."""
    args = {"replicas": 2}
    res, report = test_verifier.execute_call("deploy_service", args)
    missing = next((v for v in report.violations if v.kind == ViolationKind.MISSING_REQUIRED_PARAM), None)
    assert (
        report.is_valid,
        res,
        missing is not None,
        missing.param_name if missing else None,
        "service_name" in report.feedback_message,
    ) == (False, None, True, "service_name", True)


def test_verifier_type_mismatch_detected(test_verifier: ToolContractVerifier) -> None:
    """Verify passing string for integer parameter is rejected without silent coercion."""
    args = {"service_name": "cache-service", "replicas": "five"}
    _res, report = test_verifier.execute_call("deploy_service", args)
    violation = next((v for v in report.violations if v.kind == ViolationKind.TYPE_MISMATCH), None)
    assert (
        report.is_valid,
        violation is not None,
        violation.param_name if violation else None,
        "expected integer" in (violation.message.lower() if violation else ""),
    ) == (False, True, "replicas", True)


def test_verifier_string_length_bound_exceeded(test_verifier: ToolContractVerifier) -> None:
    """Verify strings exceeding bounded length cap are flagged to prevent log bloat / CWE-400."""
    oversized_name = "a" * 128  # Max length is 64
    args = {"service_name": oversized_name}
    _res, report = test_verifier.execute_call("deploy_service", args)
    violation = next((v for v in report.violations if v.kind == ViolationKind.LENGTH_BOUND_EXCEEDED), None)
    assert (
        report.is_valid,
        violation is not None,
        violation.param_name if violation else None,
        "length 128 exceeds maximum 64" in (violation.message if violation else ""),
    ) == (False, True, "service_name", True)


def test_verifier_numeric_range_validation(test_verifier: ToolContractVerifier) -> None:
    """Verify numbers outside min/max bounds produce descriptive errors."""
    args_too_low = {"service_name": "worker", "replicas": 0}
    _, report_low = test_verifier.execute_call("deploy_service", args_too_low)
    assert any(v.kind == ViolationKind.VALUE_OUT_OF_RANGE for v in report_low.violations)

    args_too_high = {"service_name": "worker", "replicas": 50}
    _, report_high = test_verifier.execute_call("deploy_service", args_too_high)
    assert any(v.kind == ViolationKind.VALUE_OUT_OF_RANGE for v in report_high.violations)


def test_verifier_json_schema_generation(test_verifier: ToolContractVerifier) -> None:
    """Verify tool contract generates OpenAPI / Draft 2020-12 compatible JSON schema."""
    schema = test_verifier.to_json_schema("deploy_service")
    params = schema["parameters"]
    expected_meta = ("deploy_service", "Deploy a containerized microservice to target namespace.", ["service_name"], False)
    actual_meta = (schema["name"], schema["description"], params["required"], params["additionalProperties"])
    assert actual_meta == expected_meta and all(k in params["properties"] for k in ("service_name", "replicas", "dry_run"))


def test_verifier_output_contract_enforcement() -> None:
    """Verify tool execution return values are validated against declared return type."""
    verifier = ToolContractVerifier()
    contract = ToolContract(
        tool_name="get_count",
        description="Return active count",
        parameters={},
        return_type=int,
    )
    # Defective handler returning string instead of int
    verifier.register_tool(contract, lambda: "invalid_string_output")

    res, report = verifier.execute_call("get_count", {})
    assert (
        report.is_valid,
        res,
        any(v.kind == ViolationKind.OUTPUT_CONTRACT_VIOLATION for v in report.violations),
    ) == (False, None, True)


def test_verifier_corrective_feedback_generation(test_verifier: ToolContractVerifier) -> None:
    """Verify feedback message includes actionable prescriptive instructions for LLM re-prompting."""
    args = {"wrong_key": "val"}
    report = test_verifier.validate_call("deploy_service", args)

    assert (
        report.is_valid,
        len(report.violations) >= 2,
        "TOOL CONTRACT VIOLATION DETECTED" in report.feedback_message,
        "Prescriptive Action" in report.feedback_message,
    ) == (False, True, True, True)


def test_verifier_cli_demo(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify verifier demo CLI executes cleanly and prints scorecard."""
    main()
    captured = capsys.readouterr()
    assert "FORMAL TOOL CONTRACT VERIFICATION GATE DEMO" in captured.out
    assert "deploy_service" in captured.out
