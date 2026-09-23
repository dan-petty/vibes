#!/usr/bin/env python3
"""The questions a contract asks before it becomes an application.

`cookiecutter`, `copier` and `yeoman` all collect values from the author before writing
anything, and the landscape survey ranked that gap first of twenty-four. This module is
that capability for [`app_factory.py`](./app_factory.py): a contract may declare variables,
its text may reference them as `{{ name }}`, and the answers arrive from a file, from the
command line, or from a person at a terminal.

Two decisions are load-bearing.

**Substitution happens after parsing, never before.** Rendering the YAML text and then
parsing it would let an answer containing a colon, a brace or a newline restructure the
document — the same class of defect as interpolating `${{ }}` into a workflow `run:` block,
which §8.11 already forbids for the same reason. Answers are substituted into string leaves
of the parsed document, so the worst an answer can do is be a longer string.

**Every variable must declare a default.** `cookiecutter.json` requires one for the same
reason: it makes the contract resolvable with nobody watching. An interactive step with no
non-interactive answer is a pipeline that hangs, and the hang appears on the machine least
able to respond to it. Prompting is therefore always an override and never a dependency.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

# Bounded rather than unbounded: an autonomous caller that mistypes an answer three times
# is not going to type it correctly on the fourth, and a loop with no ceiling is a hang.
MAX_ATTEMPTS: Final[int] = 3

VARIABLE_TYPES: Final[tuple[str, ...]] = ("string", "integer", "number", "boolean", "choice")

_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*$")
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")

_TRUE: Final[frozenset[str]] = frozenset({"true", "yes", "y", "1", "on"})
_FALSE: Final[frozenset[str]] = frozenset({"false", "no", "n", "0", "off"})


class ContractError(ValueError):
    """Raised when a contract cannot produce a valid application.

    Defined here rather than in the factory because both modules raise it and the factory
    imports this one; a distinct type because the caller's response differs, a malformed
    contract being the author's to fix where an OSError while writing is the environment's.
    """


@dataclass(frozen=True)
class Variable:
    """One question a contract asks, and the answer it falls back to."""

    name: str
    prompt: str
    type: str
    default: Any
    choices: tuple[str, ...] = ()

    def describe(self) -> str:
        """Render the prompt line shown at a terminal, defaults and choices included."""
        options = f" [{'/'.join(self.choices)}]" if self.choices else ""
        return f"{self.prompt}{options} ({self.default}): "


def load_variables(document: Mapping[str, Any]) -> tuple[Variable, ...]:
    """Parse and validate the `variables:` block, reporting every breach at once."""
    entries = document.get("variables", []) or []
    if not isinstance(entries, list):
        raise ContractError("variables must be a list of mappings")
    variables = tuple(_variable(entry) for entry in entries)
    problems = _declaration_problems(variables)
    if problems:
        raise ContractError("; ".join(problems))
    return variables


def _variable(entry: Any) -> Variable:
    """Build one variable from its declaration, without judging it yet."""
    if not isinstance(entry, dict):
        raise ContractError(f"expected a mapping per variable, got {type(entry).__name__}")
    choices = entry.get("choices") or []
    if not isinstance(choices, list):
        raise ContractError("choices must be a list")
    return Variable(
        name=str(entry.get("name", "")).strip(),
        prompt=str(entry.get("prompt", "")).strip(),
        type=str(entry.get("type", "string")).strip(),
        default=entry.get("default", _MISSING),
        choices=tuple(str(choice) for choice in choices),
    )


class _Missing:
    """Sentinel for an absent default, so that `default: null` stays distinguishable."""

    def __repr__(self) -> str:
        """Render as the word the error message uses."""
        return "<no default>"


_MISSING: Final[_Missing] = _Missing()


def _declaration_problems(variables: tuple[Variable, ...]) -> list[str]:
    """Return every breach across the whole block, so one run reports all of them."""
    names = [variable.name for variable in variables]
    problems = [
        f"variable name {n!r} must be a lowercase identifier" for n in names if not _IDENTIFIER.match(n)
    ]
    problems += [f"duplicate variable {n!r}" for n in sorted({n for n in names if names.count(n) > 1})]
    for variable in variables:
        problems += _one_declaration_problems(variable)
    return problems


def _one_declaration_problems(variable: Variable) -> list[str]:
    """Return every breach in one variable's declaration."""
    problems: list[str] = []
    if not variable.prompt:
        problems.append(f"{variable.name}: prompt is required; it is what the author is asked")
    if variable.type not in VARIABLE_TYPES:
        problems.append(f"{variable.name}: unknown type {variable.type!r}; allowed: {list(VARIABLE_TYPES)}")
        return problems
    problems += _choice_problems(variable)
    if isinstance(variable.default, _Missing):
        problems.append(f"{variable.name}: a default is required, so the contract resolves with no input")
        return problems
    try:
        coerce(variable, variable.default)
    except ContractError as err:
        problems.append(f"{variable.name}: default is invalid: {err}")
    return problems


