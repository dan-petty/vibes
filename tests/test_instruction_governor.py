"""Automated unit tests for the JIT instruction governor and ablation engine."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from instruction_governor import (
    AblationAuditor,
    JITEnvelopeSynthesizer,
    RetentionPriority,
    RuleAttributionMatrix,
    estimate_tokens,
    main,
    parse_instruction_sections,
)

SAMPLE_MARKDOWN: Final[str] = """# Root Guide
Introduction to engineering.

## 1. Architectural Invariants
Rule CC001 and ND001 enforce structural bounds.

## 2. Formatting & Visual Aesthetics
Rule DOC012 ensures linebreak hygiene.
""" + ("Extra padding words to expand token count for ablation test. " * 60)


def test_estimate_tokens() -> None:
    """Verify whitespace-based token estimation contract."""
    res_empty = estimate_tokens("")
    res_single = estimate_tokens("hello")
    res_multiple = estimate_tokens("one two three four five")
    assert (res_empty, res_single, res_multiple) == (0, 1, 5)


def test_parse_instruction_sections() -> None:
    """Verify parsing Markdown sections into structured metadata records."""
    sections = parse_instruction_sections(SAMPLE_MARKDOWN)
    assert len(sections) == 3
    sec1, sec2, sec3 = sections[0], sections[1], sections[2]

    actual_metadata = (
        (sec1.heading, sec1.level, sec1.rule_codes),
        (sec2.heading, sec2.level, sec2.rule_codes),
        (sec3.heading, sec3.level, sec3.rule_codes),
    )
    expected_metadata = (
        ("Root Guide", 1, ()),
        ("1. Architectural Invariants", 2, ("CC001", "ND001")),
        ("2. Formatting & Visual Aesthetics", 2, ("DOC012",)),
    )
    assert actual_metadata == expected_metadata


def test_parse_instruction_sections_fallback() -> None:
    """Verify fallback parsing for text without Markdown headings."""
    plain_text = "Plain unheaded instructions with ZT001 code."
    sections = parse_instruction_sections(plain_text)
    assert len(sections) == 1
    sec = sections[0]
    assert (sec.heading, sec.level, sec.rule_codes) == ("Root", 1, ("ZT001",))


def test_rule_attribution_matrix() -> None:
    """Verify mapping between mechanical gate codes and instruction sections."""
    sections = parse_instruction_sections(SAMPLE_MARKDOWN)
    matrix = RuleAttributionMatrix(sections)
    attributions = matrix.build_attributions()
    assert len(attributions) > 0

    attr_map = {a.gate_code: (a.section_heading, a.is_mechanically_enforced) for a in attributions}
    cc001_info = attr_map.get("CC001")
    doc012_info = attr_map.get("DOC012")

    assert (cc001_info, doc012_info) == (
        ("1. Architectural Invariants", True),
        ("2. Formatting & Visual Aesthetics", True),
    )


def test_jit_envelope_synthesizer_domains() -> None:
    """Verify file path domain detection logic."""
    synthesizer = JITEnvelopeSynthesizer()
    paths = [
        "src/app.py",
        "docs/spec.md",
        "infra/module.rs",
        ".github/workflows/ci.yml",
        "notes.txt",
    ]
    domains = synthesizer.resolve_domains(paths)
    assert domains == {"python", "docs", "polyglot", "workflows"}


def test_jit_envelope_synthesizer_synthesis() -> None:
    """Verify prompt synthesis assembling Tier 1 and Tier 2 overlays."""
    synthesizer = JITEnvelopeSynthesizer()
    envelope = synthesizer.synthesize(["tools/test.py", "docs/guide.md"])

    has_kernel = "Tier 1: Universal Architectural Invariants" in envelope.prompt_text
    has_python = "Domain: Python & Pytest Contracts" in envelope.prompt_text
    has_docs = "Domain: Documentation & Markdown" in envelope.prompt_text
    has_workflows = "Domain: GitHub Actions & CI" in envelope.prompt_text

    assert (has_kernel, has_python, has_docs, has_workflows) == (True, True, True, False)
    assert (envelope.tier1_tokens > 0, envelope.tier2_tokens > 0, envelope.total_tokens > 0) == (
        True,
        True,
        True,
    )


def test_ablation_auditor() -> None:
    """Verify ablation candidate evaluation and priority scoring."""
    sections = parse_instruction_sections(SAMPLE_MARKDOWN)
    auditor = AblationAuditor(sections)
    candidates = auditor.evaluate_candidates()
    assert len(candidates) == 3

    cand_map = {c.heading: (c.priority, c.mechanically_covered) for c in candidates}
    c_invariants = cand_map.get("1. Architectural Invariants")
    c_formatting = cand_map.get("2. Formatting & Visual Aesthetics")

    assert (c_invariants, c_formatting) == (
        (RetentionPriority.CRITICAL, True),
        (RetentionPriority.ABLATION_CANDIDATE, True),
    )


def test_cli_audit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI audit command output."""
    test_file = tmp_path / "instructions.md"
    test_file.write_text(SAMPLE_MARKDOWN, encoding="utf-8")

    exit_code = main(["audit", "--path", str(test_file)])
    captured = capsys.readouterr()
    assert (exit_code, "Instruction Audit for" in captured.out) == (0, True)

    exit_code_json = main(["audit", "--path", str(test_file), "--json"])
    captured_json = capsys.readouterr()
    parsed = json.loads(captured_json.out)
    assert (exit_code_json, parsed["section_count"]) == (0, 3)


def test_cli_attribute(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI attribute command output."""
    test_file = tmp_path / "instructions.md"
    test_file.write_text(SAMPLE_MARKDOWN, encoding="utf-8")

    exit_code = main(["attribute", "--path", str(test_file)])
    captured = capsys.readouterr()
    assert (exit_code, "Rule Attribution Matrix:" in captured.out) == (0, True)

    exit_code_json = main(["attribute", "--path", str(test_file), "--json"])
    captured_json = capsys.readouterr()
    parsed = json.loads(captured_json.out)
    assert (exit_code_json, len(parsed) > 0) == (0, True)


def test_cli_synthesize(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI synthesize command output."""
    exit_code = main(["synthesize", "main.py"])
    captured = capsys.readouterr()
    assert (exit_code, "Domain: Python & Pytest Contracts" in captured.out) == (0, True)

    exit_code_json = main(["synthesize", "main.py", "--json"])
    captured_json = capsys.readouterr()
    parsed = json.loads(captured_json.out)
    assert (exit_code_json, parsed["domains"]) == (0, ["python"])


def test_cli_ablate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI ablate command output."""
    test_file = tmp_path / "instructions.md"
    test_file.write_text(SAMPLE_MARKDOWN, encoding="utf-8")

    exit_code = main(["ablate", "--path", str(test_file)])
    captured = capsys.readouterr()
    assert (exit_code, "Ablation Candidates for" in captured.out) == (0, True)

    exit_code_json = main(["ablate", "--path", str(test_file), "--json"])
    captured_json = capsys.readouterr()
    parsed = json.loads(captured_json.out)
    assert (exit_code_json, len(parsed) == 3) == (0, True)


def test_cli_missing_file() -> None:
    """Verify error handling on non-existent instruction file."""
    assert (
        main(["audit", "--path", "non_existent_file.md"]),
        main(["ablate", "--path", "non_existent_file.md"]),
    ) == (1, 1)
