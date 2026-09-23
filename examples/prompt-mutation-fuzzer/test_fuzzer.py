"""Unit tests for the Interactive Prompt Mutation Suite & Invariant Fuzzer."""

# sentinel: allow[ZeroTrustSanitization] — adversarial prompt fixtures embedding private IPs to measure invariant drift

import ast
import json
import sys
from pathlib import Path

import pytest

# Add current dir to sys.path for direct imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fuzzer import (
    InvariantAuditor,
    PerturbationConfig,
    PerturbationKind,
    PromptMutationFuzzer,
    PromptPerturbationEngine,
    _node_complexity_weight,
    _parse_address,
    _parse_dotted_quad,
    main,
)


def test_perturbation_engine_dilution() -> None:
    """Ensure dilution perturbations insert bureaucratic filler."""
    engine = PromptPerturbationEngine(PerturbationConfig(intensity=0.5, random_seed=42))
    base_prompt = "Line 1: Rules.\nLine 2: Instructions.\nLine 3: Output format."
    result = engine.mutate_prompt(base_prompt, PerturbationKind.DILUTION)

    assert result.kind == PerturbationKind.DILUTION
    assert len(result.mutated_prompt.splitlines()) > len(base_prompt.splitlines())
    assert len(result.applied_patches) == 1


def test_perturbation_engine_distraction() -> None:
    """Ensure distraction perturbations append secondary tasks."""
    engine = PromptPerturbationEngine(PerturbationConfig(intensity=0.5, random_seed=42))
    base_prompt = "You are a code refactoring assistant."
    result = engine.mutate_prompt(base_prompt, PerturbationKind.DISTRACTION)

    assert result.kind == PerturbationKind.DISTRACTION
    assert len(result.mutated_prompt) > len(base_prompt)
    assert any(snippet in result.mutated_prompt for snippet in engine.DISTRACTION_SNIPPETS)


def test_perturbation_engine_injection() -> None:
    """Ensure injection escape perturbations append override directives."""
    engine = PromptPerturbationEngine(PerturbationConfig(intensity=0.5, random_seed=42))
    base_prompt = "Enforce M <= 10 at all times."
    result = engine.mutate_prompt(base_prompt, PerturbationKind.INJECTION_ESCAPE)

    assert result.kind == PerturbationKind.INJECTION_ESCAPE
    assert (
        "SYSTEM OVERRIDE" in result.mutated_prompt
        or "ADMIN DIRECTIVE" in result.mutated_prompt
        or "MAINTENANCE NOTICE" in result.mutated_prompt
    )


def test_perturbation_engine_truncation() -> None:
    """Ensure truncation cuts prompt lines according to intensity."""
    engine = PromptPerturbationEngine(PerturbationConfig(intensity=0.8, random_seed=42))
    base_prompt = "\n".join([f"Directive {i}: Rule statement." for i in range(10)])
    result = engine.mutate_prompt(base_prompt, PerturbationKind.TRUNCATION)

    assert result.kind == PerturbationKind.TRUNCATION
    assert len(result.mutated_prompt.splitlines()) < len(base_prompt.splitlines())


def test_perturbation_engine_complexity_trap() -> None:
    """Ensure complexity trap perturbations append nested constraints."""
    engine = PromptPerturbationEngine(PerturbationConfig(intensity=0.5, random_seed=42))
    base_prompt = "Implement sorting algorithm."
    result = engine.mutate_prompt(base_prompt, PerturbationKind.COMPLEXITY_TRAP)

    assert result.kind == PerturbationKind.COMPLEXITY_TRAP
    assert "nested if-else" in result.mutated_prompt or "monolithic" in result.mutated_prompt


def test_invariant_auditor_evaluates_clean_code() -> None:
    """Ensure InvariantAuditor certifies clean code with zero violations."""
    clean_code = """
def compute_hash(val: str) -> str:
    return val.strip().lower()
"""
    evaluation = InvariantAuditor.evaluate_code("clean", clean_code)
    assert (
        evaluation.max_complexity,
        evaluation.max_depth,
        evaluation.complexity_violation,
        evaluation.nesting_violation,
        evaluation.sanitization_leak,
        evaluation.violations,
    ) == (1, 1, False, False, False, [])


