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
    OBSERVATION_REQUIRED_SECTION_COUNT,
    contrast_ratio,
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


def test_contrast_ratio_matches_wcag_reference_values() -> None:
    """Ensure the WCAG 2.1 relative luminance formula is implemented correctly."""
    assert (
        round(contrast_ratio("#fff", "#000"), 2),
        round(contrast_ratio("#fff", "#fff"), 2),
        round(contrast_ratio("#fff", "#b3261e"), 2),
    ) == (21.0, 1.0, 6.54)


def test_mermaid_fill_without_explicit_color_is_flagged() -> None:
    """An inherited label color flips with the GitHub theme and must be rejected."""
    validator = DocsValidator()
    content = "```mermaid\nflowchart TD\n    A[\"Node\"]\n    style A fill:#b3261e\n```\n"
    findings = validator.validate_content(content, Path("doc.md"))
    assert [f.category for f in findings] == ["mermaid_style"]
    assert "no explicit 'color:'" in findings[0].message


def test_mermaid_below_wcag_contrast_is_flagged() -> None:
    """Fill and label colors closer than 4.5:1 are illegible and must be rejected."""
    validator = DocsValidator()
    content = "```mermaid\nflowchart TD\n    A[\"Node\"]\n    style A fill:#6a6,color:#fff\n```\n"
    findings = validator.validate_content(content, Path("doc.md"))
    assert [f.category for f in findings] == ["mermaid_style"]
    assert "2.80:1" in findings[0].message


def test_mermaid_palette_pairs_pass_contrast_check() -> None:
    """Every documented palette pair must satisfy the rule that enforces it."""
    palette = [("#b3261e", "#fff"), ("#1b5e20", "#fff"), ("#f2b705", "#000"), ("#4527a0", "#fff")]
    validator = DocsValidator()
    for fill, color in palette:
        content = f"```mermaid\nflowchart TD\n    A[\"Node\"]\n    style A fill:{fill},color:{color}\n```\n"
        assert validator.validate_content(content, Path("doc.md")) == []


def test_legacy_graph_declaration_is_flagged() -> None:
    """The deprecated `graph` alias must be rejected in favour of `flowchart`."""
    validator = DocsValidator()
    findings = validator.validate_content("```mermaid\ngraph TD\n    A --> B\n```\n", Path("doc.md"))
    assert [f.category for f in findings] == ["mermaid"]
    assert "flowchart TD" in findings[0].message


def test_sequence_diagram_semicolon_is_flagged() -> None:
    """A semicolon terminates a sequence statement, truncating the message silently."""
    validator = DocsValidator()
    content = "```mermaid\nsequenceDiagram\n    A->>B: do this; then that\n```\n"
    findings = validator.validate_content(content, Path("doc.md"))
    assert [f.category for f in findings] == ["mermaid"]
    assert "statement separator" in findings[0].message


def test_sequence_diagram_note_semicolon_is_flagged() -> None:
    """Notes carry the same separator semantics as messages."""
    validator = DocsValidator()
    content = "```mermaid\nsequenceDiagram\n    Note over A,B: first clause; second clause\n```\n"
    assert [f.category for f in validator.validate_content(content, Path("doc.md"))] == ["mermaid"]


def test_semicolon_outside_sequence_diagram_is_permitted() -> None:
    """Flowchart labels have no statement-separator semantics for ';'."""
    validator = DocsValidator()
    content = '```mermaid\nflowchart TD\n    A["first; second"] --> B["done"]\n```\n'
    assert validator.validate_content(content, Path("doc.md")) == []


def _write_map(tmp_path: Path, tree: str) -> Path:
    """Write a README whose directory map describes tmp_path."""
    doc = tmp_path / "README.md"
    doc.write_text(f"# Map\n\n```text\n{tree}```\n", encoding="utf-8")
    return doc


