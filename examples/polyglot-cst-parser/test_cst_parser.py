"""Unit tests for the Polyglot CST Ingestion Engine."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

_app_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_app_dir))

from parser import (
    EditSpan,
    LanguageDetector,
    PolyglotComplexityCalculator,
    PolyglotCSTParser,
    StructuralQuery,
    SymbolKind,
    apply_edit_span,
    main,
    parse_structural_query,
)


def test_language_detection() -> None:
    """Verify language detection across multiple source extensions."""
    test_cases = [
        ("main.py", "python"),
        ("lib.rs", "rust"),
        ("server.go", "go"),
        ("index.ts", "typescript"),
        ("app.js", "javascript"),
        ("deploy.sh", "bash"),
        ("setup.bash", "bash"),
        ("unknown.xyz", None),
    ]
    actual = [LanguageDetector.detect(name) for name, _ in test_cases]
    expected = [expected_lang for _, expected_lang in test_cases]
    assert actual == expected


def test_python_symbol_extraction_and_complexity() -> None:
    """Verify AST symbol extraction and complexity for Python source."""
    py_code = """
def calculate_tax(amount: float) -> float:
    \"\"\"Calculate sales tax based on bracket.\"\"\"
    if amount > 1000:
        return amount * 0.2
    elif amount > 500:
        return amount * 0.15
    return amount * 0.1

class Account:
    def deposit(self, val: float) -> None:
        pass
"""
    parser = PolyglotCSTParser()
    node = parser.parse_content(py_code, "finance.py", "python")
    sym_names = [s.name for s in node.symbols]
    sym_kinds = [s.kind for s in node.symbols]
    assert (
        node.skipped,
        node.language,
        "calculate_tax" in sym_names,
        "Account" in sym_names,
        SymbolKind.FUNCTION in sym_kinds,
        SymbolKind.CLASS in sym_kinds,
        node.metrics.cyclomatic_complexity >= 3,
    ) == (False, "python", True, True, True, True, True)


def test_rust_symbol_extraction_and_complexity() -> None:
    """Verify CST symbol extraction and complexity for Rust source."""
    rs_code = """
pub struct Client {
    pub timeout: u64,
}

pub trait Gateway {
    fn send(&self) -> Result<(), String>;
}

pub fn handle_request(client: &Client) -> Result<String, String> {
    if client.timeout > 5000 {
        return Err("timeout".to_string());
    }
    match client.timeout {
        0..=1000 => Ok("fast".to_string()),
        _ => Ok("normal".to_string()),
    }
}
"""
    parser = PolyglotCSTParser()
    node = parser.parse_content(rs_code, "lib.rs", "rust")
    assert (node.skipped, node.language) == (False, "rust")

    sym_names = [s.name for s in node.symbols]
    assert all(name in sym_names for name in ["Client", "Gateway", "handle_request"])
    assert node.metrics.cyclomatic_complexity >= 3


def test_go_symbol_extraction_and_complexity() -> None:
    """Verify CST symbol extraction and complexity for Go source."""
    go_code = """
package main

type Config struct {
    Port int
}

type Service interface {
    Start() error
}

func RouteHandler(cfg Config) string {
    if cfg.Port == 8080 {
        return "default"
    }
    switch cfg.Port {
    case 443:
        return "https"
    default:
        return "custom"
    }
}
"""
    parser = PolyglotCSTParser()
    node = parser.parse_content(go_code, "main.go", "go")
    assert (node.skipped, node.language) == (False, "go")

    sym_names = [s.name for s in node.symbols]
    assert all(name in sym_names for name in ["Config", "Service", "RouteHandler"])
    assert node.metrics.cyclomatic_complexity >= 3


def test_typescript_symbol_extraction_and_complexity() -> None:
    """Verify CST symbol extraction and complexity for TypeScript source."""
    ts_code = """
interface UserProfile {
    id: string;
    role: string;
}

class SessionManager {
    constructor() {}
}

