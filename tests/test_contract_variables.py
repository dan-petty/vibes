"""Tests for the questions a contract asks before it becomes an application.

The rules pinned here are the two that make an interactive generator safe to run without a
person watching: an answer is substituted into the parsed document rather than into its
text, so it cannot restructure the contract; and every variable carries a default, so the
contract resolves with nobody there to answer it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from contract_variables import (
    ContractError,
    Variable,
    _prompt_one_attempt,
    _resolve_one,
    coerce,
    load_variables,
    parse_assignments,
    placeholders,
    render,
    resolve,
)

ENTITY = Variable(name="entity", prompt="What is reconciled", type="string", default="invoice")
COUNT = Variable(name="count", prompt="How many", type="integer", default=3)
UNIT = Variable(
    name="unit", prompt="Which unit", type="choice", default="currency", choices=("currency", "percent")
)


def _declare(**overrides: object) -> dict[str, Any]:
    """Return a document declaring one variable with the given overrides applied."""
    return {"variables": [{"name": "entity", "prompt": "What", "default": "invoice", **overrides}]}


# --- Declaration -----------------------------------------------------------------------


def test_a_variable_without_a_default_is_refused() -> None:
    """A default is what makes the contract resolvable with nobody watching.

    Without one, a generator run from CI either prompts into a pipe and hangs or fails at
    the point where it can no longer tell anyone why.
    """
    document = {"variables": [{"name": "entity", "prompt": "What"}]}
    with pytest.raises(ContractError, match="a default is required"):
        load_variables(document)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"name": "Entity"}, "lowercase identifier"),
        ({"prompt": ""}, "prompt is required"),
        ({"type": "widget"}, "unknown type"),
        ({"type": "choice"}, "requires a non-empty choices list"),
        ({"choices": ["a", "b"]}, "only meaningful with type 'choice'"),
        ({"type": "integer", "default": "not a number"}, "default is invalid"),
        ({"type": "choice", "choices": ["a"], "default": "b"}, "default is invalid"),
    ],
)
def test_a_declaration_that_could_not_be_answered_is_refused(
    overrides: dict[str, object], expected: str
) -> None:
    """Every breach is checked where it is declared, not where its answer is used."""
    with pytest.raises(ContractError, match=expected):
        load_variables(_declare(**overrides))


def test_every_breach_in_the_block_is_reported_at_once() -> None:
    """One run, every problem: a caller fixing one declaration per run is the cost avoided."""
    document = {
        "variables": [
            {"name": "Bad", "prompt": "", "default": "x"},
            {"name": "other", "prompt": "What", "type": "integer", "default": "nope"},
        ]
    }
    with pytest.raises(ContractError) as caught:
        load_variables(document)
    message = str(caught.value)
    assert (
        "lowercase identifier" in message,
        "prompt is required" in message,
        "default is invalid" in message,
    ) == (True, True, True)


def test_two_variables_of_one_name_are_refused() -> None:
    """The second would silently win the answers dictionary and the first would vanish."""
    entry = {"name": "entity", "prompt": "What", "default": "invoice"}
    with pytest.raises(ContractError, match="duplicate variable"):
        load_variables({"variables": [entry, dict(entry)]})


def test_a_contract_with_no_variables_declares_none() -> None:
    """The block is optional, so every contract written before it still loads."""
    assert load_variables({"name": "Demo"}) == ()


# --- Coercion --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "raw", "expected"),
    [
        (ENTITY, "shipment", "shipment"),
        (COUNT, "7", 7),
        (COUNT, 7, 7),
        (UNIT, "percent", "percent"),
        (Variable("f", "How much", "number", 1.0), "2.5", 2.5),
        (Variable("f", "How much", "number", 1.0), 2, 2.0),
        (Variable("b", "Whether", "boolean", False), "yes", True),
        (Variable("b", "Whether", "boolean", False), "OFF", False),
        (Variable("b", "Whether", "boolean", False), True, True),
    ],
)
def test_an_answer_is_converted_to_the_declared_type(variable: Variable, raw: Any, expected: Any) -> None:
    """A typed answer from a file and a typed answer from a prompt land in one function.

    Two conversion paths are two opinions about what `no` means, and they diverge on the
    first value a person types that YAML would have parsed differently.
    """
    assert coerce(variable, raw) == expected


@pytest.mark.parametrize(
    ("variable", "raw", "expected"),
    [
        (COUNT, True, "got a boolean"),
        (Variable("f", "How much", "number", 1.0), False, "got a boolean"),
        (COUNT, "seven", "expected an integer"),
        (ENTITY, 7, "expected a string"),
        (UNIT, "furlongs", "is not one of"),
        (Variable("b", "Whether", "boolean", False), "maybe", "expected a boolean"),
    ],
)
def test_an_answer_the_type_cannot_hold_is_refused(variable: Variable, raw: Any, expected: str) -> None:
    """`isinstance(True, int)` is true, so a boolean would otherwise satisfy an integer.

    It would then reach the contract as the word `True` rather than as `1`, which is a
    defect visible only in the rendered output of whichever field used it.
    """
    with pytest.raises(ContractError, match=expected):
        coerce(variable, raw)


# --- Resolution ------------------------------------------------------------------------


def test_nothing_supplied_and_nobody_asked_still_resolves() -> None:
    """The non-interactive path is the one CI takes, so it is the one that must not fail."""
    assert resolve((ENTITY, COUNT, UNIT), {}) == {"entity": "invoice", "count": 3, "unit": "currency"}


def test_a_supplied_answer_is_never_asked_for() -> None:
    """Prompting for what was already answered turns a scripted run into an interactive one."""

    def refuse(_: str) -> str:
        raise AssertionError("asked for an answer that was supplied")

    assert resolve((ENTITY,), {"entity": "shipment"}, interactive=True, ask=refuse) == {"entity": "shipment"}


def test_an_empty_line_accepts_the_default() -> None:
    """Pressing return is the commonest answer, which is why a default is mandatory."""
    assert resolve((ENTITY, COUNT), {}, interactive=True, ask=lambda _: "") == {
        "entity": "invoice",
        "count": 3,
    }


def test_an_invalid_answer_is_asked_again() -> None:
    """A typo at a terminal is a typo, not a failed run."""
    answers = iter(["twelve", "12"])
    assert resolve((COUNT,), {}, interactive=True, ask=lambda _: next(answers)) == {"count": 12}


def test_asking_is_bounded() -> None:
    """A loop with no ceiling is a hang, and an autonomous caller will not improve on try four."""
    with pytest.raises(ContractError, match="no valid answer after 3 attempts"):
        resolve((COUNT,), {}, interactive=True, ask=lambda _: "still not a number")


def test_stdin_closing_mid_dialogue_falls_back_to_the_default() -> None:
    """A closed pipe is not an answer, and it is not a reason to abandon a generation run."""

    def closed(_: str) -> str:
        raise EOFError

    assert resolve((ENTITY,), {}, interactive=True, ask=closed) == {"entity": "invoice"}


def test_an_answer_to_a_variable_that_does_not_exist_is_refused() -> None:
    """A mistyped `--set` name would otherwise be accepted and silently do nothing."""
    with pytest.raises(ContractError, match="undeclared variable"):
        resolve((ENTITY,), {"entty": "shipment"})


def test_a_supplied_answer_of_the_wrong_type_names_the_variable() -> None:
    """`expected an integer` is not actionable without saying which variable expected one."""
    with pytest.raises(ContractError, match="count: expected an integer"):
        resolve((COUNT,), {"count": "many"})


# --- Rendering -------------------------------------------------------------------------


def test_answers_reach_every_string_leaf_and_nothing_else() -> None:
    """Substitution walks the parsed structure, so keys, numbers and booleans are untouched."""
    document = {"purpose": "Reconcile {{ entity }}s", "ops": [{"n": 2, "s": "one {{ entity }}", "f": True}]}
    assert render(document, {"entity": "invoice"}) == {
        "purpose": "Reconcile invoices",
        "ops": [{"n": 2, "s": "one invoice", "f": True}],
    }


def test_an_answer_cannot_restructure_the_contract() -> None:
    """The reason substitution happens after parsing rather than before.

    Rendering the YAML text and parsing the result would let this answer introduce a
    top-level key — the same defect as interpolating an expression into a workflow `run:`
    block, which §8.11 forbids for the same reason. Here it is a long string and nothing else.
    """
    hostile = "invoice\nslug: hijacked\npurpose: owned"
    rendered = render({"purpose": "About {{ entity }}", "slug": "real"}, {"entity": hostile})
    assert (rendered["slug"], "hijacked" in rendered["purpose"]) == ("real", True)


def test_an_answer_is_not_expanded_twice() -> None:
    """One pass, so an answer naming another variable is inert text rather than a second expansion."""
    rendered = render({"p": "{{ a }}"}, {"a": "{{ b }}", "b": "surprise"})
    assert rendered == {"p": "{{ b }}"}


def test_a_placeholder_nothing_declares_is_refused_with_all_of_them_named() -> None:
    """A silent empty substitution produces a contract that validates and means nothing."""
    with pytest.raises(ContractError, match=r"\['region', 'tier'\]"):
        render({"a": "{{ tier }}", "b": ["{{ region }}"]}, {})


def test_placeholders_are_found_at_every_depth() -> None:
    """Rendering and checking must agree about where a reference can appear."""
    document = {"a": "{{ one }}", "b": [{"c": "{{ two }}"}], "d": 3, "e": []}
    assert placeholders(document) == {"one", "two"}


# --- Command line ----------------------------------------------------------------------


def test_assignments_parse_and_keep_everything_after_the_first_equals() -> None:
    """A value containing `=` is ordinary; splitting on every one of them would truncate it."""
    assert parse_assignments(["entity=shipment", "note=a=b"]) == {"entity": "shipment", "note": "a=b"}


def test_every_malformed_assignment_is_reported_at_once() -> None:
    """One run, every problem, for the same reason the declaration check reports all of them."""
    with pytest.raises(ContractError, match="entity"):
        parse_assignments(["entity", "=value", "ok=1"])


def test_the_prompt_line_states_the_choices_and_the_default() -> None:
    """A prompt that hides the default hides the thing pressing return will do."""
    assert UNIT.describe() == "Which unit [currency/percent] (currency): "


def test_resolve_one_and_prompt_attempt_helpers() -> None:
    """Verify single variable resolution and prompt attempt helper behavior."""
    v = Variable(name="count", prompt="How many?", type="integer", default=1)

    ans1 = _resolve_one(v, {"count": "5"}, interactive=False, ask=lambda _: "ignored")
    ans2 = _resolve_one(v, {}, interactive=False, ask=lambda _: "ignored")
    succ3, val3 = _prompt_one_attempt(v, lambda _: "10")
    succ4, val4 = _prompt_one_attempt(v, lambda _: "invalid")

    assert (ans1, ans2, succ3, val3, succ4, val4) == (5, 1, True, 10, False, None)
