#!/usr/bin/env python3
"""Turn an application contract into a runnable application that is born passing the gates.

This repository can measure code, judge code and fuzz the things that judge code. It had
no way to *produce* one, which is how ninety-three percent of a release came to be spent
on instruments — see [`portfolio_balance.py`](./portfolio_balance.py) and §1.

What makes this a factory for *this* repository rather than a template renderer is the
acceptance test: everything it emits passes the AST invariant sentinel, ruff, mypy, the
documentation validator and its own generated pytest suite, with no edit. A generator whose
output fails the gates is a generator that hands its user a cleanup task, and generated code
fails them in predictable ways — dispatchers that branch past $M \\le 10$, modules with no
docstrings, READMEs with no structure. `tests/test_app_factory.py` runs the real gates over
the real output, so that claim is executed rather than asserted.

One declaration drives three artefacts that otherwise drift: the dispatch table, the JSON
Schema arguments are validated against, and the tests that assert an undeclared argument is
refused. Hand-written they agree exactly until someone adds a parameter. §10.3 requires
`additionalProperties: false` on every tool contract, and this is where that becomes
structural rather than remembered.

What it deliberately does not generate is behaviour. `handlers.py` is emitted once with
typed stubs and never overwritten; the contract layer around it is regenerated freely. A
factory that owns the domain logic becomes a framework nobody can leave.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml
from contract_variables import (
    ContractError,
    Variable,
    load_variables,
    parse_assignments,
    render,
    resolve,
)

__all__ = [
    "Argument",
    "Contract",
    "ContractError",
    "Generated",
    "Operation",
    "emit_handlers",
    "emit_module",
    "emit_readme",
    "emit_tests",
    "generate",
    "load_contract",
    "schema_for",
]

# JSON Schema primitives, mapped to the Python types the generated runtime checks against
# and to the annotations it writes. One table, so the schema and the runtime cannot drift.
TYPE_MAP: Final[dict[str, tuple[str, str]]] = {
    "string": ("str", "str"),
    "number": ("float", "(int, float)"),
    "integer": ("int", "int"),
    "boolean": ("bool", "bool"),
    "array": ("list[Any]", "list"),
    "object": ("dict[str, Any]", "dict"),
}

_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*$")
_SLUG: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class Argument:
    """One declared argument of one operation."""

    name: str
    type: str
    description: str
    required: bool = True

    @property
    def annotation(self) -> str:
        """Return the Python annotation the generated signature carries."""
        return TYPE_MAP[self.type][0]


@dataclass(frozen=True)
class Operation:
    """One thing the generated application can be asked to do."""

    name: str
    summary: str
    arguments: tuple[Argument, ...] = ()

    @property
    def required(self) -> list[str]:
        """Return the names the contract demands."""
        return [argument.name for argument in self.arguments if argument.required]


@dataclass(frozen=True)
class Contract:
    """A whole application, as declared and as answered."""

    name: str
    slug: str
    module: str
    purpose: str
    operations: tuple[Operation, ...]
    variables: tuple[Variable, ...] = ()
    answers: Mapping[str, Any] = field(default_factory=dict)


def load_contract(
    path: Path,
    provided: Mapping[str, Any] | None = None,
    *,
    interactive: bool = False,
    ask: Callable[[str], str] = input,
) -> Contract:
    """Parse, answer, render and validate a contract, refusing anything that would emit
    invalid Python.

    The order matters. Answers are substituted into the *parsed* document, so a value
    carrying a colon or a brace cannot restructure it, and validation runs on the rendered
    result, so a variable that produces an unusable slug is refused here rather than
    discovered as a directory nothing can import.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ContractError(f"{path}: expected a mapping at the top level")
    variables = load_variables(document)
    answers = resolve(variables, provided or {}, interactive=interactive, ask=ask)
    rendered = render({key: value for key, value in document.items() if key != "variables"}, answers)
    contract = Contract(
        name=str(rendered.get("name", "")).strip(),
        slug=str(rendered.get("slug", "")).strip(),
        module=str(rendered.get("module", "")).strip(),
        purpose=str(rendered.get("purpose", "")).strip(),
        operations=tuple(_operation(entry) for entry in rendered.get("operations", [])),
        variables=variables,
        answers=answers,
    )
    _reject_invalid(contract)
    return contract