function authenticate(user: UserProfile): boolean {
    if (!user || user.role === "guest") {
        return false;
    }
    return user.role === "admin" ? true : false;
}
"""
    parser = PolyglotCSTParser()
    node = parser.parse_content(ts_code, "auth.ts", "typescript")
    assert (node.skipped, node.language) == (False, "typescript")

    sym_names = [s.name for s in node.symbols]
    assert all(name in sym_names for name in ["UserProfile", "SessionManager", "authenticate"])
    assert node.metrics.cyclomatic_complexity >= 3


def test_bash_symbol_extraction_and_complexity() -> None:
    """Verify CST symbol extraction and complexity for Bash script."""
    bash_code = """#!/usr/bin/env bash

deploy_cluster() {
    if [ "$1" == "prod" ]; then
        echo "deploying prod"
    elif [ "$1" == "staging" ]; then
        echo "deploying staging"
    fi
}

cleanup_tmp() {
    rm -rf /tmp/mock-build
}
"""
    parser = PolyglotCSTParser()
    node = parser.parse_content(bash_code, "deploy.sh", "bash")
    assert (node.skipped, node.language) == (False, "bash")

    sym_names = [s.name for s in node.symbols]
    assert all(name in sym_names for name in ["deploy_cluster", "cleanup_tmp"])
    assert node.metrics.cyclomatic_complexity >= 3


def test_boundary_guard_file_size_exceeded(tmp_path: Path) -> None:
    """Verify pre-flight size check blocks files exceeding 5MB."""
    large_file = tmp_path / "giant.js"
    # Create file with 6MB size
    with open(large_file, "wb") as f:
        f.seek(6 * 1024 * 1024)
        f.write(b"\0")

    parser = PolyglotCSTParser(max_file_size_bytes=5 * 1024 * 1024)
    node = parser.parse_file(large_file, base_root=tmp_path)
    assert (node.skipped, "exceeds maximum limit" in node.skip_reason) == (True, True)


def test_boundary_guard_symlink_containment(tmp_path: Path) -> None:
    """Verify symlinks pointing outside workspace root are blocked."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.py"
    outside_file.write_text("def secret_fn(): pass", encoding="utf-8")

    escaping_symlink = workspace_root / "link_to_secret.py"
    try:
        escaping_symlink.symlink_to(outside_file)
    except OSError:
        pytest.skip("Symlinks not supported on filesystem")

    parser = PolyglotCSTParser()
    node = parser.parse_file(escaping_symlink, base_root=workspace_root)
    assert (node.skipped, "outside workspace root" in node.skip_reason) == (True, True)


def test_cli_demo_and_scan(capsys: Any, tmp_path: Path) -> None:
    """Verify CLI demo execution and single-file scanning."""
    exit_demo = main(["--demo"])
    assert exit_demo == 0
    captured_demo = capsys.readouterr().out
    assert "POLYGLOT CST INGESTION SCORECARD" in captured_demo

    sample_py = tmp_path / "sample.py"
    sample_py.write_text("def run_job(): pass\n", encoding="utf-8")

    exit_scan = main(["--scan", str(sample_py)])
    assert exit_scan == 0
    captured_scan = capsys.readouterr().out
    assert "run_job" in captured_scan


def test_node_decision_weights_and_extractor_dispatch() -> None:
    """Verify AST node decision counting and extractor table dispatch."""
    # Test comprehension with multiple if clauses
    comp_ast = ast.parse("[x for x in items if x > 0 if x < 10]").body[0].value.generators[0]  # type: ignore[attr-defined]
    comp_weight = PolyglotComplexityCalculator._node_decision_count(comp_ast)

    # Test match cases (concrete vs wildcard)
    match_ast = ast.parse("match val:\n    case 1:\n        pass\n    case _:\n        pass")
    cases = match_ast.body[0].cases  # type: ignore[attr-defined]
    case_weights = (
        PolyglotComplexityCalculator._node_decision_count(cases[0]),
        PolyglotComplexityCalculator._node_decision_count(cases[1]),
    )

    # Test BoolOp
    bool_ast = ast.parse("a and b and c").body[0].value  # type: ignore[attr-defined]
    bool_weight = PolyglotComplexityCalculator._node_decision_count(bool_ast)

    # Test non-decision node
    pass_ast = ast.parse("pass").body[0]
    pass_weight = PolyglotComplexityCalculator._node_decision_count(pass_ast)

    # Test unsupported language dispatch
    parser = PolyglotCSTParser()
    unknown_syms = parser._extract_symbols("content", "unsupported_lang")

    assert (comp_weight, case_weights, bool_weight, pass_weight, unknown_syms) == (
        3,
        (1, 0),
        2,
        0,
        [],
    )


