"""Unit tests for the Interactive Prompt Mutation Suite & Invariant Fuzzer."""

# sentinel: allow[ZeroTrustSanitization] — adversarial prompt fixtures embedding private IPs to measure invariant drift

import ast
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Add current dir to sys.path for direct imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fuzzer import (
    TAXONOMY_MAP,
    AttackTaxonomy,
    FuzzBaselineDiff,
    FuzzSuiteReport,
    InvariantAuditor,
    InvariantEvaluation,
    PerturbationConfig,
    PerturbationKind,
    PromptMutationFuzzer,
    PromptPerturbationEngine,
    _node_complexity_weight,
    _parse_address,
    _parse_dotted_quad,
    compute_baseline_diff,
    export_sarif,
    main,
)
from providers import (
    AnthropicProvider,
    GenericRestProvider,
    MockModelProvider,
    OllamaProvider,
    OpenAIProvider,
    extract_code_block,
    get_provider,
    list_supported_providers,
    validate_provider_endpoint,
)


def test_perturbation_engine_dilution() -> None:
    """Ensure dilution perturbations insert bureaucratic filler."""
    engine = PromptPerturbationEngine(PerturbationConfig(intensity=0.5, random_seed=42))
    base_prompt = "Line 1: Rules.\nLine 2: Instructions.\nLine 3: Output format."
    result = engine.mutate_prompt(base_prompt, PerturbationKind.DILUTION)

    assert (
        result.kind,
        len(result.mutated_prompt.splitlines()) > len(base_prompt.splitlines()),
        len(result.applied_patches),
    ) == (PerturbationKind.DILUTION, True, 1)


def test_perturbation_engine_distraction() -> None:
    """Ensure distraction perturbations append secondary tasks."""
    engine = PromptPerturbationEngine(PerturbationConfig(intensity=0.5, random_seed=42))
    base_prompt = "You are a code refactoring assistant."
    result = engine.mutate_prompt(base_prompt, PerturbationKind.DISTRACTION)
    has_distraction = any(snippet in result.mutated_prompt for snippet in engine.DISTRACTION_SNIPPETS)

    assert (result.kind, len(result.mutated_prompt) > len(base_prompt), has_distraction) == (
        PerturbationKind.DISTRACTION,
        True,
        True,
    )


def test_perturbation_engine_injection() -> None:
    """Ensure injection escape perturbations append override directives."""
    engine = PromptPerturbationEngine(PerturbationConfig(intensity=0.5, random_seed=42))
    base_prompt = "Enforce M <= 10 at all times."
    result = engine.mutate_prompt(base_prompt, PerturbationKind.INJECTION_ESCAPE)
    has_override = any(
        kw in result.mutated_prompt for kw in ("SYSTEM OVERRIDE", "ADMIN DIRECTIVE", "MAINTENANCE NOTICE")
    )

    assert (result.kind, has_override) == (PerturbationKind.INJECTION_ESCAPE, True)


def test_perturbation_engine_truncation() -> None:
    """Ensure truncation cuts prompt lines according to intensity."""
    engine = PromptPerturbationEngine(PerturbationConfig(intensity=0.8, random_seed=42))
    base_prompt = "\n".join([f"Directive {i}: Rule statement." for i in range(10)])
    result = engine.mutate_prompt(base_prompt, PerturbationKind.TRUNCATION)

    assert (result.kind, len(result.mutated_prompt.splitlines()) < len(base_prompt.splitlines())) == (
        PerturbationKind.TRUNCATION,
        True,
    )


def test_perturbation_engine_complexity_trap() -> None:
    """Ensure complexity trap perturbations append nested constraints."""
    engine = PromptPerturbationEngine(PerturbationConfig(intensity=0.5, random_seed=42))
    base_prompt = "Implement sorting algorithm."
    result = engine.mutate_prompt(base_prompt, PerturbationKind.COMPLEXITY_TRAP)
    has_trap = "nested if-else" in result.mutated_prompt or "monolithic" in result.mutated_prompt

    assert (result.kind, has_trap) == (PerturbationKind.COMPLEXITY_TRAP, True)


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
    has_depth_viol = any("Depth=" in v for v in evaluation.violations)
    assert (evaluation.max_depth > 5, evaluation.nesting_violation, has_depth_viol) == (True, True, True)