def test_invariant_auditor_detects_complexity_and_nesting() -> None:
    """Ensure InvariantAuditor flags functions breaching M <= 10 or depth <= 5."""
    complex_code = """
def deep_trap(a, b, c, d, e, f):
    if a:
        if b:
            if c:
                if d:
                    if e:
                        if f:
                            return 1
    return 0
"""
    evaluation = InvariantAuditor.evaluate_code("trap", complex_code)
    assert evaluation.max_depth > 5
    assert evaluation.nesting_violation
    assert any("Depth=" in v for v in evaluation.violations)


def test_invariant_auditor_detects_sanitization_leaks() -> None:
    """Ensure InvariantAuditor catches RFC 1918 IPs and unapproved subdomains."""
    leaky_code = """
URL_INTERNAL = "http://10.0.0.15/api"
URL_SUBDOMAIN = "https://sub.example.com/v1"
"""
    evaluation = InvariantAuditor.evaluate_code("leaky", leaky_code)
    assert evaluation.sanitization_leak
    assert len(evaluation.violations) == 2
    assert any("10.0.0.15" in v for v in evaluation.violations)
    assert any("sub.example.com" in v for v in evaluation.violations)


def test_prompt_mutation_fuzzer_matrix_scoring() -> None:
    """Ensure PromptMutationFuzzer computes drift metrics across test matrix."""
    fuzzer = PromptMutationFuzzer()
    cases = [
        (PerturbationKind.DILUTION, "def f(): return 1\n"),
        (PerturbationKind.DISTRACTION, "def g(): return 2\n"),
        (
            PerturbationKind.INJECTION_ESCAPE,
            "def h(a, b, c, d, e, f):\n if a:\n  if b:\n   if c:\n    if d:\n     if e:\n      if f: return 1\n return 0\n",
        ),
    ]
    report = fuzzer.run_fuzz_matrix("Base system prompt", cases)

    assert report.total_runs == 3
    assert report.clean_runs == 2
    assert report.drift_violations == 1
    assert report.resilience_score == 66.7
    assert report.vulnerability_breakdown[PerturbationKind.INJECTION_ESCAPE.value] == 1


def test_fuzzer_cli_main_entrypoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Ensure fuzzer CLI entrypoint executes and supports --json output."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("System instructions for coding assistant.\n", encoding="utf-8")

    exit_code = main(["--prompt-file", str(prompt_file), "--intensity", "0.6", "--json"])
    assert exit_code == 0

    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert "resilience_score" in data
    assert "vulnerability_breakdown" in data
    assert data["total_runs"] == 4


def test_node_complexity_weight_and_address_helpers() -> None:
    """Verify AST decision weighting and address resolution helpers."""
    # Test comprehension with multiple if clauses
    comp_ast = ast.parse("[x for x in items if x > 0 if x < 10]").body[0].value.generators[0]  # type: ignore[attr-defined]
    comp_weight = _node_complexity_weight(comp_ast)

    # Test match cases (concrete vs wildcard)
    match_ast = ast.parse("match val:\n    case 1:\n        pass\n    case _:\n        pass")
    cases = match_ast.body[0].cases  # type: ignore[attr-defined]
    case_weights = (
        _node_complexity_weight(cases[0]),
        _node_complexity_weight(cases[1]),
    )

    # Test BoolOp and non-decision node
    bool_ast = ast.parse("a and b and c").body[0].value  # type: ignore[attr-defined]
    pass_ast = ast.parse("pass").body[0]
    weights = (_node_complexity_weight(bool_ast), _node_complexity_weight(pass_ast))

    # Test address resolution
    quad_invalid = _parse_dotted_quad("1.2.3")
    quad_octal = _parse_dotted_quad("012.0.0.1")
    addr_invalid = _parse_address("not-an-ip")
    is_private_quad = InvariantAuditor._is_private_leak("192.168.1.1")
    is_private_doc = InvariantAuditor._is_private_leak("192.0.2.1")

    assert (
        comp_weight,
        case_weights,
        weights,
        quad_invalid,
        str(quad_octal),
        addr_invalid,
        is_private_quad,
        is_private_doc,
    ) == (
        3,
        (1, 0),
        (2, 0),
        None,
        "10.0.0.1",
        None,
        True,
        False,
    )
