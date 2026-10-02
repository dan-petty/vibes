"""Tests for the Verification Pyramid Auditor & Test Horizon Scaling Oracle.

Verifies detection of linear assertion density sprawl (VER001), uncontained
subprocess invocations (VER002), shared state race hazards (VER003),
monolithic file sizing (VER004), metrics scoring, and SARIF/Markdown exports.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from verification_pyramid_auditor import (
    AuditSummary,
    VerificationFinding,
    VerificationRule,
    VerificationSeverity,
    audit_test_file,
    audit_test_suite,
    calculate_metrics,
    format_markdown_report,
    format_sarif,
    is_structural_assertion,
    main,
)


def test_is_structural_assertion() -> None:
    """Structural assertions recognize tuple comparisons and collection predicates."""
    code_tuple_left = ast.parse("assert (a, b) == (1, 2)").body[0]
    code_tuple_right = ast.parse("assert val == (10, 20)").body[0]
    code_predicate_all = ast.parse("assert all(x > 0 for x in items)").body[0]
    code_linear = ast.parse("assert a == 1").body[0]

    assert (
        isinstance(code_tuple_left, ast.Assert),
        isinstance(code_tuple_right, ast.Assert),
        isinstance(code_predicate_all, ast.Assert),
        isinstance(code_linear, ast.Assert),
    ) == (True, True, True, True)

    results = (
        is_structural_assertion(code_tuple_left),
        is_structural_assertion(code_tuple_right),
        is_structural_assertion(code_predicate_all),
        is_structural_assertion(code_linear),
    )
    assert results == (True, True, True, False)


def test_linear_assertion_density_detection(tmp_path: Path) -> None:
    """Functions with >= 4 linear asserts trigger VER001; tuple checks pass cleanly."""
    sprawl_file = tmp_path / "test_sprawl.py"
    sprawl_file.write_text(
        "def test_linear():\n"
        "    assert 1 == 1\n"
        "    assert 2 == 2\n"
        "    assert 3 == 3\n"
        "    assert 4 == 4\n",
        encoding="utf-8",
    )

    clean_file = tmp_path / "test_clean.py"
    clean_file.write_text(
        "def test_structural():\n"
        "    assert (1, 2, 3, 4) == (1, 2, 3, 4)\n",
        encoding="utf-8",
    )

    sprawl_funcs, sprawl_asserts, sprawl_struct, sprawl_findings = audit_test_file(sprawl_file)
    clean_funcs, clean_asserts, clean_struct, clean_findings = audit_test_file(clean_file)

    assert (
        (sprawl_funcs, sprawl_asserts, sprawl_struct, len(sprawl_findings)),
        (clean_funcs, clean_asserts, clean_struct, len(clean_findings)),
        (sprawl_findings[0].rule_code, sprawl_findings[0].rule_name),
    ) == (
        (1, 4, 0, 1),
        (1, 1, 1, 0),
        (VerificationRule.ASSERTION_DENSITY_SPRAWL.value, "AssertionDensitySprawl"),
    )


def test_subprocess_containment_detection(tmp_path: Path) -> None:
    """Subprocesses without start_new_session=True trigger VER002."""
    uncontained_file = tmp_path / "test_proc_uncontained.py"
    uncontained_file.write_text(
        "import subprocess\n"
        "def test_spawn():\n"
        "    subprocess.Popen(['echo', 'hello'])\n",
        encoding="utf-8",
    )

    contained_file = tmp_path / "test_proc_contained.py"
    contained_file.write_text(
        "import subprocess\n"
        "def test_safe_spawn():\n"
        "    subprocess.Popen(['echo', 'hello'], start_new_session=True)\n",
        encoding="utf-8",
    )

    _, _, _, uncontained_findings = audit_test_file(uncontained_file)
    _, _, _, contained_findings = audit_test_file(contained_file)

    assert (len(uncontained_findings), len(contained_findings)) == (1, 0)
    assert (uncontained_findings[0].rule_code, uncontained_findings[0].severity) == (
        VerificationRule.UNCONTAINED_PROCESS_SPAWN.value,
        VerificationSeverity.ERROR.value,
    )


def test_shared_state_contention_detection(tmp_path: Path) -> None:
    """Direct access to global .coverage or static paths triggers VER003."""
    contention_file = tmp_path / "test_contention.py"
    contention_file.write_text(
        "def test_coverage_file():\n"
        "    path = '.coverage'\n"
        "    assert path == '.coverage'\n",
        encoding="utf-8",
    )

    _, _, _, findings = audit_test_file(contention_file)
    assert (len(findings), findings[0].rule_code) == (
        1,
        VerificationRule.SHARED_STATE_CONTENTION.value,
    )


def test_monolithic_cold_start_detection(tmp_path: Path) -> None:
    """Files with > 30 test functions trigger VER004 cold-start warning."""
    monolithic_file = tmp_path / "test_monolith.py"
    test_lines = [f"def test_{i}():\n    assert (1,) == (1,)\n" for i in range(35)]
    monolithic_file.write_text("\n".join(test_lines), encoding="utf-8")

    funcs, asserts, structs, findings = audit_test_file(monolithic_file)
    assert (funcs, asserts, structs, len(findings)) == (35, 35, 35, 1)
    assert findings[0].rule_code == VerificationRule.MONOLITHIC_COLD_START.value


def test_unparseable_source_handling(tmp_path: Path) -> None:
    """Syntax errors emit VER000 without crashing the oracle."""
    broken_file = tmp_path / "test_broken.py"
    broken_file.write_text("def broken_syntax(:\n", encoding="utf-8")

    funcs, asserts, structs, findings = audit_test_file(broken_file)
    assert (funcs, asserts, structs, len(findings), findings[0].rule_code) == (0, 0, 0, 1, "VER000")


def test_calculate_metrics() -> None:
    """Health score and friction index correctly calculate weighted deductions."""
    empty_findings: list[VerificationFinding] = []
    perfect_health, zero_friction = calculate_metrics(empty_findings, 10)

    findings = [
        VerificationFinding(
            rule_code="VER002",
            rule_name="UncontainedProcessSpawn",
            file_path="test_a.py",
            line_number=10,
            message="uncontained",
            severity=VerificationSeverity.ERROR.value,
            recommendation="fix",
        ),
        VerificationFinding(
            rule_code="VER001",
            rule_name="AssertionDensitySprawl",
            file_path="test_b.py",
            line_number=20,
            message="sprawl",
            severity=VerificationSeverity.WARNING.value,
            recommendation="fix",
        ),
    ]
    imperfect_health, positive_friction = calculate_metrics(findings, 4)

    assert (perfect_health, zero_friction) == (100.0, 0.0)
    assert (imperfect_health, positive_friction) == (80.0, 0.5)


def test_format_sarif_payload() -> None:
    """SARIF formatter emits valid OASIS 2.1.0 schema structure."""
    summary = AuditSummary(
        total_files=2,
        total_functions=5,
        findings=[
            VerificationFinding(
                rule_code="VER001",
                rule_name="AssertionDensitySprawl",
                file_path="tests/test_demo.py",
                line_number=42,
                message="High assertion density",
                severity="warning",
                recommendation="Use tuples",
            )
        ],
    )
    sarif_dict = format_sarif(summary)
    run = sarif_dict["runs"][0]

    assert (sarif_dict["version"], len(run["results"]), run["results"][0]["ruleId"]) == (
        "2.1.0",
        1,
        "VER001",
    )


def test_format_markdown_report() -> None:
    """Markdown report includes executive headers and finding tables."""
    summary = AuditSummary(
        total_files=1,
        total_functions=2,
        total_assertions=4,
        structural_tuple_assertions=4,
        health_score=100.0,
        friction_index=0.0,
    )
    clean_report = format_markdown_report(summary)
    assert "Verification Health Score" in clean_report
    assert "Zero verification pyramid defects found" in clean_report


def test_audit_test_suite_directory(tmp_path: Path) -> None:
    """Audit test suite discovers test files across directory trees."""
    sub_dir = tmp_path / "unit"
    sub_dir.mkdir()
    (sub_dir / "test_one.py").write_text("def test_a():\n    assert (1,) == (1,)\n", encoding="utf-8")
    (sub_dir / "test_two.py").write_text("def test_b():\n    assert (2,) == (2,)\n", encoding="utf-8")

    summary = audit_test_suite([tmp_path])
    assert (summary.total_files, summary.total_functions, summary.total_assertions) == (2, 2, 2)
    assert (summary.health_score, len(summary.findings)) == (100.0, 0)


def test_cli_main_invocation(tmp_path: Path) -> None:
    """CLI main executes cleanly with multiple output formats."""
    test_file = tmp_path / "test_cli.py"
    test_file.write_text("def test_example():\n    assert (True,) == (True,)\n", encoding="utf-8")

    exit_default = main([str(test_file)])
    exit_json = main([str(test_file), "--json"])
    exit_sarif = main([str(test_file), "--sarif"])
    exit_markdown = main([str(test_file), "--markdown"])

    assert (exit_default, exit_json, exit_sarif, exit_markdown) == (0, 0, 0, 0)


def test_markdown_report_with_findings() -> None:
    """Markdown report formats diagnostic finding tables when defects exist."""
    finding = VerificationFinding(
        rule_code="VER001",
        rule_name="AssertionDensitySprawl",
        file_path="tests/test_sprawl.py",
        line_number=12,
        message="Too many linear asserts",
        severity="warning",
        recommendation="Use tuples",
    )
    summary = AuditSummary(
        total_files=1,
        total_functions=1,
        total_assertions=5,
        structural_tuple_assertions=0,
        findings=[finding],
        health_score=95.0,
        friction_index=1.0,
    )
    report = format_markdown_report(summary)
    dict_payload = summary.to_dict()

    assert (
        "| Rule | Severity | Location | Message | Recommendation |" in report,
        "`VER001`" in report,
        dict_payload["findings_count"],
        len(dict_payload["findings"]),
    ) == (True, True, 1, 1)


def test_is_structural_assertion_any_predicate() -> None:
    """Predicate any(...) is recognized as a structural assertion."""
    code_predicate_any = ast.parse("assert any(x == 1 for x in items)").body[0]
    assert isinstance(code_predicate_any, ast.Assert)
    assert (is_structural_assertion(code_predicate_any),) == (True,)
