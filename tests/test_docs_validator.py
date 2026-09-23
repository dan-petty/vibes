"""Tests for the Vibes Documentation Syntax, Code Fence, Mermaid, and Link Validator."""

# sentinel: allow[ZeroTrustSanitization] — fixtures asserting the prose sanitization rule fires

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

# Add tools/ to sys.path for direct imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import doc_core
from doc_core import PathOracle
from doc_rules_structure import check_directory_maps
from docs_validator import (
    OBSERVATION_REQUIRED_SECTION_COUNT,
    DocsValidator,
    auto_fix_content,
    contrast_ratio,
    mask_code_spans,
)
from docs_validator import (
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


def test_auto_fix_is_a_round_trip_when_there_is_nothing_to_fix() -> None:
    """A repair pass that reports no repairs must return the document it was given.

    `splitlines()` discards the final terminator and the rejoin used to restore it only
    when the joined text did not already end in a newline. A document ending in two or
    more newlines splits to a trailing empty element, so the join ended in a newline for
    the wrong reason and one blank line was dropped on every call — silently, because the
    repair count stayed at zero. Found by `tools/fuzz_harness.py`, which minimized it to
    the four-line case kept in `artifacts/fuzz-corpus/docs_fix/`.
    """
    documents = ["```mermaid\n```\n\n\n", "# Heading\n\n\n\n", "text\n\n", "text\n", "text", ""]
    results = [auto_fix_content(document, Path("case.md")) for document in documents]
    assert [text for text, _ in results] == documents
    assert [count for _, count in results] == [0] * len(documents)


def test_auto_fix_converges_in_one_application() -> None:
    """A repair a second pass would change again is a hook that never reaches a fixed point."""
    broken = "```python\nprint(1)\n\n\n"
    once, _ = auto_fix_content(broken, Path("case.md"))
    twice, second_pass = auto_fix_content(once, Path("case.md"))
    assert (twice, second_pass) == (once, 0)


def _run_concurrent_validations(
    validator: DocsValidator, doc_content: str, repetitions: int = 10
) -> Exception | None:
    """Helper running repeated validations in a single thread, returning any caught exception."""
    try:
        for _ in range(repetitions):
            findings = validator.validate_content(doc_content)
            assert len(findings) == 0
        return None
    except Exception as exc:
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


def test_documentation_sanitization_flags_a_concrete_private_address(tmp_path: Path) -> None:
    """Markdown was never checked for RFC 1918 addresses; only Python was."""
    doc = tmp_path / "notes.md"
    doc.write_text("Point the agent at http://192.168.1.50:11434 to reproduce.\n", encoding="utf-8")

    findings = DocsValidator().validate_file(doc)
    assert [f.category for f in findings] == ["sanitization"]
    assert "192.168.1.50" in findings[0].message


def test_documentation_sanitization_permits_cidr_ranges_stating_the_rule(tmp_path: Path) -> None:
    """A range is how the policy is written down; a host address is somebody's machine."""
    doc = tmp_path / "policy.md"
    doc.write_text("Never publish 10.0.0.0/8, 172.16.0.0/12 or 192.168.0.0/16.\n", encoding="utf-8")
    assert DocsValidator().validate_file(doc) == []


def test_documentation_sanitization_permits_rfc5737_and_metadata_constants(tmp_path: Path) -> None:
    """Flagging the mandated documentation ranges would forbid following the policy."""
    doc = tmp_path / "examples.md"
    doc.write_text(
        "Use 192.0.2.1, 198.51.100.7 or 203.0.113.9. Block 169.254.169.254.\n", encoding="utf-8"
    )
    assert DocsValidator().validate_file(doc) == []


def test_documentation_sanitization_flags_internal_hostnames(tmp_path: Path) -> None:
    """A `.lan` hostname names a real machine as surely as its address does."""
    doc = tmp_path / "setup.md"
    doc.write_text("Deploy to node1.homelab.lan and verify.\n", encoding="utf-8")
    assert [f.category for f in DocsValidator().validate_file(doc)] == ["sanitization"]


def test_sanitization_waiver_requires_a_justification(tmp_path: Path) -> None:
    """An unexplained waiver is how a real leak gets silenced."""
    validator = DocsValidator()
    bare = tmp_path / "bare.md"
    bare.write_text("<!-- docs: allow[sanitization] -->\n\nSee 10.0.0.5 here.\n", encoding="utf-8")
    assert [f.category for f in validator.validate_file(bare)] == ["sanitization"]

    justified = tmp_path / "justified.md"
    justified.write_text(
        "<!-- docs: allow[sanitization] — quotes a detector fixture by design -->\n\nSee 10.0.0.5 here.\n",
        encoding="utf-8",
    )
    assert validator.validate_file(justified) == []


def test_directory_map_ignores_build_artifacts(tmp_path: Path) -> None:
    """A map describes what a reader navigates, not what a build produced."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "pkg.egg-info").mkdir()
    (tmp_path / "pkg.egg-info" / "PKG-INFO").write_text("meta", encoding="utf-8")
    (tmp_path / "build").mkdir()
    doc = tmp_path / "README.md"
    doc.write_text("# Map\n\n```text\n├── src/\n│   └── app.py\n```\n", encoding="utf-8")

    assert DocsValidator().validate_file(doc) == []


def test_path_oracle_answers_from_one_scan_per_directory(tmp_path: Path) -> None:
    """The oracle must collapse repeated questions about one directory into a single scan."""
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    scans: list[Path] = []
    real_scandir = os.scandir

    def counting_scandir(path: Any) -> Any:
        scans.append(Path(path))
        return real_scandir(path)

    oracle = PathOracle()
    with mock.patch.object(doc_core.os, "scandir", counting_scandir):
        kinds = [
            oracle.kind(tmp_path / "a.md"),
            oracle.kind(tmp_path / "sub"),
            oracle.kind(tmp_path / "missing.md"),
            oracle.kind(tmp_path / "a.md"),
        ]
    assert (kinds, scans.count(tmp_path)) == (["file", "dir", None, "file"], 1)


def test_path_oracle_resolves_paths_absent_from_their_parent_listing(tmp_path: Path) -> None:
    """`a/..` and a filesystem root name no entry in any listing, so they need a direct stat."""
    (tmp_path / "sub").mkdir()
    oracle = PathOracle()
    assert (
        oracle.kind(tmp_path / "sub" / ".."),
        oracle.kind(Path(tmp_path.root)),
        oracle.exists(tmp_path / "sub" / ".." / "sub"),
        oracle.exists(tmp_path / "gone" / ".."),
    ) == ("dir", "dir", True, False)


def test_validate_directory_shares_one_oracle_across_every_document(tmp_path: Path) -> None:
    """A sweep must not rescan a directory once per document that asks about it."""
    for index in range(4):
        (tmp_path / f"doc{index}.md").write_text(
            f"# Doc {index}\n\nSee [sibling](doc0.md) and [target](target.txt).\n", encoding="utf-8"
        )
    (tmp_path / "target.txt").write_text("x", encoding="utf-8")
    scans: list[Path] = []
    real_scandir = os.scandir

    def counting_scandir(path: Any) -> Any:
        scans.append(Path(path))
        return real_scandir(path)

    with mock.patch.object(doc_core.os, "scandir", counting_scandir):
        report = DocsValidator().validate_directory(tmp_path)
    # Two scans of the directory: one walk to discover the documents, and one oracle
    # scan shared by all four. A per-document cache scanned it once per document.
    assert (report.is_valid, report.total_files, scans.count(tmp_path)) == (True, 4, 2)


def test_directory_map_rule_skips_documents_that_draw_no_tree(tmp_path: Path) -> None:
    """A document with no text block must not touch the filesystem to prove it has no map."""
    doc = tmp_path / "plain.md"
    doc.write_text("# Plain\n\nProse only, no tree.\n", encoding="utf-8")
    lines = doc.read_text(encoding="utf-8").splitlines()
    with mock.patch.object(doc_core.os, "scandir", side_effect=AssertionError("scanned")):
        assert check_directory_maps(lines, doc) == []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("see `[text](url)` here", "see               here"),
        ("real [a](./README.md) link", "real [a](./README.md) link"),
        ("``a ` b`` and [x](y)", "          and [x](y)"),
        ("`![alt](src)` rendered", "              rendered"),
        ("unclosed `backtick [a](b)", "unclosed `backtick [a](b)"),
    ],
)
def test_a_link_inside_backticks_is_not_a_link(line: str, expected: str) -> None:
    """Any document *about* Markdown reported its own examples as broken links.

    `[text](url)` in prose resolved `url` as a path and said it did not exist. Masking
    rather than deleting keeps every other column where it was, so a finding elsewhere on
    the line still points at the right place. An unclosed span is left alone, because
    guessing where it ends would blank the rest of a line that is ordinary prose.
    """
    assert mask_code_spans(line) == expected


def test_a_document_full_of_markdown_examples_validates_clean(tmp_path: Path) -> None:
    """The regression, end to end rather than on the helper.

    The crawler's README describes the Markdown its extractor emits, and every example in
    it was reported as a broken link before code spans were masked.
    """
    document = tmp_path / "README.md"
    document.write_text(
        "# Title\n\nLinks are inlined as `[text](url)` and images as `![alt](src)`.\n",
        encoding="utf-8",
    )
    assert [f for f in DocsValidator().validate_file(document) if f.category == "link"] == []


@pytest.mark.parametrize(
    ("address", "leaks"),
    [
        ("0.0.0.0", False),
        ("255.255.255.255", False),
        ("127.0.0.1", False),
        ("8.8.8.8", False),
        ("10.1.2.3", True),
        ("192.168.1.5", True),
        ("172.16.0.9", True),
        ("100.64.0.1", True),
    ],
)
def test_a_bind_address_is_not_a_homelab_leak(address: str, leaks: bool) -> None:
    """`is_private` is not RFC 1918, and the difference was a false positive.

    CPython documents `is_private` as "not globally reachable by iana-ipv4-special-registry",
    which also covers `0.0.0.0` and `255.255.255.255`. That made the sanitization rule reject
    `endpoint: 0.0.0.0:4317` — a bind-to-all directive copied from this repository's own
    collector config, naming nobody's machine — as a leak, in the one rule whose false
    positives are the most expensive to argue with.
    """
    import ipaddress

    from doc_rules_structure import _leaks

    assert _leaks(ipaddress.ip_address(address)) is leaks


# --- CommonMark conformance ------------------------------------------------------------------


def test_a_link_title_is_not_part_of_the_destination(tmp_path: Path) -> None:
    """CommonMark §6.3: `[text](destination "title")`, title optional and separated.

    Capturing everything up to `)` resolved the path `README.md "The project readme"`,
    which exists nowhere — a false broken link on every correctly-titled link.
    """
    (tmp_path / "README.md").write_text("# R\n", encoding="utf-8")
    index = tmp_path / "index.md"
    index.write_text('# I\n\n[Readme](README.md "The project readme")\n', encoding="utf-8")
    assert [f for f in DocsValidator().validate_file(index) if f.category == "link"] == []


def test_an_angle_bracketed_destination_resolves(tmp_path: Path) -> None:
    """§6.3's other form: anything at all between `<` and `>`."""
    (tmp_path / "a b.md").write_text("# A\n", encoding="utf-8")
    index = tmp_path / "index.md"
    index.write_text("# I\n\n[Spaced](<a b.md>)\n", encoding="utf-8")
    assert [f for f in DocsValidator().validate_file(index) if f.category == "link"] == []


def test_a_tilde_block_does_not_silence_the_rest_of_the_document(tmp_path: Path) -> None:
    """§4.5: a closing fence must use the same character as the opener.

    Toggling on any line starting with ``` or ~~~ meant a `~~~markdown` block containing a
    backtick fence closed early and inverted the state — every link, tag and heading after
    it judged in the wrong context, and judged silently.
    """
    document = tmp_path / "x.md"
    document.write_text(
        "# Fencing\n\nTo open a fenced block, write:\n\n~~~markdown\n```\n~~~\n\n"
        "## Reference\n\nSee [the plan](does-not-exist.md) and <details>.\n",
        encoding="utf-8",
    )
    categories = sorted({f.category for f in DocsValidator().validate_file(document)})
    assert categories == ["html_tag", "link"]


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Setup & Install", "setup--install"),
        ("snake_case heading", "snake_case-heading"),
        ("A  Double  Space", "a--double--space"),
        ("Plain Heading", "plain-heading"),
        ("8. Protocol & Tooling", "8-protocol--tooling"),
    ],
)
def test_heading_slugs_match_the_generator_github_uses(heading: str, slug: str) -> None:
    """`github-slugger` strips rejected characters then replaces **each** space with one hyphen.

    Folding runs and underscores to single hyphens made the validator reject the anchors
    GitHub actually emits — and, worse, accept two links in this repository's own AGENTS.md
    that are broken on GitHub today. The corrected algorithm found them on its first run.
    """
    from docs_validator import slugify_heading

    assert slugify_heading(heading) == slug


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["~~~md", "```", "~~~", "after"], [True, True, True, False]),
        (["```py", "x = 1", "```", "after"], [True, True, True, False]),
        (["````", "```", "````", "after"], [True, True, True, False]),
        (["```", "~~~", "```", "after"], [True, True, True, False]),
        (["```", "text", "``` info", "still inside"], [True, True, True, True]),
    ],
)
def test_fence_tracking_honours_the_marker_its_length_and_the_info_string(
    lines: list[str], expected: list[bool]
) -> None:
    """One implementation, because five call sites had the same naive toggle.

    The last case is §4.5's rule that a closing fence carries no info string, so ```` info`
    does not close the block it appears in.
    """
    from doc_core import fenced_line_flags

    assert fenced_line_flags(lines) == expected


