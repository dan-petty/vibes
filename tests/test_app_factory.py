"""Tests for the application factory.

The claim worth testing is not that the factory writes files but that what it writes is
*already compliant*. Generated code fails gates in predictable ways — dispatchers that
branch past the complexity ceiling, modules with no docstring, READMEs with no structure —
and a generator whose output fails them hands its user a cleanup task instead of an
application. So the central test runs this repository's real instruments over the real
output: the AST invariant sentinel, ruff, mypy, the documentation validator, and the
generated pytest suite.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "examples" / "ast-invariant-sentinel"))

from app_factory import (
    ANSWERS_FILENAME,
    Contract,
    ContractError,
    generate,
    load_contract,
    main,
    schema_for,
)
from docs_validator import DocsValidator
from sentinel import audit_targets

WORKED_CONTRACT = REPO_ROOT / "artifacts" / "contracts" / "invoice-reconciler.yaml"

MINIMAL = {
    "name": "Demo",
    "slug": "demo-app",
    "module": "demo",
    "purpose": "Demonstrate the factory.",
    "operations": [{"name": "run", "summary": "Run it.", "arguments": []}],
}


def _contract_file(tmp_path: Path, **overrides: object) -> Path:
    """Write a contract with the given overrides applied to a valid baseline."""
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump({**MINIMAL, **overrides}), encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate the worked contract once, and gate the same output from several angles."""
    out = tmp_path_factory.mktemp("factory")
    contract = load_contract(WORKED_CONTRACT)
    generate(contract, out)
    return out / contract.slug


def test_generated_code_passes_the_ast_invariant_sentinel(generated: Path) -> None:
    """A generated dispatcher is exactly where complexity and nesting ceilings get breached."""
    report = audit_targets([generated])
    assert (report.violations, report.files_checked >= 3) == ([], True)


def test_the_generated_readme_passes_the_documentation_validator(generated: Path) -> None:
    """A README nobody can validate is the part of a scaffold that rots first."""
    assert DocsValidator().validate_file(generated / "README.md") == []


