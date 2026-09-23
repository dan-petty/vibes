"""Tests for conformance to the OpenTelemetry GenAI semantic conventions.

The claim these pin is not that the validator runs but that the generator beside it emits
spans a GenAI-aware backend can actually read. That claim is executed against the real
session and the real snapshot, because a private vocabulary and a conventional one are
indistinguishable in an OTLP payload — both parse, and only one charts.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import semconv
from generator import build_synthetic_agent_session, validate_session

CONVENTION = semconv.Convention.load()
INFERENCE = "gen_ai.inference.client"


def _codes(findings: list[semconv.Finding]) -> list[str]:
    """Return finding codes, which is what a test should assert on rather than prose."""
    return sorted(finding.code for finding in findings)


# --- The generator's own output ----------------------------------------------------------


def test_the_generated_session_has_no_conformance_errors() -> None:
    """The headline claim, executed rather than asserted."""
    errors = [f for _, f in validate_session(build_synthetic_agent_session("goal")) if f.level == semconv.ERROR]
    assert errors == []


def test_the_only_finding_is_the_documented_open_enum_warning() -> None:
    """Pinned so a new warning cannot hide behind the one that is there on purpose.

    `ollama` is not a well-known `gen_ai.provider.name`, and OpenTelemetry enums are open
    unless declared closed — so a local runtime is permitted and a typo looks identical.
    The exhibit keeps one, and this test makes sure it stays the only one.
    """
    findings = validate_session(build_synthetic_agent_session("goal"))
    assert [(f.level, f.code) for _, f in findings] == [(semconv.WARNING, "undeclared_enum_value")]


def test_every_span_carries_the_conventional_name() -> None:
    """A hand-written span name agrees with the convention until someone changes the model."""
    session = build_synthetic_agent_session("Ship the thing")
    names = [span.name for span in session.spans]
    assert names[:3] == [
        "invoke_workflow Ship the thing",
        "chat claude-3-5-sonnet",
        "chat qwen2.5-coder:14b",
    ]


# --- What the old spans looked like ------------------------------------------------------


def test_a_span_in_the_previous_private_vocabulary_is_rejected() -> None:
    """Exactly what this generator used to emit, and exactly why it charted nowhere."""
    findings = semconv.validate(
        CONVENTION, INFERENCE, "FrontierPlanning", "internal",
        {"ai.model.name": "claude-3-5-sonnet", "gen_ai.usage.prompt_tokens": 3400},
    )
    assert _codes(findings) == [
        "missing_required", "missing_required", "renamed_attribute", "wrong_kind",
    ]


def test_a_renamed_attribute_names_its_replacement() -> None:
    """The cost of a rename is paid by whoever reads the data, so the finding must say what to read."""
    findings = semconv.validate(
        CONVENTION, INFERENCE, "chat m", "client",
        {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
         "gen_ai.usage.completion_tokens": 10},
    )
    assert "gen_ai.usage.output_tokens" in findings[0].message


def test_an_attribute_whose_definition_merely_moved_is_not_deprecated() -> None:
    """The regression: thirty errors on a span that was right.

    The deprecation table is built from the repository the GenAI conventions *left*, where
    every one of them is marked deprecated with the reason "moved". Reading that table
    before the live registry reported every conformant attribute as obsolete.
    """
    assert CONVENTION.deprecated["gen_ai.operation.name"]["status"] == "moved"
    findings = semconv.validate(
        CONVENTION, INFERENCE, "chat m", "client",
        {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai"},
    )
    assert findings == []


# --- Attributes --------------------------------------------------------------------------


def test_an_attribute_outside_the_namespace_is_left_alone() -> None:
    """The convention permits custom attributes; this repository's agent metadata is that."""
    findings = semconv.validate(
        CONVENTION, INFERENCE, "chat m", "client",
        {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
         "vibes.agent.persona": "architect"},
    )
    assert findings == []


def test_a_misspelled_convention_key_is_an_error() -> None:
    """A typo inside the namespace is indistinguishable from an absent attribute downstream."""
    findings = semconv.validate(
        CONVENTION, INFERENCE, "chat m", "client",
        {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
         "gen_ai.usage.input_token": 5},
    )
    assert _codes(findings) == ["unknown_attribute"]


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ({"gen_ai.usage.input_tokens": True}, ["wrong_type"]),
        ({"gen_ai.usage.input_tokens": "3400"}, ["wrong_type"]),
        ({"gen_ai.response.finish_reasons": "stop"}, ["wrong_type"]),
        ({"gen_ai.response.finish_reasons": [1]}, ["wrong_type"]),
        ({"gen_ai.request.temperature": 0.7}, []),
        ({"gen_ai.request.temperature": True}, ["wrong_type"]),
        ({"gen_ai.response.finish_reasons": ["stop"]}, []),
    ],
)
def test_declared_types_are_checked(attributes: dict[str, object], expected: list[str]) -> None:
    """`isinstance(True, int)` is true, so a boolean satisfies an int unless it is refused.

    A scalar where an array is declared is the subtler one: it reaches the backend as a
    different column type and every query written against the array returns nothing.
    """
    findings = semconv.validate(
        CONVENTION, INFERENCE, "chat m", "client",
        {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai", **attributes},
    )
    assert _codes(findings) == expected


def test_an_external_attribute_is_accepted_without_a_registry_entry() -> None:
    """`server.address` is a general OpenTelemetry attribute the GenAI spans reuse."""
    findings = semconv.validate(
        CONVENTION, INFERENCE, "chat m", "client",
        {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
         "server.address": "example.com"},
    )
    assert findings == []


# --- Span shape ---------------------------------------------------------------------------


def test_an_unknown_span_type_stops_the_check_rather_than_guessing() -> None:
    """Every later rule is relative to the type, so continuing would invent its own answers."""
    findings = semconv.validate(CONVENTION, "gen_ai.nope", "n", "client", {})
    assert _codes(findings) == ["unknown_span_type"]


def test_the_span_name_rule_is_a_warning_because_the_convention_says_SHOULD() -> None:
    """Reporting a SHOULD as an error is how a validator teaches its reader to ignore it."""
    findings = semconv.validate(
        CONVENTION, INFERENCE, "FrontierPlanning", "client",
        {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
         "gen_ai.request.model": "gpt-4"},
    )
    assert [(f.level, f.code) for f in findings] == [(semconv.WARNING, "span_name")]


def test_operation_name_is_required_on_an_inference_span() -> None:
    """The derivation regression, pinned in the artefact that depends on it.

    `gen_ai.inference.client` re-references `gen_ai.operation.name` with no requirement
    level, purely to mark it sampling-relevant, while its attribute group declares it
    **required**. Reading the silent re-reference as the default downgraded it, and a
    validator built on that snapshot would accept a span missing the one attribute the
    convention mandates.
    """
    assert "gen_ai.operation.name" in CONVENTION.required(INFERENCE)
