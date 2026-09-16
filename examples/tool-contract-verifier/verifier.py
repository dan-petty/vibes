#!/usr/bin/env python3
"""Formal Tool Contract Verification Gate for AI Agents.

Validates incoming agent tool invocation payloads and outgoing tool execution
results against rigid schemas (JSON Schema Draft 2020-12 / OpenAPI compatible).
Enforces negative assertions against hallucinated parameters, missing required
arguments, unvalidated string lengths (CWE-400), and type coercion vulnerabilities.
Emits structured, prescriptive error prompts for autonomous zero-shot self-correction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import sys
from typing import Any, Callable, Sequence

# Canonical string length ceiling to mitigate log bloat / CWE-400
DEFAULT_STRING_LENGTH_CAP = 256


class ViolationKind(str, Enum):
    """Taxonomy of tool contract violations."""
    HALLUCINATED_PARAM = "HALLUCINATED_PARAM"
    MISSING_REQUIRED_PARAM = "MISSING_REQUIRED_PARAM"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    LENGTH_BOUND_EXCEEDED = "LENGTH_BOUND_EXCEEDED"
    VALUE_OUT_OF_RANGE = "VALUE_OUT_OF_RANGE"
    OUTPUT_CONTRACT_VIOLATION = "OUTPUT_CONTRACT_VIOLATION"


class ParameterType(str, Enum):
    """Supported parameter data types."""
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


@dataclass(frozen=True)
class ParameterContract:
    """Formal specification for a single tool argument."""
    name: str
    param_type: ParameterType
    required: bool = True
    max_length: int = DEFAULT_STRING_LENGTH_CAP
    min_value: float | int | None = None
    max_value: float | int | None = None
    default_value: Any = None
    description: str = ""

    def to_json_schema_property(self) -> dict[str, Any]:
        """Convert parameter contract into JSON Schema property definition."""
        prop: dict[str, Any] = {
            "type": self.param_type.value,
            "description": self.description,
        }
        if self.param_type == ParameterType.STRING:
            prop["maxLength"] = self.max_length
        if self.min_value is not None:
            prop["minimum"] = self.min_value
        if self.max_value is not None:
            prop["maximum"] = self.max_value
        if self.default_value is not None:
            prop["default"] = self.default_value
        return prop


@dataclass(frozen=True)
class ContractViolation:
    """Detailed record of a single schema contract breach."""
    param_name: str
    kind: ViolationKind
    received_value: Any
    expected_spec: str
    message: str
    corrective_hint: str


@dataclass
class ContractValidationReport:
    """Evaluation scorecard for an agent tool call validation pass."""
    tool_name: str
    is_valid: bool
    violations: list[ContractViolation] = field(default_factory=list)
    feedback_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize validation report to JSON-compatible dictionary."""
        return {
            "tool_name": self.tool_name,
            "is_valid": self.is_valid,
            "violations_count": len(self.violations),
            "violations": [
                {
                    "param_name": v.param_name,
                    "kind": v.kind.value,
                    "received_value": str(v.received_value)[:128],
                    "expected_spec": v.expected_spec,
                    "message": v.message,
                    "corrective_hint": v.corrective_hint,
                }
                for v in self.violations
            ],
            "feedback_message": self.feedback_message,
        }


@dataclass
class ToolContract:
    """Complete specification of a callable agent tool."""
    tool_name: str
    description: str
    parameters: dict[str, ParameterContract] = field(default_factory=dict)
    strict_forbid_extra: bool = True
    return_type: type | None = None


TYPE_PREDICATES: dict[ParameterType, Callable[[Any], bool]] = {
    ParameterType.BOOLEAN: lambda v: isinstance(v, bool),
    ParameterType.INTEGER: lambda v: isinstance(v, int) and not isinstance(v, bool),
    ParameterType.FLOAT: lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    ParameterType.STRING: lambda v: isinstance(v, str),
    ParameterType.ARRAY: lambda v: isinstance(v, list),
    ParameterType.OBJECT: lambda v: isinstance(v, dict),
}


def _check_type_match(expected: ParameterType, val: Any) -> bool:
    """Predicate evaluating if value matches declared parameter type via table lookup."""
    predicate = TYPE_PREDICATES.get(expected)
    return bool(predicate and predicate(val))


def _check_string_length(param: ParameterContract, val: str) -> tuple[bool, str | None]:
    """Validate string length against upper bound cap."""
    if len(val) > param.max_length:
        msg = f"Parameter '{param.name}' length {len(val)} exceeds maximum {param.max_length} characters."
        return False, msg
    return True, None


def _check_numeric_bounds(param: ParameterContract, val: int | float) -> tuple[bool, str | None]:
    """Validate numeric value against min/max bounds."""
    if param.min_value is not None and val < param.min_value:
        return False, f"Parameter '{param.name}' value {val} is below minimum {param.min_value}."
    if param.max_value is not None and val > param.max_value:
        return False, f"Parameter '{param.name}' value {val} exceeds maximum {param.max_value}."
    return True, None