def test_generated_code_passes_ruff(generated: Path) -> None:
    """Import order and modernization are mechanical, so a generator has no excuse."""
    proc = _run([sys.executable, "-m", "ruff", "check", str(generated), "--isolated",
                 "--select", "E4,E7,E9,F,I,UP,B,SIM,RUF"])
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_generated_code_type_checks(generated: Path) -> None:
    """The contract declares types; the emitted annotations must agree with them."""
    cache = REPO_ROOT / ".mypy_cache"
    proc = _run([sys.executable, "-m", "mypy", "reconciler.py", "handlers.py", "--no-error-summary", f"--cache-dir={cache}"], cwd=generated)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_generated_suite_passes_unedited(generated: Path) -> None:
    """A scaffold that ships a red suite trains its user to ignore the suite."""
    test_path = generated / "test_reconciler.py"
    spec = importlib.util.spec_from_file_location("test_reconciler", test_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    test_funcs = [func for name, func in vars(mod).items() if name.startswith("test_") and callable(func)]
    assert len(test_funcs) >= 5
    for func in test_funcs:
        func()


def test_the_generated_application_runs_and_refuses_a_contract_breach(generated: Path) -> None:
    """Both problems at once: a caller correcting one argument per round trip is the cost avoided."""
    proc = _run(
        [sys.executable, "reconciler.py", "reconcile", "--arguments", '{"invoice_id": 7, "bogus": 1}'],
        cwd=generated,
    )
    assert (proc.returncode, "undeclared argument 'bogus'" in proc.stdout) == (1, True)
    assert "must be string, got int" in proc.stdout


def _run(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run one gate against generated output."""
    # Fixed argv, no shell; every path is one this test wrote.
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False, timeout=300)


def test_regenerating_never_overwrites_domain_logic(tmp_path: Path) -> None:
    """The one file a developer edits is the one file the factory refuses to touch twice."""
    contract = load_contract(_contract_file(tmp_path))
    generate(contract, tmp_path)
    handlers = tmp_path / contract.slug / "handlers.py"
    handlers.write_text("# my implementation\n", encoding="utf-8")
    result = generate(contract, tmp_path)
    assert (handlers.read_text(encoding="utf-8"), result.preserved) == ("# my implementation\n", [handlers])


def test_the_contract_layer_is_regenerated(tmp_path: Path) -> None:
    """Preserving handlers must not mean preserving a stale schema beside them."""
    contract = load_contract(_contract_file(tmp_path))
    generate(contract, tmp_path)
    module = tmp_path / contract.slug / "demo.py"
    module.write_text("# clobbered\n", encoding="utf-8")
    generate(contract, tmp_path)
    assert "clobbered" not in module.read_text(encoding="utf-8")


def test_every_operation_schema_forbids_undeclared_arguments() -> None:
    """§10.3. A schema that tolerates them cannot produce a correctable error."""
    schema = schema_for(load_contract(WORKED_CONTRACT))
    assert all(spec["additionalProperties"] is False for spec in schema["operations"].values())


def test_an_optional_argument_is_absent_from_required() -> None:
    """`required: false` has to reach the schema, or every call is rejected for missing it."""
    schema = schema_for(load_contract(WORKED_CONTRACT))
    assert schema["operations"]["reconcile"]["required"] == ["invoice_id"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"slug": "Not A Slug"}, "slug"),
        ({"module": "9lives"}, "module"),
        ({"purpose": ""}, "purpose"),
        ({"operations": []}, "at least one operation"),
        ({"operations": [{"name": "Run", "summary": "s"}]}, "lowercase identifier"),
        ({"operations": [{"name": "run", "summary": ""}]}, "summary is required"),
        ({"operations": [{"name": "run", "summary": "s"}, {"name": "run", "summary": "s"}]}, "duplicate"),
    ],
)
def test_a_contract_that_would_emit_broken_code_is_refused(
    tmp_path: Path, overrides: dict[str, object], expected: str
) -> None:
    """Refused at parse time, not discovered at the end of a generation run.

    A slug with a space produces a directory nothing can import and a duplicate operation
    silently loses a dispatch entry — defects a generator should make impossible rather
    than report.
    """
    with pytest.raises(ContractError, match=expected):
        load_contract(_contract_file(tmp_path, **overrides))


def test_an_unknown_argument_type_is_refused(tmp_path: Path) -> None:
    """The type table drives the schema, the annotation and the runtime check at once."""
    operations = [{"name": "run", "summary": "s", "arguments": [{"name": "a", "type": "widget"}]}]
    with pytest.raises(ContractError, match="unknown argument type"):
        load_contract(_contract_file(tmp_path, operations=operations))


def test_a_duplicate_argument_is_refused(tmp_path: Path) -> None:
    """Two arguments of one name collapse into one schema property and one silently vanishes."""
    argument = {"name": "a", "type": "string"}
    operations = [{"name": "run", "summary": "s", "arguments": [argument, argument]}]
    with pytest.raises(ContractError, match="duplicate argument"):
        load_contract(_contract_file(tmp_path, operations=operations))


def test_the_worked_contract_declares_more_than_the_simplest_case() -> None:
    """The acid test is only worth what the contract behind it exercises."""
    contract: Contract = load_contract(WORKED_CONTRACT)
    optional = [a for op in contract.operations for a in op.arguments if not a.required]
    assert (len(contract.operations) >= 2, len(optional) >= 1) == (True, True)


# --- Variables reach the emitted application -------------------------------------------


def test_an_answer_reaches_the_generated_module_and_readme(tmp_path: Path) -> None:
    """A variable nothing renders is a question asked for no reason."""
    contract = load_contract(WORKED_CONTRACT, {"entity": "shipment"})
    generate(contract, tmp_path)
    module = (tmp_path / contract.slug / "reconciler.py").read_text(encoding="utf-8")
    readme = (tmp_path / contract.slug / "README.md").read_text(encoding="utf-8")
    assert ("Reconcile submitted shipments" in module, "shipment" in readme) == (True, True)


def test_the_answers_are_recorded_beside_the_application_and_replay(tmp_path: Path) -> None:
    """A regeneration that repeats the dialogue is a regeneration nobody runs twice."""
    generate(load_contract(WORKED_CONTRACT, {"entity": "shipment"}), tmp_path)
    recorded = tmp_path / "invoice-reconciler" / ANSWERS_FILENAME
    replayed = load_contract(WORKED_CONTRACT, yaml.safe_load(recorded.read_text(encoding="utf-8")))
    assert replayed.answers["entity"] == "shipment"


def test_generating_never_prompts_without_a_terminal(tmp_path: Path) -> None:
    """The failure this prevents is not an error but a wait, on the machine least able to answer.

    pytest's stdin is not a terminal, so a factory that prompted unconditionally would hang
    this test rather than fail it — which is exactly how it would behave in CI.
    """
    code = main(["new", "--contract", str(WORKED_CONTRACT), "--out", str(tmp_path)])
    assert (code, (tmp_path / "invoice-reconciler" / "reconciler.py").exists()) == (0, True)


def test_a_contract_gated_after_rendering_still_passes_the_sentinel(tmp_path: Path) -> None:
    """The acid test has to run on rendered output, or it only ever gated the defaults."""
    contract = load_contract(WORKED_CONTRACT, {"entity": "consignment", "tolerance_unit": "percent"})
    generate(contract, tmp_path)
    report = audit_targets([tmp_path / contract.slug])
    assert (report.violations, report.files_checked >= 3) == ([], True)


def test_a_variable_that_renders_an_unusable_slug_is_refused(tmp_path: Path) -> None:
    """Validation runs on the rendered contract, so an answer is judged where it lands."""
    path = _contract_file(tmp_path, slug="{{ target }}", variables=[
        {"name": "target", "prompt": "Where", "default": "demo-app"},
    ])
    load_contract(path)
    with pytest.raises(ContractError, match="slug"):
        load_contract(path, {"target": "Not A Slug"})