def test_invariant_auditor_detects_sanitization_leaks() -> None:
    """Ensure InvariantAuditor catches RFC 1918 IPs and unapproved subdomains."""
    leaky_code = """
URL_INTERNAL = "http://10.0.0.15/api"
URL_SUBDOMAIN = "https://sub.example.com/v1"
"""
    evaluation = InvariantAuditor.evaluate_code("leaky", leaky_code)
    has_leak_1 = any("Private host address" in v for v in evaluation.violations)
    has_leak_2 = any("Subdomain" in v for v in evaluation.violations)
    assert (evaluation.sanitization_leak, len(evaluation.violations), has_leak_1, has_leak_2) == (
        True,
        2,
        True,
        True,
    )


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

    assert (
        report.total_runs,
        report.clean_runs,
        report.drift_violations,
        report.resilience_score,
        report.vulnerability_breakdown[PerturbationKind.INJECTION_ESCAPE.value],
    ) == (3, 2, 1, 66.7, 1)


def test_fuzzer_cli_main_entrypoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Ensure fuzzer CLI entrypoint executes and supports --json output."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("System instructions for coding assistant.\n", encoding="utf-8")

    exit_code = main(["--prompt-file", str(prompt_file), "--intensity", "0.6", "--json"])
    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert (
        exit_code,
        "resilience_score" in data,
        "vulnerability_breakdown" in data,
        data["total_runs"],
    ) == (0, True, True, 5)


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


def _mock_openai_transport(req: Any, timeout: float) -> tuple[int, dict[str, Any], float]:
    """Mock HTTP transport returning OpenAI completion payload."""
    payload = {
        "id": "chatcmpl-123",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "```python\ndef generated_fn():\n    return 42\n```",
                },
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 10},
    }
    return 200, payload, 0.05


def _mock_anthropic_transport(req: Any, timeout: float) -> tuple[int, dict[str, Any], float]:
    """Mock HTTP transport returning Anthropic message payload."""
    payload = {
        "id": "msg-123",
        "content": [{"type": "text", "text": "```python\ndef anthropic_fn():\n    return 1\n```"}],
        "usage": {"input_tokens": 20, "output_tokens": 8},
    }
    return 200, payload, 0.04


def _mock_ollama_transport(req: Any, timeout: float) -> tuple[int, dict[str, Any], float]:
    """Mock HTTP transport returning Ollama generate payload."""
    payload = {
        "model": "qwen2.5-coder:7b",
        "response": "```python\ndef ollama_fn():\n    return 100\n```",
        "prompt_eval_count": 12,
        "eval_count": 6,
    }
    return 200, payload, 0.03


def _mock_rest_transport(req: Any, timeout: float) -> tuple[int, dict[str, Any], float]:
    """Mock HTTP transport returning custom REST payload."""
    payload = {"data": {"result": "```python\ndef rest_fn():\n    return 'ok'\n```"}}
    return 200, payload, 0.02


def _mock_error_transport(req: Any, timeout: float) -> tuple[int, dict[str, Any], float]:
    """Mock HTTP transport returning 500 error payload."""
    return 500, {"error": "Internal Server Error"}, 0.01


def test_provider_registry_and_discovery() -> None:
    """Verify supported model providers and factory instantiation."""
    supported = list_supported_providers()
    mock_p = get_provider("mock", model="m1")
    openai_p = get_provider("openai", model="gpt-4o", api_base="https://example.com/v1")
    anthropic_p = get_provider("anthropic", model="claude-3-5-sonnet", api_base="https://example.com/v1")
    ollama_p = get_provider("ollama", model="qwen2.5-coder:7b", api_base="http://localhost:11434")
    rest_p = get_provider("rest", model="custom-llm", api_base="https://example.com/api")

    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("nonexistent")

    assert (
        supported,
        isinstance(mock_p, MockModelProvider),
        isinstance(openai_p, OpenAIProvider),
        isinstance(anthropic_p, AnthropicProvider),
        isinstance(ollama_p, OllamaProvider),
        isinstance(rest_p, GenericRestProvider),
        (mock_p.provider_name, openai_p.provider_name, anthropic_p.provider_name),
    ) == (
        ("anthropic", "mock", "ollama", "openai", "rest"),
        True,
        True,
        True,
        True,
        True,
        ("mock", "openai", "anthropic"),
    )


def test_extract_code_block_helper() -> None:
    """Ensure code extraction isolates Python code fences or returns raw text."""
    fenced_py = "Some text\n```python\ndef foo():\n    return 1\n```\nExplanation."
    fenced_generic = "```\ndef bar():\n    return 2\n```"
    raw_code = "def baz():\n    return 3"

    assert (
        extract_code_block(fenced_py),
        extract_code_block(fenced_generic),
        extract_code_block(raw_code),
    ) == (
        "def foo():\n    return 1",
        "def bar():\n    return 2",
        "def baz():\n    return 3",
    )