# --- Driven off the parser, not off the lines -------------------------------------------


def _link_findings(body: str, tmp_path: Path) -> list[str]:
    """Return the link findings for one document, with a real README beside it."""
    (tmp_path / "README.md").write_text("# R\n", encoding="utf-8")
    document = tmp_path / "doc.md"
    document.write_text(body, encoding="utf-8")
    return [f.message for f in DocsValidator().validate_file(document) if f.category == "link"]


@pytest.mark.parametrize(
    ("body", "clean"),
    [
        ('[Readme](README.md "The project readme")\n', True),
        ("Use `[text](does-not-exist.md)` in prose.\n", True),
        ("# T\n\n    example [a](does-not-exist.md)\n", True),
        ("# T\n\n<!-- multi\nline [c](does-not-exist.md) -->\n", True),
        ("# T\n\n[gone](does-not-exist.md)\n", False),
    ],
    ids=["title", "code-span", "indented-code", "html-comment", "really-broken"],
)
def test_links_come_from_the_parser_not_from_the_lines(
    body: str, clean: bool, tmp_path: Path
) -> None:
    """Four defects dissolve rather than get fixed.

    A link title is an attribute, so it can never be read as part of the destination; a
    `[text](url)` in a code span is a `code_inline` child and never a link; an indented
    chunk is a `code_block`; a multi-line `<!-- -->` is one `html_block`. None of that
    needed a rule — it needed the parser that was already imported in this file.
    """
    assert (_link_findings(body, tmp_path) == []) is clean