def test_directory_map_flags_vanished_path(tmp_path: Path) -> None:
    """A mapped path that no longer exists is silent drift no other gate can see."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "kept.md").write_text("x", encoding="utf-8")
    doc = _write_map(tmp_path, "├── docs/\n│   ├── kept.md\n│   └── deleted.md\n")

    findings = DocsValidator().validate_file(doc)
    assert [f.category for f in findings] == ["directory_map"]
    assert "deleted.md" in findings[0].message


def test_directory_map_flags_unlisted_sibling(tmp_path: Path) -> None:
    """A directory the map enumerates must be enumerated completely."""
    (tmp_path / "docs").mkdir()
    for name in ("listed.md", "added-later.md"):
        (tmp_path / "docs" / name).write_text("x", encoding="utf-8")
    doc = _write_map(tmp_path, "├── docs/\n│   └── listed.md\n")

    findings = DocsValidator().validate_file(doc)
    assert [f.category for f in findings] == ["directory_map"]
    assert "added-later.md" in findings[0].message


def test_directory_map_respects_ellipsis_as_partial_listing(tmp_path: Path) -> None:
    """An explicit ellipsis marks the listing partial and waives completeness."""
    (tmp_path / "docs").mkdir()
    for name in ("listed.md", "unlisted.md"):
        (tmp_path / "docs" / name).write_text("x", encoding="utf-8")
    doc = _write_map(tmp_path, "├── docs/\n│   ├── listed.md\n│   └── ...\n")

    assert DocsValidator().validate_file(doc) == []


def test_directory_map_ignores_non_map_box_drawing_art(tmp_path: Path) -> None:
    """Trace waterfalls use the same glyphs and must not be read as a filesystem map."""
    doc = tmp_path / "README.md"
    trace = (
        "[0.000s - 4.820s] root_task: Refactor (4820ms)\n"
        "  ├── [0.050s - 1.250s] subagent: Planning (1200ms)\n"
        "  └── [1.300s - 3.450s] subagent: Synthesize (2150ms)\n"
    )
    doc.write_text(f"# Trace\n\n```text\n{trace}```\n", encoding="utf-8")

    assert DocsValidator().validate_file(doc) == []


def test_directory_map_tolerates_dot_directories_and_caches(tmp_path: Path) -> None:
    """Hidden entries and bytecode caches are never expected in a map."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "__pycache__").mkdir()
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    doc = _write_map(tmp_path, "├── src/\n│   └── app.py\n")

    assert DocsValidator().validate_file(doc) == []


def test_validate_file_and_validate_content_run_identical_rules(tmp_path: Path) -> None:
    """Both entry points must share one rule registry; parallel lists silently diverge."""
    doc = tmp_path / "obs.md"
    content = "# Doc\n\n```mermaid\ngraph TD\n    A --> B\n```\n"
    doc.write_text(content, encoding="utf-8")

    validator = DocsValidator()
    from_file = [(f.category, f.line_number) for f in validator.validate_file(doc)]
    from_content = [(f.category, f.line_number) for f in validator.validate_content(content, doc)]
    assert from_file == from_content != []