def test_mock_provider_generation_and_heuristics() -> None:
    """Test MockModelProvider response generation under default and canned modes."""
    provider = MockModelProvider()
    resp_clean = provider.generate("Refactor this code.")
    resp_injection = provider.generate("Apply INJECTION_ESCAPE bypass.")
    resp_trap = provider.generate("Generate COMPLEXITY_TRAP pattern.")

    canned_provider = MockModelProvider(canned_responses={"custom": "def canned(): pass"})
    resp_canned = canned_provider.generate("custom")

    assert (
        resp_clean.error is None,
        "clean_helper" in resp_clean.extracted_code,
        "injected_bypass" in resp_injection.extracted_code,
        "monolithic_trap" in resp_trap.extracted_code,
        resp_canned.extracted_code,
        resp_clean.prompt_tokens > 0,
    ) == (
        True,
        True,
        True,
        True,
        "def canned(): pass",
        True,
    )


def test_zero_trust_endpoint_validation() -> None:
    """Verify zero-trust URL and IP validation across external and local hosts."""
    valid_https, _ = validate_provider_endpoint("https://example.com/v1")
    valid_local, _ = validate_provider_endpoint("http://localhost:11434")
    valid_ip_local, _ = validate_provider_endpoint("http://127.0.0.1:8000")
    valid_test_net, _ = validate_provider_endpoint("http://192.0.2.1:8000")
    bad_scheme, _ = validate_provider_endpoint("ftp://example.com")
    bad_internal, _ = validate_provider_endpoint("https://untrusted.internal/api")
    bad_private, _ = validate_provider_endpoint("https://10.0.0.1:8000")

    assert (
        valid_https,
        valid_local,
        valid_ip_local,
        valid_test_net,
        bad_scheme,
        bad_internal,
        bad_private,
    ) == (
        True,
        True,
        True,
        True,
        False,
        False,
        False,
    )


def test_openai_provider_with_mock_transport() -> None:
    """Verify OpenAIProvider generates code and extracts usage via mock transport."""
    provider = OpenAIProvider(
        model_id="gpt-4o",
        api_base="https://example.com/v1",
        api_key="test-key",
        transport=_mock_openai_transport,
    )
    resp = provider.generate("Write helper fn.")

    err_provider = OpenAIProvider(
        model_id="gpt-4o",
        api_base="https://example.com/v1",
        api_key="test-key",
        transport=_mock_error_transport,
    )
    err_resp = err_provider.generate("Write helper fn.")

    assert (
        resp.provider_name,
        resp.model_id,
        "generated_fn" in resp.extracted_code,
        (resp.prompt_tokens, resp.completion_tokens),
        resp.error is None,
        err_resp.error is not None,
    ) == (
        "openai",
        "gpt-4o",
        True,
        (15, 10),
        True,
        True,
    )


def test_anthropic_provider_with_mock_transport() -> None:
    """Verify AnthropicProvider generates code and extracts usage via mock transport."""
    provider = AnthropicProvider(
        model_id="claude-3-5-sonnet",
        api_base="https://example.com/v1",
        api_key="test-anthropic-key",
        transport=_mock_anthropic_transport,
    )
    resp = provider.generate("Write helper fn.")

    err_provider = AnthropicProvider(
        model_id="claude-3-5-sonnet",
        api_base="https://example.com/v1",
        api_key="test-anthropic-key",
        transport=_mock_error_transport,
    )
    err_resp = err_provider.generate("Write helper fn.")

    assert (
        resp.provider_name,
        resp.model_id,
        "anthropic_fn" in resp.extracted_code,
        (resp.prompt_tokens, resp.completion_tokens),
        resp.error is None,
        err_resp.error is not None,
    ) == (
        "anthropic",
        "claude-3-5-sonnet",
        True,
        (20, 8),
        True,
        True,
    )


def test_ollama_provider_with_mock_transport() -> None:
    """Verify OllamaProvider generates code and extracts usage via mock transport."""
    provider = OllamaProvider(
        model_id="qwen2.5-coder:7b",
        api_base="http://localhost:11434",
        transport=_mock_ollama_transport,
    )
    resp = provider.generate("Write helper fn.")

    err_provider = OllamaProvider(
        model_id="qwen2.5-coder:7b",
        api_base="http://localhost:11434",
        transport=_mock_error_transport,
    )
    err_resp = err_provider.generate("Write helper fn.")

    assert (
        resp.provider_name,
        resp.model_id,
        "ollama_fn" in resp.extracted_code,
        (resp.prompt_tokens, resp.completion_tokens),
        resp.error is None,
        err_resp.error is not None,
    ) == (
        "ollama",
        "qwen2.5-coder:7b",
        True,
        (12, 6),
        True,
        True,
    )