def _choice_problems(variable: Variable) -> list[str]:
    """Return the breaches of the rule that `choices` and `type: choice` imply each other."""
    if variable.type == "choice" and not variable.choices:
        return [f"{variable.name}: type 'choice' requires a non-empty choices list"]
    if variable.type != "choice" and variable.choices:
        return [f"{variable.name}: choices are only meaningful with type 'choice'"]
    return []


def coerce(variable: Variable, raw: Any) -> Any:
    """Convert one answer to the declared type, refusing what does not convert.

    An answer reaches here either typed, from a YAML answers file, or as text, from a
    prompt or `--set`. Both paths land in one function so the two cannot disagree about
    what `no` means.
    """
    return _COERCERS[variable.type](variable, raw)


def _coerce_choice(variable: Variable, raw: Any) -> str:
    """Accept only one of the declared choices."""
    value = str(raw)
    if value not in variable.choices:
        raise ContractError(f"{value!r} is not one of {list(variable.choices)}")
    return value


def _coerce_string(_: Variable, raw: Any) -> str:
    """Accept text only, mirroring the strictness of the runtime the factory emits."""
    if not isinstance(raw, str):
        raise ContractError(f"expected a string, got {type(raw).__name__}")
    return raw


def _coerce_boolean(_: Variable, raw: Any) -> bool:
    """Accept a real boolean or one of the words a person types for one."""
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ContractError(f"expected a boolean, got {raw!r}")


def _coerce_integer(_: Variable, raw: Any) -> int:
    """Accept an integer, and never a boolean.

    `isinstance(True, int)` is true in Python, so a boolean answer would silently satisfy
    an integer variable and arrive in the contract as `True` rather than `1`.
    """
    if isinstance(raw, bool):
        raise ContractError(f"expected an integer, got a boolean ({raw!r})")
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except ValueError:
        raise ContractError(f"expected an integer, got {raw!r}") from None


def _coerce_number(_: Variable, raw: Any) -> float:
    """Accept any real number, and never a boolean, for the reason above."""
    if isinstance(raw, bool):
        raise ContractError(f"expected a number, got a boolean ({raw!r})")
    if isinstance(raw, int | float):
        return float(raw)
    try:
        return float(str(raw).strip())
    except ValueError:
        raise ContractError(f"expected a number, got {raw!r}") from None


# Table dispatch per §10.1: one entry per declared type, so a new type is a new row.
_COERCERS: Final[dict[str, Callable[[Variable, Any], Any]]] = {
    "string": _coerce_string,
    "integer": _coerce_integer,
    "number": _coerce_number,
    "boolean": _coerce_boolean,
    "choice": _coerce_choice,
}