def test_a_percent_encoded_target_resolves_to_the_file_it_names(tmp_path: Path) -> None:
    """RFC 3986 §2.1: `%20` is a space, so a link to a file with one must write it that way.

    Resolving the raw text reported a file that exists as missing, and the author's only
    remedy was to break the link.
    """
    (tmp_path / "a b.md").write_text("# AB\n", encoding="utf-8")
    assert _link_findings("# T\n\n[e](a%20b.md)\n", tmp_path) == []


def test_a_setext_heading_produces_an_anchor() -> None:
    """The line scan knew only the ATX form, so links to setext headings were reported broken."""
    from docs_validator import extract_heading_anchors

    anchors = extract_heading_anchors("Setext Heading\n--------------\n\n## Setup & Install\n")
    assert anchors == {"setext-heading", "setup--install"}


def test_an_anchor_must_match_exactly(tmp_path: Path) -> None:
    """The substring test accepted `#install` against a page whose only heading is Installation.

    In both directions — a link GitHub cannot resolve passed the gate whose job is
    resolving links.
    """
    body = "# Installation\n\nJump to [it](#install).\n"
    assert _link_findings(body, tmp_path) != []


@pytest.mark.parametrize("colour", ["#abc", "#abcd", "#aabbcc", "#aabbccdd"])
def test_every_css_hex_length_is_a_colour(colour: str) -> None:
    """CSS Color 4 §5.1 defines four lengths; two of them raised `ValueError` out of `int()`.

    Uncaught, so a 4-digit fill killed the single-file run and, through
    `ThreadPoolExecutor.map`, the whole directory sweep — a traceback in place of a finding.
    """
    from doc_rules_mermaid import contrast_ratio

    assert contrast_ratio(colour, "#ffffff") > 0