def test_structural_query_matching() -> None:
    """Verify StructuralQuery filters symbols by kind, pattern, scope, and line bounds."""
    code = """
def init_system() -> None:
    pass

class DataPipeline:
    def process_records(self) -> None:
        pass

    def export_metrics(self) -> None:
        pass

def teardown_system() -> None:
    pass
"""
    parser = PolyglotCSTParser()
    node = parser.parse_content(code, "pipeline.py", "python")

    # Match all functions at top level
    q_top_fn = StructuralQuery(kind=SymbolKind.FUNCTION, parent_scope=None)
    top_fns = node.query_symbols(q_top_fn)

    # Match methods inside DataPipeline
    q_methods = StructuralQuery(kind=SymbolKind.METHOD, parent_scope="DataPipeline")
    methods = node.query_symbols(q_methods)

    # Match by name regex
    q_regex = StructuralQuery(name_pattern=r"system$")
    system_syms = node.query_symbols(q_regex)

    # Match using parsed string query
    parsed_q = parse_structural_query("kind:method scope:DataPipeline name:^export")
    export_syms = node.query_symbols(parsed_q)

    assert (
        [s.name for s in top_fns],
        [s.name for s in methods],
        [s.name for s in system_syms],
        [s.name for s in export_syms],
    ) == (
        ["init_system", "teardown_system"],
        ["process_records", "export_metrics"],
        ["init_system", "teardown_system"],
        ["export_metrics"],
    )


def test_incremental_parsing_preserves_and_shifts_symbols() -> None:
    """Verify incremental reparse preserves unchanged symbols and shifts offsets."""
    code = (
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
        "\n"
        "def gamma():\n"
        "    return 3\n"
    )
    parser = PolyglotCSTParser()
    orig_node = parser.parse_content(code, "service.py", "python")
    orig_sym_names = [s.name for s in orig_node.symbols]
    orig_gamma_line = next(s.line_start for s in orig_node.symbols if s.name == "gamma")

    # Replace beta() (lines 4-5) with a 4-line function containing a branch
    edit = EditSpan(
        start_line=4,
        old_end_line=5,
        new_text="def beta_prime():\n    if True:\n        return 20\n    return 10\n",
    )
    updated_node, updated_code = parser.incremental_reparse(orig_node, code, edit)
    updated_syms = {s.name: s for s in updated_node.symbols}

    assert (
        orig_sym_names,
        updated_node.incremental,
        updated_node.reparsed_symbols_count,
        sorted(updated_syms.keys()),
        updated_syms["alpha"].line_start,
        updated_syms["gamma"].line_start,
        updated_node.metrics.cyclomatic_complexity > orig_node.metrics.cyclomatic_complexity,
        "def beta_prime():" in updated_code,
    ) == (
        ["alpha", "beta", "gamma"],
        True,
        1,
        ["alpha", "beta_prime", "gamma"],
        1,
        orig_gamma_line + 2,
        True,
        True,
    )


def test_apply_edit_span_and_query_cli(capsys: Any, tmp_path: Path) -> None:
    """Verify apply_edit_span text splicing and CLI --query option."""
    content = "line 1\nline 2\nline 3\nline 4\n"
    edit = EditSpan(start_line=2, old_end_line=3, new_text="replaced content")
    spliced = apply_edit_span(content, edit)
    assert spliced == "line 1\nreplaced content\nline 4\n"

    # Test CLI --query filtering
    sample_py = tmp_path / "mod.py"
    sample_py.write_text("class Storage:\n    pass\n\ndef fetch_data():\n    pass\n", encoding="utf-8")
    exit_query = main(["--scan", str(sample_py), "--query", "kind:function"])
    assert exit_query == 0
    captured = capsys.readouterr().out
    assert ("fetch_data" in captured, "class Storage" not in captured) == (True, True)