def _operation(entry: Any) -> Operation:
    """Parse one operation entry."""
    if not isinstance(entry, dict):
        raise ContractError(f"expected a mapping per operation, got {type(entry).__name__}")
    return Operation(
        name=str(entry.get("name", "")).strip(),
        summary=str(entry.get("summary", "")).strip(),
        arguments=tuple(_argument(item) for item in entry.get("arguments", [])),
    )


def _argument(item: Any) -> Argument:
    """Parse one argument entry, rejecting a type the schema cannot express."""
    if not isinstance(item, dict):
        raise ContractError(f"expected a mapping per argument, got {type(item).__name__}")
    declared = str(item.get("type", "")).strip()
    if declared not in TYPE_MAP:
        raise ContractError(f"unknown argument type {declared!r}; allowed: {sorted(TYPE_MAP)}")
    return Argument(
        name=str(item.get("name", "")).strip(),
        type=declared,
        description=str(item.get("description", "")).strip(),
        required=bool(item.get("required", True)),
    )


def _reject_invalid(contract: Contract) -> None:
    """Refuse a contract that would emit code this repository's own gates would reject.

    Checked here rather than discovered at the end of a generation run: a slug with a
    space produces a directory nothing can import, and a duplicate operation name silently
    loses an entry in the dispatch table, which is the kind of defect a generator should
    make impossible rather than report.
    """
    problems = _naming_problems(contract) + _operation_problems(contract)
    if problems:
        raise ContractError("; ".join(problems))


def _naming_problems(contract: Contract) -> list[str]:
    """Return every breach of the naming rules the emitted files depend on."""
    problems: list[str] = []
    if not _SLUG.match(contract.slug):
        problems.append(f"slug {contract.slug!r} must match {_SLUG.pattern}")
    if not _IDENTIFIER.match(contract.module):
        problems.append(f"module {contract.module!r} must be a lowercase Python identifier")
    if not contract.purpose:
        problems.append("purpose is required; it becomes the module docstring")
    return problems


def _operation_problems(contract: Contract) -> list[str]:
    """Return every breach in the operation list itself."""
    problems: list[str] = []
    if not contract.operations:
        problems.append("at least one operation is required")
    names = [operation.name for operation in contract.operations]
    problems += [f"operation name {n!r} must be a lowercase identifier" for n in names if not _IDENTIFIER.match(n)]
    problems += [f"duplicate operation {n!r}" for n in sorted({n for n in names if names.count(n) > 1})]
    for operation in contract.operations:
        problems += _argument_problems(operation)
    return problems


def _argument_problems(operation: Operation) -> list[str]:
    """Return every breach in one operation's argument list."""
    names = [argument.name for argument in operation.arguments]
    problems = [f"{operation.name}: argument {n!r} must be a lowercase identifier" for n in names if not _IDENTIFIER.match(n)]
    problems += [f"{operation.name}: duplicate argument {n!r}" for n in sorted({n for n in names if names.count(n) > 1})]
    if not operation.summary:
        problems.append(f"{operation.name}: summary is required; it becomes the docstring")
    return problems


def schema_for(contract: Contract) -> dict[str, Any]:
    """Build the JSON Schema the generated runtime validates arguments against.

    `additionalProperties: false` on every operation, per §10.3. A schema that tolerates
    undeclared arguments cannot produce the prescriptive error that lets a model correct
    itself zero-shot, because it never notices there was anything to correct.
    """
    return {
        "operations": {
            operation.name: {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    argument.name: {"type": argument.type, "description": argument.description}
                    for argument in operation.arguments
                },
                "required": operation.required,
            }
            for operation in contract.operations
        }
    }


# --- Emission -------------------------------------------------------------------------
#
# Built as line lists rather than formatted templates. The generated code contains dict
# literals, f-strings and type parameters, so every `{` in a `.format` template would need
# doubling — and a missed one produces a file that fails to parse at generation time
# instead of a template that fails to render.


def _signature(operation: Operation) -> str:
    """Return the handler signature the contract implies."""
    parts = [f"{a.name}: {a.annotation}" for a in operation.arguments if a.required]
    parts += [f"{a.name}: {a.annotation} | None = None" for a in operation.arguments if not a.required]
    return f"def {operation.name}({', '.join(parts)}) -> dict[str, Any]:"


