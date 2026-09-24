"""Unit tests for the Differential Polyglot AST Mutation Fuzzer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from polyglot_fuzzer import (
    FuzzBatchReport,
    build_cyclical_symlink_tree,
    evaluate_python_ast,
    evaluate_symlink_containment,
    evaluate_text_boundary,
    execute_mutation_check,
    main,
    mutate_bracket_desync,
    mutate_structural_depth,
    mutate_token_smuggle,
    mutate_truncated_stream,
    run_fuzz_campaign,
    to_markdown,
    to_sarif,
)


def test_bracket_desync_mutation() -> None:
    """Verify bracket desynchronization behavior."""
    sample = "def foo():\n    return (1 + 2)\n"
    res = mutate_bracket_desync(sample, "python")
    assert (len(res) > 0, res != sample) == (True, True)


def test_token_smuggle_mutation() -> None:
    """Verify comment token smuggling."""
    sample = "let x: number = 42;\n"
    res = mutate_token_smuggle(sample, "typescript")
    assert ("// injected payload" in res, "\x00" in res) == (True, True)


def test_structural_depth_mutation_python() -> None:
    """Verify deep nesting synthesis for Python."""
    sample = "x = 1\n"
    res = mutate_structural_depth(sample, "python")
    assert ("if True:" in res, res.count("if True:") == 25) == (True, True)


def test_structural_depth_mutation_c_like() -> None:
    """Verify deep nesting synthesis for Rust/Go/TypeScript and Bash."""
    sample_rs = "let x = 1;"
    sample_sh = "echo 1"
    res_rs = mutate_structural_depth(sample_rs, "rust")
    res_sh = mutate_structural_depth(sample_sh, "bash")
    assert ("if (true)" in res_rs, "if true; then" in res_sh) == (True, True)


def test_truncated_stream_mutation() -> None:
    """Verify stream truncation."""
    sample = "abcdefghijklmnop"
    res = mutate_truncated_stream(sample, "python")
    assert (len(res) < len(sample), len(res) > 0) == (True, True)


def test_evaluate_python_ast_success_and_syntax_error() -> None:
    """Verify Python AST parser handles valid code and syntax errors cleanly."""
    valid_py = "x = 42\n"
    invalid_py = "def invalid(\n"
    ok1, st1 = evaluate_python_ast(valid_py)
    ok2, st2 = evaluate_python_ast(invalid_py)
    assert (ok1, st1) == (True, "CLEAN_PARSE")
    assert (ok2, "HANDLED_SYNTAX_ERROR" in st2) == (True, True)


def test_evaluate_text_boundary() -> None:
    """Verify text boundary bounds and corruption detection."""
    valid = "line 1\nline 2\n"
    huge = "a" * 200_000
    ok1, st1 = evaluate_text_boundary(valid)
    ok2, st2 = evaluate_text_boundary(huge)
    assert (ok1, st1) == (True, "CONTAINED")
    assert (ok2, st2) == (False, "UNBOUNDED_MEMORY")


def test_execute_mutation_check_survived() -> None:
    """Verify mutation check execution returns clean metrics."""
    res = execute_mutation_check(
        "test_mutator",
        lambda content, _lang: content + "# mod\n",
        "x = 1\n",
        "python",
    )
    assert (res.mutator, res.survived, res.memory_bounded) == ("test_mutator", True, True)


def test_symlink_containment_and_tree(tmp_path: Path) -> None:
    """Verify cyclic symlink generation and containment loop guard."""
    loop_link = build_cyclical_symlink_tree(tmp_path)
    res = evaluate_symlink_containment(tmp_path)
    assert (loop_link.parent.exists(), res.survived) == (True, True)


def test_run_fuzz_campaign(tmp_path: Path) -> None:
    """Verify end-to-end fuzzing campaign."""
    seeds = {"python": "x = 1\n", "typescript": "let y = 2;\n"}
    report = run_fuzz_campaign(seeds, tmp_path)
    assert (report.total_mutations >= 8, report.pass_rate > 0.0) == (True, True)


def test_sarif_and_markdown_formatting() -> None:
    """Verify SARIF 2.1.0 and Markdown export format contracts."""
    report = FuzzBatchReport(total_mutations=1, passed=1, failed=0)
    sarif = to_sarif(report)
    md = to_markdown(report)
    assert (sarif["version"], len(sarif["runs"])) == ("2.1.0", 1)
    assert ("# Polyglot AST Mutation Fuzzer Report" in md, "✅ PASSED" in md) == (True, True)


def test_main_cli_execution(tmp_path: Path) -> None:
    """Verify main entrypoint across formats."""
    out_file = tmp_path / "out.json"
    rc = main(["--format", "json", "--out", str(out_file)])
    assert (rc, out_file.exists()) == (0, True)

def test_bracket_desync_no_match() -> None:
    """Verify bracket desync when target delimiter is absent."""
    sample = "pure text without symbols"
    res = mutate_bracket_desync(sample, "python")
    assert (len(res) > len(sample), sample in res) == (True, True)


def test_execute_mutation_check_unbounded_memory() -> None:
    """Verify unbounded memory detection."""
    res = execute_mutation_check(
        "huge_mutator",
        lambda content, _lang: "x" * 6_000_000,
        "x",
        "python",
    )
    assert (res.survived, res.memory_bounded, res.status) == (False, False, "UNBOUNDED_MEMORY")


def test_evaluate_text_boundary_empty() -> None:
    """Verify text boundary evaluation with empty text."""
    ok, st = evaluate_text_boundary("")
    assert (ok, st) == (True, "CONTAINED")


def test_main_cli_all_formats(tmp_path: Path, capsys: Any) -> None:
    """Verify main entrypoint across markdown, sarif, and default text formats."""
    sarif_file = tmp_path / "out.sarif"
    rc_sarif = main(["--format", "sarif", "--out", str(sarif_file)])
    assert (rc_sarif, sarif_file.exists()) == (0, True)

    md_file = tmp_path / "out.md"
    rc_md = main(["--format", "markdown", "--out", str(md_file)])
    assert (rc_md, md_file.exists()) == (0, True)

    rc_text = main(["--format", "text"])
    captured = capsys.readouterr()
    assert (rc_text, "Polyglot Fuzzer:" in captured.out) == (0, True)

