"""Unit tests for AST Invariant Sentinel."""

import tempfile
from pathlib import Path

from sentinel import audit_file, audit_targets, main


def test_clean_code_passes_all_invariants():
    code = """
def simple_calculator(a: int, b: int, op: str) -> int:
    operations = {
        "+": lambda x, y: x + y,
        "-": lambda x, y: x - y,
        "*": lambda x, y: x * y,
    }
    handler = operations.get(op)
    if not handler:
        raise ValueError("Invalid operator")
    return handler(a, b)
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = Path(tmp.name)

    try:
        violations = audit_file(tmp_path)
        assert len(violations) == 0
    finally:
        tmp_path.unlink()


def test_high_complexity_is_flagged():
    # Construct a function with cyclomatic complexity > 10
    code = """
def complex_spaghetti(x: int) -> int:
    if x == 1:
        return 1
    elif x == 2:
        return 2
    elif x == 3:
        return 3
    elif x == 4:
        return 4
    elif x == 5:
        return 5
    elif x == 6:
        return 6
    elif x == 7:
        return 7
    elif x == 8:
        return 8
    elif x == 9:
        return 9
    elif x == 10:
        return 10
    elif x == 11:
        return 11
    return 0
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = Path(tmp.name)

    try:
        violations = audit_file(tmp_path, max_complexity=10, max_depth=20)
        assert len(violations) == 1
        assert violations[0].invariant == "CyclomaticComplexity"
        assert violations[0].metric_value == 12
    finally:
        tmp_path.unlink()


def test_deep_nesting_is_flagged():
    # Construct a function with 6 levels of nesting
    code = """
def deeply_nested(items):
    for a in items:
        if a > 0:
            for b in items:
                if b > 0:
                    for c in items:
                        if c > 0:
                            print(a, b, c)
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = Path(tmp.name)

    try:
        violations = audit_file(tmp_path, max_depth=5)
        assert len(violations) == 1
        assert violations[0].invariant == "NestingDepth"
        assert violations[0].metric_value == 7
    finally:
        tmp_path.unlink()


def test_private_ip_leakage_is_flagged():
    code = 'DATABASE_URL = "http://192.168.1.105:5432/db"\n'
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = Path(tmp.name)

    try:
        violations = audit_file(tmp_path)
        assert len(violations) == 1
        assert violations[0].invariant == "ZeroTrustSanitization"
        assert "192.168.1.105" in violations[0].message
    finally:
        tmp_path.unlink()


def test_rfc5737_documentation_ip_is_permitted():
    code = 'MOCK_API = "http://198.51.100.25:8080/v1"\n'
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = Path(tmp.name)

    try:
        violations = audit_file(tmp_path)
        assert len(violations) == 0
    finally:
        tmp_path.unlink()


def test_subdomain_mock_domain_is_flagged():
    code = 'ENDPOINT = "https://api.example.com/health"\n'
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = Path(tmp.name)

    try:
        violations = audit_file(tmp_path)
        assert len(violations) == 1
        assert violations[0].invariant == "ZeroTrustSanitization"
        assert "api.example.com" in violations[0].message
    finally:
        tmp_path.unlink()


def _write_module(directory: Path, name: str, code: str) -> Path:
    path = directory / name
    path.write_text(code)
    return path


def test_audit_targets_scans_every_supplied_path(tmp_path):
    """Pre-commit passes N filenames; every one must be audited, not just the first."""
    clean = _write_module(tmp_path, "clean.py", "def ok() -> int:\n    return 1\n")
    dirty = _write_module(tmp_path, "dirty.py", 'HOST = "http://192.168.1.10:8080"\n')

    report = audit_targets([clean, dirty])
    assert (report.files_checked, len(report.violations)) == (2, 1)
    assert report.violations[0].invariant == "ZeroTrustSanitization"


def test_main_audits_all_cli_paths_and_fails(tmp_path, capsys):
    """A violation in any trailing argument must fail the run, never be silently dropped."""
    clean = _write_module(tmp_path, "clean.py", "def ok() -> int:\n    return 1\n")
    dirty = _write_module(tmp_path, "dirty.py", 'HOST = "http://10.1.2.3"\n')

    exit_code = main(["sentinel.py", str(clean), str(dirty)])
    assert exit_code == 1
    assert "Checked 2 Python files." in capsys.readouterr().out


def test_audit_targets_deduplicates_overlapping_paths(tmp_path):
    """A directory plus one of its own files must not double-count that file."""
    module = _write_module(tmp_path, "solo.py", "def ok() -> int:\n    return 1\n")

    report = audit_targets([tmp_path, module])
    assert (report.files_checked, report.is_clean) == (1, True)


def test_main_defaults_to_current_directory_without_paths(tmp_path, monkeypatch):
    """Bare invocation must retain its recursive whole-repository default."""
    _write_module(tmp_path, "clean.py", "def ok() -> int:\n    return 1\n")
    monkeypatch.chdir(tmp_path)

    assert main(["sentinel.py"]) == 0