def emit_handlers(contract: Contract) -> str:
    """Emit the domain logic stubs, which are written once and never regenerated."""
    lines = [
        '"""Domain logic for ' + contract.name + ".",
        "",
        "Written once by `tools/app_factory.py` and never overwritten: regenerating the contract",
        "layer leaves this file alone, so filling a stub in is not a thing the factory can undo.",
        "",
        "Each signature is the contract. Arguments are validated before a handler is called, so a",
        "value that arrives here is already the type it claims to be; changing a signature means",
        "changing the contract and regenerating.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
    ]
    for operation in contract.operations:
        lines += [
            "",
            _signature(operation),
            f'    """{operation.summary}"""',
            "    # TODO: implement. Returning the declared shape keeps the generated suite green",
            "    # until this is filled in, and keeps the application runnable in the meantime.",
            f'    return {{"status": "unimplemented", "operation": "{operation.name}"}}',
        ]
    return "\n".join(lines) + "\n"


def _schema_literal(contract: Contract) -> list[str]:
    """Render the schema as an indented Python literal."""
    rendered = json.dumps(schema_for(contract), indent=4).replace(": false", ": False")
    return ["SCHEMA: Final[dict[str, Any]] = " + rendered]


def _dispatch_literal(contract: Contract) -> list[str]:
    """Render the dispatch table, which §10.1 requires instead of a branch ladder."""
    lines = ["_DISPATCH: Final[dict[str, Callable[..., dict[str, Any]]]] = {"]
    lines += [f'    "{operation.name}": handlers.{operation.name},' for operation in contract.operations]
    return [*lines, "}"]


def emit_module(contract: Contract) -> str:
    """Emit the contract layer: schema, validation, dispatch, timing and CLI."""
    lines = [
        "#!/usr/bin/env python3",
        '"""' + contract.purpose,
        "",
        "Generated by `tools/app_factory.py`. This file is the contract layer and is rewritten on",
        "every regeneration; the domain logic lives in `handlers.py`, which is not.",
        "",
        "Arguments are checked against the schema before dispatch, and an operation that breaches",
        "its contract is refused with every problem listed at once rather than the first — a caller",
        "correcting one argument per round trip is the cost that prescriptive errors exist to avoid.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import argparse",
        "import json",
        "import sys",
        "import time",
        "from collections.abc import Callable, Sequence",
        "from dataclasses import asdict, dataclass",
        "from typing import Any, Final",
        "",
        "import handlers",
        "",
        *_schema_literal(contract),
        "",
        "# JSON Schema primitives to the Python types this runtime accepts for them.",
        "_RUNTIME_TYPES: Final[dict[str, Any]] = {",
        '    "string": str,',
        '    "number": (int, float),',
        '    "integer": int,',
        '    "boolean": bool,',
        '    "array": list,',
        '    "object": dict,',
        "}",
        "",
        "",
        "@dataclass(frozen=True)",
        "class OperationResult:",
        '    """One invocation: what was asked, what came back, and how long it took."""',
        "",
        "    operation: str",
        "    status: str",
        "    payload: dict[str, Any]",
        "    elapsed_ms: float",
        "",
        "    def to_json(self) -> dict[str, Any]:",
        '        """Render as one trace record."""',
        "        return asdict(self)",
        "",
        "",
        "def _matches(expected: str, value: Any) -> bool:",
        '    """Report whether a value satisfies a declared JSON Schema type.',
        "",
        "    `bool` is a subclass of `int` in Python, so a plain isinstance check accepts `True`",
        "    where the contract declared an integer. JSON does not agree that those are the same",
        "    thing, and neither does the schema this validates against.",
        '    """',
        '    if expected == "boolean":',
        "        return isinstance(value, bool)",
        "    if isinstance(value, bool):",
        "        return False",
        "    return isinstance(value, _RUNTIME_TYPES[expected])",
        "",
        "",
        "def _type_problems(declared: dict[str, Any], arguments: dict[str, Any]) -> list[str]:",
        '    """Return every argument whose value does not match its declared type."""',
        "    found: list[str] = []",
        "    for name, value in sorted(arguments.items()):",
        '        expected = declared.get(name, {}).get("type")',
        "        if expected is not None and not _matches(expected, value):",
        "            found.append(f\"argument {name!r} must be {expected}, got {type(value).__name__}\")",
        "    return found",
        "",
        "",
        "def contract_problems(operation: str, arguments: dict[str, Any]) -> list[str]:",
        '    """Return every way the call breaches its contract, so one round trip can fix all of them."""',
        '    spec = SCHEMA["operations"].get(operation)',
        "    if spec is None:",
        "        declared = sorted(SCHEMA[\"operations\"])",
        "        return [f\"unknown operation {operation!r}; declared: {declared}\"]",
        '    properties = spec["properties"]',
        "    problems = [",
        "        f\"undeclared argument {name!r}; allowed: {sorted(properties)}\"",
        "        for name in sorted(arguments)",
        "        if name not in properties",
        "    ]",
        "    problems += [",
        "        f\"missing required argument {name!r}\"",
        '        for name in spec["required"]',
        "        if name not in arguments",
        "    ]",
        "    return problems + _type_problems(properties, arguments)",
        "",
        "",
        *_dispatch_literal(contract),
        "",
        "",
        "def invoke(operation: str, arguments: dict[str, Any]) -> OperationResult:",
        '    """Validate against the contract, dispatch, and time the call."""',
        "    started = time.perf_counter()",
        "    problems = contract_problems(operation, arguments)",
        "    if problems:",
        "        elapsed = (time.perf_counter() - started) * 1000",
        '        return OperationResult(operation, "rejected", {"problems": problems}, round(elapsed, 3))',
        "    payload = _DISPATCH[operation](**arguments)",
        "    elapsed = (time.perf_counter() - started) * 1000",
        '    return OperationResult(operation, "ok", payload, round(elapsed, 3))',
        "",
        "",
        "def build_arg_parser() -> argparse.ArgumentParser:",
        '    """Construct the CLI parser."""',
        "    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])",
        '    parser.add_argument("operation", choices=sorted(SCHEMA["operations"]))',
        '    parser.add_argument("--arguments", default="{}", help="JSON object of arguments")',
        "    return parser",
        "",
        "",
        "def main(argv: Sequence[str] | None = None) -> int:",
        '    """CLI entry point. Exits non-zero when the call breached its contract."""',
        "    args = build_arg_parser().parse_args(argv)",
        "    result = invoke(args.operation, json.loads(args.arguments))",
        "    print(json.dumps(result.to_json(), indent=2))",
        '    return 0 if result.status == "ok" else 1',
        "",
        "",
        'if __name__ == "__main__":',
        "    sys.exit(main(sys.argv[1:]))",
    ]
    return "\n".join(lines) + "\n"