def test_generic_rest_provider_with_mock_transport() -> None:
    """Verify GenericRestProvider extracts response via custom JSON path."""
    provider = GenericRestProvider(
        endpoint_url="https://example.com/api/predict",
        model_id="custom-llm",
        response_path=("data", "result"),
        transport=_mock_rest_transport,
    )
    resp = provider.generate("Generate code.")

    err_provider = GenericRestProvider(
        endpoint_url="https://example.com/api/predict",
        model_id="custom-llm",
        transport=_mock_error_transport,
    )
    err_resp = err_provider.generate("Generate code.")

    assert (
        resp.provider_name,
        resp.model_id,
        "rest_fn" in resp.extracted_code,
        resp.error is None,
        err_resp.error is not None,
    ) == (
        "rest",
        "custom-llm",
        True,
        True,
        True,
    )


def test_fuzzer_with_mock_provider_end_to_end() -> None:
    """Verify PromptMutationFuzzer executes dynamic matrix via ModelProvider."""
    fuzzer = PromptMutationFuzzer(
        config=PerturbationConfig(intensity=0.5),
        provider=MockModelProvider(),
    )
    report = fuzzer.run_fuzz_matrix("System prompt: keep M <= 10.")
    rendered = fuzzer.render_report(report)

    assert (
        report.total_runs,
        report.clean_runs,
        report.drift_violations,
        "Provider: mock" in rendered,
        "Invariant Resilience Score:" in rendered,
    ) == (
        5,
        4,
        1,
        True,
        True,
    )


def test_fuzzer_cli_list_providers(capsys: pytest.CaptureFixture[str]) -> None:
    """Ensure fuzzer CLI --list-providers outputs supported providers."""
    exit_code = main(["--list-providers"])
    captured = capsys.readouterr().out
    assert (
        exit_code,
        "Supported model providers:" in captured,
        "openai" in captured,
        "anthropic" in captured,
        "ollama" in captured,
        "rest" in captured,
    ) == (0, True, True, True, True, True)


def test_attack_taxonomy_mappings() -> None:
    """Ensure every perturbation kind has a valid attack taxonomy mapping."""
    kinds = list(PerturbationKind)
    has_all_mapped = all(k in TAXONOMY_MAP for k in kinds)
    sample_tax = TAXONOMY_MAP[PerturbationKind.INJECTION_ESCAPE]

    assert (
        has_all_mapped,
        isinstance(sample_tax, AttackTaxonomy),
        sample_tax.owasp_id,
        sample_tax.mitre_atlas_id,
        sample_tax.cwe_id,
    ) == (
        True,
        True,
        "LLM01",
        "AML.T0051",
        "CWE-77",
    )


def test_baseline_diff_computation() -> None:
    """Ensure differential comparisons categorize introduced, resolved, and persistent violations."""
    curr_evals = [
        InvariantEvaluation(
            target_name="fuzz-INJECTION_ESCAPE",
            max_complexity=12,
            max_depth=3,
            complexity_violation=True,
            nesting_violation=False,
            sanitization_leak=False,
            violations=["Complexity M=12 breaches cap 10."],
        ),
        InvariantEvaluation(
            target_name="fuzz-TRUNCATION",
            max_complexity=3,
            max_depth=2,
            complexity_violation=False,
            nesting_violation=False,
            sanitization_leak=False,
            violations=["New syntax truncation error."],
        ),
    ]
    curr_report = FuzzSuiteReport(
        total_runs=2,
        clean_runs=0,
        drift_violations=2,
        resilience_score=0.0,
        vulnerability_breakdown={"INJECTION_ESCAPE": 1, "TRUNCATION": 1},
        evaluations=curr_evals,
    )
    base_data = {
        "resilience_score": 50.0,
        "evaluations": [
            {
                "target_name": "fuzz-INJECTION_ESCAPE",
                "violations": ["Complexity M=12 breaches cap 10."],
            },
            {
                "target_name": "fuzz-DISTRACTION",
                "violations": ["Private host address leak: 10.0.0.1"],
            },
        ],
    }
    diff = compute_baseline_diff(curr_report, base_data)
    d_dict = diff.to_dict()

    assert (
        diff.baseline_score,
        diff.current_score,
        diff.score_delta,
        diff.status,
        diff.persistent_violations,
        diff.resolved_violations,
        diff.introduced_violations,
        "score_delta" in d_dict,
    ) == (
        50.0,
        0.0,
        -50.0,
        "REGRESSED",
        ["fuzz-INJECTION_ESCAPE: Complexity M=12 breaches cap 10."],
        ["fuzz-DISTRACTION: Private host address leak: 10.0.0.1"],
        ["fuzz-TRUNCATION: New syntax truncation error."],
        True,
    )


