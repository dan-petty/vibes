"""Unit tests for the Polyglot CST Ingestion Engine."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from typing import Any
import pytest

_app_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_app_dir))

from parser import (
    BoundaryGuard,
    FileMetrics,
    LanguageDetector,
    PolyglotCSTParser,
    PolyglotFileNode,
    PolyglotSymbol,
    SymbolKind,
    main,
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
    assert (node.skipped, node.language) == (False, "python")

    sym_names = [s.name for s in node.symbols]
    sym_kinds = [s.kind for s in node.symbols]
    assert "calculate_tax" in sym_names
    assert "Account" in sym_names
    assert (SymbolKind.FUNCTION in sym_kinds, SymbolKind.CLASS in sym_kinds) == (True, True)
    assert node.metrics.cyclomatic_complexity >= 3


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