SAMPLE_VALUES: Final[dict[str, str]] = {
    "string": '"sample"',
    "number": "1.5",
    "integer": "1",
    "boolean": "True",
    "array": "[]",
    "object": "{}",
}

# A value of the wrong type for each declared type, used by the generated negative tests.
# `True` against `integer` is deliberate: it is the case a plain isinstance check accepts.
WRONG_VALUES: Final[dict[str, str]] = {
    "string": "1",
    "number": '"not a number"',
    "integer": "True",
    "boolean": '"not a boolean"',
    "array": '"not an array"',
    "object": '"not an object"',
}


def _valid_arguments(operation: Operation) -> str:
    """Render a minimal argument mapping that satisfies one operation's contract."""
    pairs = [f'"{a.name}": {SAMPLE_VALUES[a.type]}' for a in operation.arguments if a.required]
    return "{" + ", ".join(pairs) + "}"


def emit_tests(contract: Contract) -> str:
    """Emit the contract tests: what the application must refuse, and what it must accept."""
    first = contract.operations[0]
    typed = [argument for argument in first.arguments if argument.required]
    lines = [
        '"""Contract tests for ' + contract.name + ".",
        "",
        "Generated by `tools/app_factory.py`. These assert the contract rather than the domain",
        "logic: that an undeclared argument is refused, that a missing one is, that a wrong type",
        "does not reach a handler, and that every declared operation can actually be dispatched.",
        "They keep passing as `handlers.py` is filled in, so add tests beside them rather than",
        "replacing them.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import sys",
        "from pathlib import Path",
        "",
        "sys.path.insert(0, str(Path(__file__).resolve().parent))",
        "",
        f"import {contract.module}",
        "",
        "",
        "def test_every_declared_operation_can_be_dispatched() -> None:",
        '    """A declared operation with no handler is a contract the application cannot honour."""',
        f'    assert sorted({contract.module}._DISPATCH) == sorted({contract.module}.SCHEMA["operations"])',
        "",
        "",
        "def test_an_undeclared_argument_is_refused() -> None:",
        '    """§10.3: a schema that tolerates undeclared arguments cannot prompt a correction."""',
        f'    arguments = dict({_valid_arguments(first)}, undeclared_argument="x")',
        f'    result = {contract.module}.invoke("{first.name}", arguments)',
        '    assert result.status == "rejected"',
        "    assert any(\"undeclared_argument\" in problem for problem in result.payload[\"problems\"])",
        "",
        "",
        "def test_an_unknown_operation_names_the_ones_that_exist() -> None:",
        '    """A rejection that does not say what was allowed costs another round trip to find out."""',
        f'    result = {contract.module}.invoke("no_such_operation", {{}})',
        '    assert result.status == "rejected"',
        f'    assert "{first.name}" in result.payload["problems"][0]',
        "",
        "",
        "def test_a_valid_call_reaches_its_handler_and_is_timed() -> None:",
        '    """The positive path, so the negative tests above are not the only thing exercised."""',
        f'    result = {contract.module}.invoke("{first.name}", {_valid_arguments(first)})',
        '    assert result.status == "ok"',
        "    assert result.elapsed_ms >= 0.0",
    ]
    lines += _negative_type_test(contract, first, typed)
    lines += _missing_argument_test(contract, first, typed)
    return "\n".join(lines) + "\n"


