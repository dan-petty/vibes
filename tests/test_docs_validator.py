"""Tests for the Vibes Documentation Syntax, Code Fence, Mermaid, and Link Validator."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import pytest

# Add tools/ to sys.path for direct imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from docs_validator import (
    DocFinding,
    DocValidationReport,
    DocsValidator,
    main as docs_validator_main,
)
from resource_iteration_workbench import ResourceScanner, ResourceType


def test_docs_validator_clean_markdown(tmp_path: Path) -> None:
    """Ensure well-formed markdown passes with zero findings."""
    doc = tmp_path / "valid.md"
    doc.write_text(
        "# Valid Document\n\n"
        "Here is a [link to heading](#valid-document).\n\n"
        "| Col 1 | Col 2 |\n"
        "|---|---|\n"
        "| Val 1 | `code | pipe` and \\| escaped |\n\n"
        "````markdown\n"
        "```python\n"
        "x = 1\n"
        "```\n"
        "````\n\n"
        "```mermaid\n"
        "flowchart TD\n"
        "    A[\"Valid Node (quoted)\"] --> B[\"Another\"]\n"
        "```\n",
        encoding="utf-8",
    )
    validator = DocsValidator()
    findings = validator.validate_file(doc)
    assert (len(findings), findings) == (0, [])


def test_docs_validator_code_fences() -> None:
    """Ensure unclosed code fences and premature inner fences are detected."""
    validator = DocsValidator()

    unclosed = "```python\nprint('hello')\n"
    unclosed_findings = validator.check_code_fences(unclosed)
    assert len(unclosed_findings) == 1
    assert (
        unclosed_findings[0].category,
        unclosed_findings[0].severity,
        unclosed_findings[0].line_number,
    ) == ("code_fence", "error", 1)

    nested_bad = (
        "```markdown\n"
        "Code:\n"
        "```python\n"
        "print('inner')\n"
        "```\n"
        "```\n"
    )
    nested_findings = validator.check_code_fences(nested_bad)
    assert any(f.category == "code_fence" and f.line_number == 3 for f in nested_findings)


def test_docs_validator_mermaid_validation() -> None:
    """Ensure mermaid labels with unquoted parentheses and unknown types are flagged."""
    validator = DocsValidator()

    invalid_node = "```mermaid\nflowchart TD\n    A[Unquoted (label)] --> B[OK]\n```\n"
    node_findings = validator.check_mermaid_blocks(invalid_node)

    invalid_type = "```mermaid\nunknown_diagram\n    A --> B\n```\n"
    type_findings = validator.check_mermaid_blocks(invalid_type)

    invalid_edge = "```mermaid\nflowchart TD\n    A -->|Yes (Error)| B\n```\n"
    edge_findings = validator.check_mermaid_blocks(invalid_edge)

    assert (len(node_findings), len(type_findings), len(edge_findings)) == (1, 1, 1)
    assert (
        node_findings[0].category,
        "Unquoted parentheses" in node_findings[0].message,
        type_findings[0].category,
        "Unrecognized Mermaid diagram type" in type_findings[0].message,
        edge_findings[0].category,
        "Unquoted special characters in Mermaid edge label" in edge_findings[0].message,
    ) == (
        "mermaid",
        True,
        "mermaid",
        True,
        "mermaid",
        True,
    )


def test_docs_validator_table_columns() -> None:
    """Ensure markdown tables flag column count mismatches."""
    validator = DocsValidator()
    bad_table = (
        "| Header 1 | Header 2 |\n"
        "|---|---|\n"
        "| Cell 1 | Cell 2 | Cell 3 |\n"
    )
    findings = validator.check_table_columns(bad_table)
    assert len(findings) == 1
    assert (findings[0].category, findings[0].line_number) == ("table", 3)


def test_docs_validator_links_and_anchors(tmp_path: Path) -> None:
    """Ensure broken relative links and non-existent local anchors are detected."""
    source_doc = tmp_path / "source.md"
    source_doc.write_text(
        "# Source Header\n\n"
        "[Good Anchor](#source-header)\n"
        "[Bad Target](nonexistent.md)\n"
        "[Bad Anchor](#missing-anchor)\n"
        "[Bad Host URI](file:///invalid/path.md)\n",
        encoding="utf-8",
    )
    validator = DocsValidator()
    findings = validator.check_links(source_doc.read_text(encoding="utf-8"), file_path=source_doc)
    assert len(findings) == 3
    messages = " ".join(f.message for f in findings)
    assert (
        "nonexistent.md" in messages,
        "#missing-anchor" in messages,
        "invalid/path.md" in messages,
    ) == (True, True, True)


def test_docs_validator_code_snippets() -> None:
    """Ensure syntax errors in embedded Python, JSON, and YAML blocks are caught."""
    validator = DocsValidator()

    bad_py = "```python\ndef broken(\n```\n"
    assert len(validator.check_code_snippets(bad_py)) == 1

    bad_json = "```json\n{\"broken\": }\n```\n"
    assert len(validator.check_code_snippets(bad_json)) == 1

    bad_yaml = "```yaml\nkey: [unclosed\n```\n"
    assert len(validator.check_code_snippets(bad_yaml)) == 1


def test_docs_validator_html_tags() -> None:
    """Ensure unclosed structural HTML tags are flagged."""
    validator = DocsValidator()
    unclosed = "<details><summary>Unclosed"
    findings = validator.check_html_tags(unclosed)
    assert len(findings) == 2
    assert all(f.category == "html_tag" for f in findings)


def test_docs_validator_cli_entrypoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Ensure the standalone CLI runner executes properly and handles flags."""
    valid_doc = tmp_path / "doc.md"
    valid_doc.write_text("# Title\nClean content.", encoding="utf-8")

    # Clean execution
    exit_code = docs_validator_main([str(valid_doc)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "passed validation" in captured.out

    # JSON output flag
    json_exit = docs_validator_main([str(valid_doc), "--json"])
    assert json_exit == 0
    json_out = capsys.readouterr().out
    data = json.loads(json_out)
    assert (data["total_files"], data["is_valid"]) == (1, True)


def test_resource_scanner_scans_documentation(tmp_path: Path) -> None:
    """Ensure ResourceScanner integrates documentation files into repository scans."""
    doc = tmp_path / "guide.md"
    doc.write_text("# User Guide\nHelpful instructions.", encoding="utf-8")

    metrics = ResourceScanner.scan_doc_file(doc)
    assert (
        metrics.resource_type,
        metrics.loc,
        metrics.complexity_violations,
    ) == (ResourceType.DOCUMENTATION, 2, [])


def test_all_vibes_documentation_clean() -> None:
    """Verify that all markdown documents in the Vibes repository pass validation."""
    vibes_root = Path(__file__).resolve().parent.parent
    validator = DocsValidator()
    report = validator.validate_directory(vibes_root)
    assert (report.is_valid, report.error_count, report.total_files >= 50) == (True, 0, True)


def test_docs_validator_auto_fix(tmp_path: Path) -> None:
    """Ensure auto-fixing repairs unclosed code fences, mermaid labels, and absolute URIs."""
    target_file = tmp_path / "target.md"
    target_file.write_text("# Target\nClean doc.", encoding="utf-8")

    broken = tmp_path / "broken.md"
    content = (
        "# Title\n\n"
        f"Link to [Target](file://{target_file.resolve()})\n\n"
        "```mermaid\n"
        "flowchart TD\n"
        "    A[Node (Unquoted)] -->|Yes (Condition)| B\n"
        "```\n\n"
        "```python\n"
        "print('unclosed')\n"
    )
    broken.write_text(content, encoding="utf-8")

    validator = DocsValidator()
    fixes = validator.fix_file(broken)
    remediated = broken.read_text(encoding="utf-8")

    # Verify that the unclosed fence was closed, mermaid was quoted, and absolute link was converted
    assert (
        fixes >= 3,
        'A["Node (Unquoted)"]' in remediated,
        '|"(Condition)"|' in remediated or '|"Yes (Condition)"|' in remediated,
        "target.md" in remediated,
        remediated.endswith("```\n") or remediated.endswith("```"),
    ) == (True, True, True, True, True)


def _run_concurrent_validations(
    validator: DocsValidator, doc_content: str, repetitions: int = 10
) -> Exception | None:
    """Helper running repeated validations in a single thread, returning any caught exception."""
    try:
        for _ in range(repetitions):
            findings = validator.validate_content(doc_content)
            assert len(findings) == 0
        return None
    except Exception as exc:  # noqa: BLE001
        return exc


def test_docs_validator_thread_safety() -> None:
    """Ensure concurrent calls to DocsValidator do not crash or corrupt parser state."""
    import threading

    validator = DocsValidator()
    doc_content = "# Title\n\nHere is [link](#title).\n\n```python\nx = 1\n```\n"
    errors: list[Exception] = []

    def _worker() -> None:
        err = _run_concurrent_validations(validator, doc_content)
        if err is not None:
            errors.append(err)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert (len(errors), errors) == (0, [])