class ToolContractVerifier:
    """Validates tool invocations and generates corrective prompt feedback."""

    def __init__(self) -> None:
        self._contracts: dict[str, ToolContract] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}

    def register_tool(self, contract: ToolContract, handler: Callable[..., Any] | None = None) -> None:
        """Register a tool contract and its optional execution handler."""
        self._contracts[contract.tool_name] = contract
        if handler:
            self._handlers[contract.tool_name] = handler

    def get_tool(self, tool_name: str) -> ToolContract | None:
        """Retrieve registered tool contract by name."""
        return self._contracts.get(tool_name)

    def to_json_schema(self, tool_name: str) -> dict[str, Any]:
        """Generate JSON Schema Draft 2020-12 representation of a tool contract."""
        contract = self._contracts[tool_name]
        props = {p.name: p.to_json_schema_property() for p in contract.parameters.values()}
        required = [p.name for p in contract.parameters.values() if p.required]

        return {
            "name": contract.tool_name,
            "description": contract.description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": not contract.strict_forbid_extra,
            },
        }

    def validate_call(self, tool_name: str, arguments: dict[str, Any]) -> ContractValidationReport:
        """Validate argument dictionary against tool contract."""
        contract = self._contracts.get(tool_name)
        if not contract:
            return ContractValidationReport(
                tool_name=tool_name,
                is_valid=False,
                feedback_message=f"Unknown tool '{tool_name}'. Register contract before invoking.",
            )

        violations: list[ContractViolation] = []
        if contract.strict_forbid_extra:
            self._check_hallucinated_params(contract, arguments, violations)

        self._check_missing_params(contract, arguments, violations)
        self._validate_argument_values(contract, arguments, violations)

        is_valid = len(violations) == 0
        feedback = self._synthesize_feedback(tool_name, violations) if not is_valid else ""

        return ContractValidationReport(
            tool_name=tool_name,
            is_valid=is_valid,
            violations=violations,
            feedback_message=feedback,
        )

    @staticmethod
    def _check_hallucinated_params(
        contract: ToolContract, arguments: dict[str, Any], violations: list[ContractViolation]
    ) -> None:
        valid_keys = set(contract.parameters.keys())
        for key, val in sorted(arguments.items()):
            if key not in valid_keys:
                violations.append(
                    ContractViolation(
                        param_name=key,
                        kind=ViolationKind.HALLUCINATED_PARAM,
                        received_value=val,
                        expected_spec=f"Allowed parameters: {sorted(valid_keys)}",
                        message=f"Hallucinated parameter '{key}' is not defined in contract.",
                        corrective_hint=f"Remove '{key}' from tool arguments.",
                    )
                )

    @staticmethod
    def _check_missing_params(
        contract: ToolContract, arguments: dict[str, Any], violations: list[ContractViolation]
    ) -> None:
        for name, param in sorted(contract.parameters.items()):
            if param.required and name not in arguments:
                violations.append(
                    ContractViolation(
                        param_name=name,
                        kind=ViolationKind.MISSING_REQUIRED_PARAM,
                        received_value=None,
                        expected_spec=f"{param.param_type.value} (required)",
                        message=f"Required parameter '{name}' is missing.",
                        corrective_hint=f"Provide a valid {param.param_type.value} for '{name}'.",
                    )
                )

    @classmethod
    def _validate_argument_values(
        cls, contract: ToolContract, arguments: dict[str, Any], violations: list[ContractViolation]
    ) -> None:
        for key, val in sorted(arguments.items()):
            param = contract.parameters.get(key)
            if param is not None:
                cls._validate_single_param(param, val, violations)

    @classmethod
    def _validate_single_param(
        cls, param: ParameterContract, val: Any, violations: list[ContractViolation]
    ) -> None:
        if not _check_type_match(param.param_type, val):
            violations.append(
                ContractViolation(
                    param_name=param.name,
                    kind=ViolationKind.TYPE_MISMATCH,
                    received_value=val,
                    expected_spec=param.param_type.value,
                    message=f"Parameter '{param.name}' expected {param.param_type.value}, received {type(val).__name__}.",
                    corrective_hint=f"Coerce value to {param.param_type.value}.",
                )
            )
            return

        cls._validate_bounds(param, val, violations)

    @classmethod
    def _validate_string_bounds(
        cls, param: ParameterContract, val: str, violations: list[ContractViolation]
    ) -> None:
        ok, msg = _check_string_length(param, val)
        if not ok:
            violations.append(
                ContractViolation(
                    param_name=param.name,
                    kind=ViolationKind.LENGTH_BOUND_EXCEEDED,
                    received_value=val,
                    expected_spec=f"max_length: {param.max_length}",
                    message=msg or "",
                    corrective_hint=f"Truncate string to <= {param.max_length} characters.",
                )
            )

    @classmethod
    def _validate_numeric_bounds(
        cls, param: ParameterContract, val: int | float, violations: list[ContractViolation]
    ) -> None:
        ok, msg = _check_numeric_bounds(param, val)
        if not ok:
            violations.append(
                ContractViolation(
                    param_name=param.name,
                    kind=ViolationKind.VALUE_OUT_OF_RANGE,
                    received_value=val,
                    expected_spec=f"Range: [{param.min_value}, {param.max_value}]",
                    message=msg or "",
                    corrective_hint=f"Constrain value between {param.min_value} and {param.max_value}.",
                )
            )

    @classmethod
    def _validate_bounds(
        cls, param: ParameterContract, val: Any, violations: list[ContractViolation]
    ) -> None:
        if isinstance(val, str):
            cls._validate_string_bounds(param, val, violations)
        elif isinstance(val, (int, float)) and not isinstance(val, bool):
            cls._validate_numeric_bounds(param, val, violations)

    @staticmethod
    def _synthesize_feedback(tool_name: str, violations: list[ContractViolation]) -> str:
        lines = [
            f"❌ TOOL CONTRACT VIOLATION DETECTED for '{tool_name}' ({len(violations)} errors):",
            "--------------------------------------------------------------------------------",
        ]
        for idx, v in enumerate(violations, 1):
            lines.append(f"{idx}. [{v.kind.value}] {v.message}")
            lines.append(f"   👉 Prescriptive Action: {v.corrective_hint}")
        lines.append("--------------------------------------------------------------------------------")
        lines.append("Please re-issue the tool invocation adhering strictly to the schema contract.")
        return "\n".join(lines)

    def execute_call(self, tool_name: str, arguments: dict[str, Any]) -> tuple[Any | None, ContractValidationReport]:
        """Validate input arguments, invoke handler, and enforce return type contract."""
        report = self.validate_call(tool_name, arguments)
        if not report.is_valid:
            return None, report

        handler = self._handlers.get(tool_name)
        if not handler:
            return None, report

        try:
            result = handler(**arguments)
        except Exception as err:
            report.is_valid = False
            report.feedback_message = f"Execution exception in '{tool_name}': {str(err)[:256]}"
            return None, report

        contract = self._contracts[tool_name]
        if contract.return_type and not isinstance(result, contract.return_type):
            viol = ContractViolation(
                param_name="return_value",
                kind=ViolationKind.OUTPUT_CONTRACT_VIOLATION,
                received_value=result,
                expected_spec=contract.return_type.__name__,
                message=f"Return value expected {contract.return_type.__name__}, received {type(result).__name__}.",
                corrective_hint="Align handler output with contract return type.",
            )
            report.violations.append(viol)
            report.is_valid = False
            report.feedback_message = self._synthesize_feedback(tool_name, report.violations)
            return None, report

        return result, report