def _negative_type_test(contract: Contract, operation: Operation, typed: list[Argument]) -> list[str]:
    """Emit the wrong-type test, when the operation has a required argument to get wrong."""
    if not typed:
        return []
    argument = typed[0]
    return [
        "",
        "",
        "def test_an_argument_of_the_wrong_type_is_refused() -> None:",
        '    """Declared types are enforced at the boundary, not assumed inside the handler."""',
        f'    arguments = dict({_valid_arguments(operation)}, {argument.name}={WRONG_VALUES[argument.type]})',
        f'    result = {contract.module}.invoke("{operation.name}", arguments)',
        '    assert result.status == "rejected"',
        f'    assert any("{argument.name}" in problem for problem in result.payload["problems"])',
    ]


def _missing_argument_test(contract: Contract, operation: Operation, typed: list[Argument]) -> list[str]:
    """Emit the missing-argument test, when the operation requires anything at all."""
    if not typed:
        return []
    return [
        "",
        "",
        "def test_a_missing_required_argument_is_refused() -> None:",
        '    """Required means required; defaulting it silently would hide the caller\'s mistake."""',
        f'    result = {contract.module}.invoke("{operation.name}", {{}})',
        '    assert result.status == "rejected"',
        f'    assert any("{typed[0].name}" in problem for problem in result.payload["problems"])',
    ]


def emit_readme(contract: Contract) -> str:
    """Emit a README the documentation validator accepts without editing."""
    lines = [
        f"# {contract.name}",
        "",
        contract.purpose,
        "",
        f"Generated by `tools/app_factory.py`. The contract layer in `{contract.module}.py` is",
        "regenerated on demand; `handlers.py` is written once and holds the domain logic.",
        "",
        "## Operations",
        "",
        "| Operation | Arguments | Summary |",
        "|---|---|---|",
    ]
    for operation in contract.operations:
        rendered = ", ".join(
            f"`{a.name}: {a.type}`" + ("" if a.required else " *(optional)*") for a in operation.arguments
        )
        lines.append(f"| `{operation.name}` | {rendered or '—'} | {operation.summary} |")
    first = contract.operations[0]
    lines += [
        "",
        "## Running it",
        "",
        "```bash",
        f"python3 {contract.module}.py {first.name} --arguments '{_json_arguments(first)}'",
        f"pytest test_{contract.module}.py",
        "```",
        "",
        "## What is enforced",
        "",
        "Arguments are validated against the emitted JSON Schema before any handler is called, and",
        "the schema sets `additionalProperties: false`, so an undeclared argument is refused rather",
        "than ignored. A rejection lists every problem at once: a caller that has to correct one",
        "argument per round trip is the cost that prescriptive errors exist to remove.",
        "",
        "Every declared operation is asserted to be dispatchable, so a contract the application",
        "cannot honour fails a test rather than a call.",
    ]
    return "\n".join(lines) + "\n"


def _json_arguments(operation: Operation) -> str:
    """Render a valid argument object as JSON, for the README's example invocation."""
    literal = {
        "string": "sample", "number": 1.5, "integer": 1,
        "boolean": True, "array": [], "object": {},
    }
    return json.dumps({a.name: literal[a.type] for a in operation.arguments if a.required})


# --- Generation and CLI ---------------------------------------------------------------

