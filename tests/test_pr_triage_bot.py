"""Tests for Closed-Loop PR Triage & Invariant Review Bot."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import pr_triage_bot as bot


def test_security_reviewer_detects_rfc1918_and_dangerous_calls(tmp_path: Path) -> None:
    """Security persona detects private IPs, secrets, and dangerous primitives."""
    private_ip = ".".join(["192", "168", "1", "100"])
    py_code = f"""
ip = "{private_ip}"
secret_key = "abc12345678901234567890"
eval("2 + 2")
import subprocess
subprocess.run(["ls"], shell=True)
"""
    py_file = tmp_path / "unsafe.py"
    py_file.write_text(py_code, encoding="utf-8")

    reviewer = bot.SecurityReviewer()
    findings = reviewer.review_file(py_file, py_code)
    rule_ids = tuple(sorted(f.rule_id for f in findings))

    assert (
        "SEC-DANGEROUS-EVAL" in rule_ids,
        "SEC-GENERIC_TOKEN" in rule_ids,
        "SEC-RFC1918-LEAK" in rule_ids,
        "SEC-SUBPROCESS-SHELL" in rule_ids,
    ) == (True, True, True, True)


def test_architecture_reviewer_detects_complexity_and_nesting(tmp_path: Path) -> None:
    """Architecture persona flags high cyclomatic complexity and deep nesting."""
    code = """
def complex_fn(a, b, c, d, e):
    if a:
        if b:
            if c:
                if d:
                    if e:
                        if a and b:
                            return 1
    return 0
"""
    py_file = tmp_path / "deep.py"
    py_file.write_text(code, encoding="utf-8")

    reviewer = bot.ArchitectureReviewer()
    findings = reviewer.review_file(py_file, code)
    rules = tuple(f.rule_id for f in findings)

    assert (
        "ARCH-PARAM-COUNT" in rules,
        "ARCH-NESTING-EXCEEDED" in rules,
    ) == (True, True)


def test_architecture_reviewer_detects_assertion_sprawl(tmp_path: Path) -> None:
    """Architecture persona warns on sequential linear assertions in tests."""
    code = """
def test_something():
    assert 1 == 1
    assert 2 == 2
    assert 3 == 3
    assert 4 == 4
    assert 5 == 5
    assert 6 == 6
"""
    test_file = tmp_path / "test_sprawl.py"
    test_file.write_text(code, encoding="utf-8")

    reviewer = bot.ArchitectureReviewer()
    findings = reviewer.review_file(test_file, code)
    rules = tuple(f.rule_id for f in findings)

    assert ("ARCH-ASSERTION-SPRAWL" in rules,) == (True,)


def test_devops_reviewer_flags_unpinned_actions(tmp_path: Path) -> None:
    """DevOps persona flags unpinned action uses directives and missing permissions."""
    wf_code = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo "${{ github.event.issue.title }}"
"""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf_file = wf_dir / "ci.yml"
    wf_file.write_text(wf_code, encoding="utf-8")

    reviewer = bot.DevOpsReviewer()
    findings = reviewer.review_file(wf_file, wf_code)
    rules = tuple(f.rule_id for f in findings)

    assert (
        "DEVOPS-UNPINNED-ACTION" in rules,
        "DEVOPS-MISSING-PERMISSIONS" in rules,
        "DEVOPS-EXPRESSION-INJECTION" in rules,
    ) == (True, True, True)


def test_qa_reviewer_flags_missing_docstrings_and_unclosed_fences(tmp_path: Path) -> None:
    """QA persona catches missing public docstrings and unclosed markdown fences."""
    py_code = "def undocumented_public_api():\n    pass\n"
    py_file = tmp_path / "mod.py"
    py_file.write_text(py_code, encoding="utf-8")

    md_code = "# Title\n\n```python\ncode\n"
    md_file = tmp_path / "doc.md"
    md_file.write_text(md_code, encoding="utf-8")

    reviewer = bot.QAReviewer()
    py_findings = reviewer.review_file(py_file, py_code)
    md_findings = reviewer.review_file(md_file, md_code)

    assert (
        len(py_findings) >= 1,
        py_findings[0].rule_id,
        len(md_findings) >= 1,
        md_findings[0].rule_id,
    ) == (True, "QA-MISSING-DOCSTRING", True, "QA-UNCLOSED-FENCE")


def test_pr_triage_bot_clean_files_verdict(tmp_path: Path) -> None:
    """Clean files receive APPROVE verdict across all personas."""
    clean_py = '"""Module docstring."""\n\ndef clean_fn() -> int:\n    """Return answer."""\n    return 42\n'
    clean_file = tmp_path / "clean.py"
    clean_file.write_text(clean_py, encoding="utf-8")

    triage = bot.PRTriageBot()
    report = triage.evaluate_files([clean_file])

    assert (
        report.overall_verdict,
        report.total_findings,
        all(r.verdict == bot.ReviewVerdict.APPROVE for r in report.reviews),
    ) == (bot.ReviewVerdict.APPROVE, 0, True)


def test_pr_triage_bot_markdown_and_json_rendering(tmp_path: Path) -> None:
    """Report formats structured Markdown and valid JSON output."""
    clean_file = tmp_path / "sample.py"
    clean_file.write_text('"""Clean."""\n\ndef run() -> None:\n    """Run."""\n    pass\n', encoding="utf-8")

    triage = bot.PRTriageBot()
    report = triage.evaluate_files([clean_file])

    md = report.render_markdown()
    data = json.loads(report.to_json())

    assert (
        "## 🤖 Automated PR Triage & Invariant Review: APPROVE" in md,
        "| **Security** | `APPROVE` |" in md,
        data["overall_verdict"],
        len(data["reviews"]),
    ) == (True, True, "APPROVE", 4)


def test_cli_execution_with_findings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI evaluates files, renders Markdown, and exits 1 under strict mode with errors."""
    unsafe_file = tmp_path / "unsafe.py"
    unsafe_file.write_text('eval("1+1")\n', encoding="utf-8")

    exit_code = bot.main([
        "--paths", str(unsafe_file),
        "--strict",
    ])
    captured = capsys.readouterr()

    assert (
        exit_code,
        "REQUEST_CHANGES" in captured.out,
        "SEC-DANGEROUS-EVAL" in captured.out,
    ) == (1, True, True)


def test_cli_execution_json_out(tmp_path: Path) -> None:
    """CLI writes machine-readable JSON report to output file."""
    clean_file = tmp_path / "clean.py"
    clean_file.write_text('"""Doc."""\n', encoding="utf-8")
    out_file = tmp_path / "report.json"

    exit_code = bot.main([
        "--paths", str(clean_file),
        "--out", str(out_file),
        "--json",
    ])

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert (exit_code, data["overall_verdict"]) == (0, "APPROVE")


def test_cli_missing_paths() -> None:
    """CLI invocation with no paths returns usage code 2."""
    assert (bot.main([]) == 2,) == (True,)