def resolve(
    variables: tuple[Variable, ...],
    provided: Mapping[str, Any],
    *,
    interactive: bool = False,
    ask: Callable[[str], str] = input,
) -> dict[str, Any]:
    """Settle every variable, from what was supplied, from a person, or from its default.

    An answer supplied explicitly is never re-asked: a caller that passed `--set` has
    already answered, and prompting anyway is how a scripted run becomes an interactive one.
    """
    unknown = sorted(set(provided) - {variable.name for variable in variables})
    if unknown:
        declared = sorted(variable.name for variable in variables)
        raise ContractError(f"undeclared variable(s) {unknown}; declared: {declared}")
    return {variable.name: _resolve_one(variable, provided, interactive, ask) for variable in variables}


def _resolve_one(
    variable: Variable,
    provided: Mapping[str, Any],
    interactive: bool,
    ask: Callable[[str], str],
) -> Any:
    """Resolve a single variable from provided map, interactive prompt, or default."""
    if variable.name in provided:
        return _coerce_supplied(variable, provided[variable.name])
    if interactive:
        return _ask(variable, ask)
    return coerce(variable, variable.default)


def _coerce_supplied(variable: Variable, raw: Any) -> Any:
    """Coerce a supplied answer, naming the variable the value was supplied for."""
    try:
        return coerce(variable, raw)
    except ContractError as err:
        raise ContractError(f"{variable.name}: {err}") from None


def _prompt_one_attempt(variable: Variable, ask: Callable[[str], str]) -> tuple[bool, Any]:
    """Execute a single interactive prompt attempt, returning (success, value)."""
    try:
        entered = ask(variable.describe()).strip()
    except EOFError:
        return True, coerce(variable, variable.default)
    if not entered:
        return True, coerce(variable, variable.default)
    try:
        return True, coerce(variable, entered)
    except ContractError as err:
        print(f"  ↳ {err}")
        return False, None


def _ask(variable: Variable, ask: Callable[[str], str]) -> Any:
    """Ask until the answer converts, or until the attempt ceiling is reached.

    An empty line accepts the default, which is why a default is mandatory: pressing return
    is the commonest answer, and it must always mean something.
    """
    for _ in range(MAX_ATTEMPTS):
        success, value = _prompt_one_attempt(variable, ask)
        if success:
            return value
    raise ContractError(f"{variable.name}: no valid answer after {MAX_ATTEMPTS} attempts")


def placeholders(value: Any) -> set[str]:
    """Return every variable name referenced anywhere in a parsed document."""
    if isinstance(value, str):
        return set(_PLACEHOLDER.findall(value))
    if isinstance(value, dict):
        return set().union(*(placeholders(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(placeholders(item) for item in value)) if value else set()
    return set()


def render(document: Any, answers: Mapping[str, Any]) -> Any:
    """Substitute answers into every string leaf of a parsed document.

    Applied to the parsed structure rather than to the source text, so an answer cannot
    introduce a key, close a quote or start a list. Substitution is a single pass, so an
    answer that itself contains `{{ other }}` is inert text rather than another expansion.
    """
    missing = sorted(placeholders(document) - set(answers))
    if missing:
        raise ContractError(f"contract references undeclared variable(s) {missing}")
    return _substitute(document, answers)


def _substitute(value: Any, answers: Mapping[str, Any]) -> Any:
    """Walk the document, rewriting strings and leaving every other scalar alone."""
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda match: str(answers[match.group(1)]), value)
    if isinstance(value, dict):
        return {key: _substitute(item, answers) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, answers) for item in value]
    return value


def parse_assignments(assignments: Sequence[str]) -> dict[str, str]:
    """Parse `name=value` pairs from the command line, reporting every malformed one."""
    bad = [text for text in assignments if "=" not in text or not text.split("=", 1)[0].strip()]
    if bad:
        raise ContractError(f"expected NAME=VALUE, got {bad}")
    return {text.split("=", 1)[0].strip(): text.split("=", 1)[1] for text in assignments}