def test_docs_validator_cli_accepts_multiple_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pre-commit passes N filenames; every supplied document must be validated."""
    clean = tmp_path / "clean.md"
    clean.write_text("# Clean\nNothing wrong here.", encoding="utf-8")
    broken = tmp_path / "broken.md"
    broken.write_text("# Broken\n\n```python\ndef unclosed(", encoding="utf-8")

    exit_code = docs_validator_main([str(clean), str(broken)])
    captured = capsys.readouterr().out
    assert exit_code == 1
    assert "broken.md" in captured and "clean.md" not in captured


def test_docs_validator_cli_aggregates_totals_across_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ensure counts from every target are merged rather than reported per-file."""
    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / name).write_text(f"# {name}\nClean content.", encoding="utf-8")

    exit_code = docs_validator_main([str(tmp_path / "a.md"), str(tmp_path / "b.md"), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert (exit_code, data["total_files"], data["is_valid"]) == (0, 2, True)


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
    """Verify that all markdown documents in the Vibes repository pass validation.

    This is the repository's single whole-corpus sweep. Observation structure findings
    are errors, so this assertion subsumes a per-observation structure pass; a separate
    real-repo structure test duplicated it at 0.26s per run and was removed.
    """
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


_FULL_OBSERVATION = """\
# Streaming Reasoning Token Parsers

## 1. Executive Context & Baseline
devops-cli production context.

## 2. The Observed Phenomenon
<think> tags leaked into terminal output.

## 3. The Underlying Failure Mode or Catalyst
Missing boundary buffer in streaming FSM.

## 4. Remediation & Architectural Pattern
Introduced bounded prefix accumulator.

## 5. Verifiable Impact & Key Takeaways
100% containment rate.
"""


def test_observation_structure_valid(tmp_path: Path) -> None:
    """Observation with all 5 sections passes with zero structure findings."""
    obs_dir = tmp_path / "observations" / "devops-cli"
    obs_dir.mkdir(parents=True)
    doc = obs_dir / "13-streaming-reasoning.md"
    doc.write_text(_FULL_OBSERVATION, encoding="utf-8")
    validator = DocsValidator()
    findings = [f for f in validator.validate_file(doc) if f.category == "observation_structure"]
    assert (len(findings), findings) == (0, [])


def test_observation_structure_missing_sections(tmp_path: Path) -> None:
    """Observation with only ## 1. reports sections 2-5 as missing."""
    obs_dir = tmp_path / "observations" / "devops-cli"
    obs_dir.mkdir(parents=True)
    doc = obs_dir / "99-incomplete.md"
    doc.write_text("# Incomplete\n\n## 1. Executive Context & Baseline\nSome context.\n", encoding="utf-8")
    validator = DocsValidator()
    findings = [f for f in validator.validate_file(doc) if f.category == "observation_structure"]
    missing_numbers = {int(f.message.split("## ")[1].split(".")[0]) for f in findings}
    expected = set(range(2, OBSERVATION_REQUIRED_SECTION_COUNT + 1))
    assert (len(findings), missing_numbers) == (OBSERVATION_REQUIRED_SECTION_COUNT - 1, expected)


def test_observation_structure_non_observation_file(tmp_path: Path) -> None:
    """Non-observation files (patterns/, docs/) are exempt from section structure checks."""
    doc = tmp_path / "patterns" / "some-pattern.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Pattern\n\nNo required sections here.\n", encoding="utf-8")
    validator = DocsValidator()
    findings = [f for f in validator.validate_file(doc) if f.category == "observation_structure"]
    assert (len(findings), findings) == (0, [])




def test_pattern_header_requires_the_canonical_fields(tmp_path: Path) -> None:
    """Three competing header vocabularies had accumulated before this was mechanical."""
    patterns = tmp_path / "patterns"
    patterns.mkdir()
    doc = patterns / "some-pattern.md"
    doc.write_text("# Pattern: Some Pattern\n\n> **Category**: Legacy vocabulary\n\n## Problem Statement\n", encoding="utf-8")

    findings = DocsValidator().validate_file(doc)
    assert [f.category for f in findings] == ["pattern_header"]
    assert "Pattern Class" in findings[0].message


def test_pattern_header_accepts_the_canonical_block(tmp_path: Path) -> None:
    """Reference Implementation stays optional: not every pattern has executable code."""
    patterns = tmp_path / "patterns"
    patterns.mkdir()
    doc = patterns / "some-pattern.md"
    doc.write_text(
        "# Pattern: Some Pattern\n\n> **Pattern Class**: Governance\n"
        "> **Problem**: A thing goes wrong\n> **Solution**: Stop it going wrong\n\n## Problem Statement\n",
        encoding="utf-8",
    )
    assert DocsValidator().validate_file(doc) == []


def test_pattern_header_rule_ignores_non_pattern_documents(tmp_path: Path) -> None:
    """Observations and docs carry no such header and must not be faulted for it."""
    doc = tmp_path / "notes.md"
    doc.write_text("# Notes\n\nNo metadata block here.\n", encoding="utf-8")
    assert DocsValidator().validate_file(doc) == []