def main() -> None:
    """Execute interactive CLI demonstration of tool contract verification."""
    print("🛡️  FORMAL TOOL CONTRACT VERIFICATION GATE DEMO\n")

    verifier = ToolContractVerifier()
    contract = ToolContract(
        tool_name="deploy_service",
        description="Deploy a containerized service with replica constraints.",
        parameters={
            "service_name": ParameterContract(
                name="service_name",
                param_type=ParameterType.STRING,
                required=True,
                max_length=32,
                description="Unique service identifier.",
            ),
            "replicas": ParameterContract(
                name="replicas",
                param_type=ParameterType.INTEGER,
                required=False,
                min_value=1,
                max_value=5,
                default_value=1,
                description="Replica count (1-5).",
            ),
        },
        return_type=dict,
    )

    verifier.register_tool(contract, lambda service_name, replicas=1: {"status": "ok", "service": service_name, "replicas": replicas})

    print("1. Schema Generation (Draft 2020-12):")
    print(json.dumps(verifier.to_json_schema("deploy_service"), indent=2))

    print("\n2. Validating Malformed Call (Hallucinated Param + Type Mismatch + Bound Breach):")
    bad_payload = {
        "service_name": "super_long_unbounded_service_name_that_exceeds_thirty_two_chars",
        "replicas": "invalid_string_replica",
        "secret_token_leak": "token-12345",
    }
    _, report = verifier.execute_call("deploy_service", bad_payload)
    print(report.feedback_message)

    print("\n3. Validating Compliant Call:")
    clean_payload = {"service_name": "auth-api", "replicas": 3}
    res, report_clean = verifier.execute_call("deploy_service", clean_payload)
    print(f"Validation Passed: {report_clean.is_valid} | Result: {res}")


if __name__ == "__main__":
    main()