# `handlers.py` is deliberately absent. Regenerating must never overwrite domain logic, so
# the one file a developer edits is the one file the factory refuses to touch twice.
REGENERATED: Final[tuple[str, ...]] = ("module", "tests", "readme")

# Recorded beside the application so a regeneration can replay the dialogue instead of
# repeating it. `copier` writes `.copier-answers.yml` for the same reason, and it is what
# re-applying a contract to an existing project will need when that lands.
ANSWERS_FILENAME: Final[str] = ".factory-answers.yaml"


@dataclass
class Generated:
    """What one generation run wrote, and what it left alone."""

    written: list[Path] = field(default_factory=list)
    preserved: list[Path] = field(default_factory=list)


def generate(contract: Contract, out_dir: Path) -> Generated:
    """Write the application, preserving any handlers that already exist."""
    target = out_dir / contract.slug
    target.mkdir(parents=True, exist_ok=True)
    result = Generated()
    files = {
        target / f"{contract.module}.py": emit_module(contract),
        target / f"test_{contract.module}.py": emit_tests(contract),
        target / "README.md": emit_readme(contract),
    }
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
        result.written.append(path)
    handlers = target / "handlers.py"
    if handlers.exists():
        result.preserved.append(handlers)
    else:
        handlers.write_text(emit_handlers(contract), encoding="utf-8")
        result.written.append(handlers)
    if contract.variables:
        answers = target / ANSWERS_FILENAME
        answers.write_text(yaml.safe_dump(dict(contract.answers), sort_keys=True), encoding="utf-8")
        result.written.append(answers)
    return result


def _supplied(args: argparse.Namespace) -> dict[str, Any]:
    """Merge answers from a file with answers from the command line.

    `--set` wins, because it is the more specific of the two and the one typed most
    recently: a recorded file is a starting point and a flag is a correction to it.
    """
    from_file: dict[str, Any] = {}
    if args.answers:
        loaded = yaml.safe_load(args.answers.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ContractError(f"{args.answers}: expected a mapping of answers")
        from_file = {str(key): value for key, value in loaded.items()}
    return {**from_file, **parse_assignments(args.set or [])}


def _interactive(args: argparse.Namespace) -> bool:
    """Decide whether to ask, erring towards not asking.

    Never prompt without a terminal on the other end. A generator that prompts into a pipe
    does not fail, it waits, and it waits on the machine least able to answer it — which
    is why every variable carries a default and this returns False whenever it is unsure.
    """
    return not args.no_input and sys.stdin.isatty()


def _handle_new(args: argparse.Namespace) -> int:
    """Generate an application from a contract."""
    contract = load_contract(args.contract, _supplied(args), interactive=_interactive(args))
    result = generate(contract, args.out)
    for path in result.written:
        print(f"  wrote     {path}")
    for path in result.preserved:
        print(f"  preserved {path}  (domain logic is never regenerated)")
    print(f"{contract.name}: {len(result.written)} file(s) written.")
    if contract.variables:
        print(f"Replay: --answers {args.out / contract.slug / ANSWERS_FILENAME}")
    print(f"Next: cd {args.out / contract.slug} && pytest test_{contract.module}.py")
    return 0


def _handle_validate(args: argparse.Namespace) -> int:
    """Check a contract without writing anything, resolving variables but never asking."""
    supplied = _supplied(args)
    contract = load_contract(args.contract, supplied)
    operations = ", ".join(operation.name for operation in contract.operations)
    for variable in contract.variables:
        source = "supplied" if variable.name in supplied else "default"
        print(f"  {variable.name} = {contract.answers[variable.name]!r}  ({source})")
    print(f"{contract.name} ({contract.slug}): {len(contract.operations)} operation(s): {operations}")
    return 0


_HANDLERS: Final[dict[str, Any]] = {"new": _handle_new, "validate": _handle_validate}


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser for the application factory."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=sorted(_HANDLERS))
    parser.add_argument("--contract", type=Path, required=True, help="Contract YAML to build from")
    parser.add_argument("--out", type=Path, default=Path("."), help="Directory to generate into")
    parser.add_argument("--answers", type=Path, help="YAML file of answers to the contract's variables")
    parser.add_argument("--set", action="append", metavar="NAME=VALUE", help="Answer one variable")
    parser.add_argument("--no-input", action="store_true", help="Never prompt; take defaults")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the application factory."""
    args = build_arg_parser().parse_args(argv)
    try:
        return int(_HANDLERS[args.command](args))
    except ContractError as err:
        print(f"❌ {args.contract}: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