def test_baseline_diff_improved_status() -> None:
    """Ensure baseline diff sets status IMPROVED and UNCHANGED appropriately."""
    report_improved = FuzzSuiteReport(
        total_runs=1,
        clean_runs=1,
        drift_violations=0,
        resilience_score=100.0,
        vulnerability_breakdown={},
        evaluations=[],
    )
    diff_improved = compute_baseline_diff(report_improved, {"resilience_score": 80.0, "evaluations": []})
    diff_unchanged = compute_baseline_diff(report_improved, {"resilience_score": 100.0, "evaluations": []})

    assert (
        diff_improved.status,
        diff_improved.score_delta,
        diff_unchanged.status,
        diff_unchanged.score_delta,
    ) == (
        "IMPROVED",
        20.0,
        "UNCHANGED",
        0.0,
    )


def test_export_sarif_telemetry() -> None:
    """Ensure export_sarif emits valid OASIS SARIF 2.1.0 document with taxonomy tags."""
    report = FuzzSuiteReport(
        total_runs=1,
        clean_runs=0,
        drift_violations=1,
        resilience_score=0.0,
        vulnerability_breakdown={"INJECTION_ESCAPE": 1},
        evaluations=[
            InvariantEvaluation(
                target_name="fuzz-INJECTION_ESCAPE",
                max_complexity=12,
                max_depth=2,
                complexity_violation=True,
                nesting_violation=False,
                sanitization_leak=False,
                violations=["Complexity M=12 breaches cap."],
            )
        ],
    )
    diff = FuzzBaselineDiff(
        baseline_score=0.0,
        current_score=0.0,
        score_delta=0.0,
        introduced_violations=[],
        resolved_violations=[],
        persistent_violations=[],
        status="UNCHANGED",
    )
    sarif_str = export_sarif(report, diff)
    doc = json.loads(sarif_str)
    run = doc["runs"][0]
    rule_ids = [r["id"] for r in run["tool"]["driver"]["rules"]]
    result = run["results"][0]

    assert (
        doc["version"],
        "sarif-schema-2.1.0.json" in doc["$schema"],
        len(run["tool"]["driver"]["rules"]),
        "PMF-LLM01-INJECTION_ESCAPE" in rule_ids,
        result["ruleId"],
        result["level"],
        "baselineDiff" in run["properties"],
    ) == (
        "2.1.0",
        True,
        5,
        True,
        "PMF-LLM01-INJECTION_ESCAPE",
        "error",
        True,
    )


def test_render_report_with_diff() -> None:
    """Ensure render_report includes taxonomy columns and baseline diff summary."""
    fuzzer = PromptMutationFuzzer()
    report = FuzzSuiteReport(
        total_runs=1,
        clean_runs=1,
        drift_violations=0,
        resilience_score=100.0,
        vulnerability_breakdown={"DILUTION": 0},
        evaluations=[],
    )
    diff = FuzzBaselineDiff(
        baseline_score=80.0,
        current_score=100.0,
        score_delta=20.0,
        introduced_violations=[],
        resolved_violations=["fuzz-DILUTION: old breach"],
        persistent_violations=[],
        status="IMPROVED",
    )
    table = fuzzer.render_report(report, diff)

    assert (
        "OWASP" in table,
        "MITRE ATLAS" in table,
        "BASELINE DIFF: IMPROVED" in table,
        "Base: 80.0% -> Curr: 100.0% (+20.0%)" in table,
    ) == (
        True,
        True,
        True,
        True,
    )


def test_fuzzer_cli_baseline_and_sarif_flags(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Ensure CLI correctly handles --save-baseline, --baseline, and --sarif."""
    base_file = tmp_path / "base_scorecard.json"
    exit_save = main(["--save-baseline", str(base_file), "--json"])
    assert (exit_save, base_file.is_file()) == (0, True)

    capsys.readouterr()  # Flush buffer
    exit_diff = main(["--baseline", str(base_file), "--json"])
    diff_out = json.loads(capsys.readouterr().out)
    assert (exit_diff, "baseline_diff" in diff_out, diff_out["baseline_diff"]["status"]) == (
        0,
        True,
        "UNCHANGED",
    )

    exit_sarif = main(["--sarif"])
    sarif_out = json.loads(capsys.readouterr().out)
    assert (exit_sarif, sarif_out["version"], len(sarif_out["runs"])) == (0, "2.1.0", 1)
