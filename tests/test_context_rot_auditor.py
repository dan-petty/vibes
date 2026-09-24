"""Unit and integration test suite for Attention Dilution & Context Rot Auditor.

Validates token estimation, transcript parsing, lost-in-the-middle invariant detection,
observation bloat detection, repetitive error deduplication, and active context compaction.
Consolidates assertions into structural tuple checks to maintain proactive complexity headroom.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from context_rot_auditor import (
    ActiveContextCompactor,
    ContextMessage,
    ContextRotAuditor,
    MessageRole,
    RotSeverity,
    estimate_tokens,
    export_json,
    export_sarif,
    format_markdown_report,
    main,
    parse_transcript_text,
)


def test_estimate_tokens() -> None:
    """Verify standard character-to-token ratio approximation."""
    empty_cnt = estimate_tokens("")
    text_cnt = estimate_tokens("a" * 38)
    assert (empty_cnt, text_cnt) == (0, 10)


def test_parse_jsonl_transcript() -> None:
    """Verify parsing JSONL formatted agent trajectory logs."""
    jsonl_data = (
        '{"role": "system", "content": "You are a coding assistant."}\n'
        '{"role": "user", "content": "Refactor dispatcher."}\n'
        '{"role": "tool", "content": "stdout: test passed"}\n'
    )
    messages = parse_transcript_text(jsonl_data)
    roles = [m.role for m in messages]
    assert (len(messages), roles) == (
        3,
        [MessageRole.SYSTEM, MessageRole.USER, MessageRole.TOOL],
    )


def test_parse_plaintext_turns() -> None:
    """Verify parsing plaintext turns formatted with role prefixes."""
    raw = (
        "System: Follow architectural invariant M<=6.\n"
        "User: Optimize function f.\n"
        "Assistant: Decomposed into helpers.\n"
    )
    messages = parse_transcript_text(raw)
    roles = [m.role for m in messages]
    assert (len(messages), roles) == (
        3,
        [MessageRole.SYSTEM, MessageRole.USER, MessageRole.ASSISTANT],
    )


def test_auditor_clean_transcript() -> None:
    """Verify clean transcript with high invariant density yields 0 findings."""
    messages = [
        ContextMessage(
            role=MessageRole.SYSTEM,
            content="Mandatory invariant: cyclomatic complexity M<=6, zero-trust network.",
            token_count=100,
            turn_index=0,
            position_ratio=0.0,
            is_invariant_bearing=True,
            is_tool_observation=False,
        ),
        ContextMessage(
            role=MessageRole.USER,
            content="Check status of repository gates.",
            token_count=50,
            turn_index=1,
            position_ratio=0.66,
            is_invariant_bearing=False,
            is_tool_observation=False,
        ),
    ]
    auditor = ContextRotAuditor()
    summary = auditor.audit(messages, "clean.jsonl")
    assert (len(summary.findings), summary.attention_dilution_index) == (0, 0.0)


def test_auditor_lost_in_the_middle_invariant_rot002() -> None:
    """Verify invariant constraint trapped in middle depth triggers ROT002."""
    messages = [
        ContextMessage(MessageRole.SYSTEM, "Init.", 100, 0, 0.0, False, False),
        ContextMessage(MessageRole.USER, "Turn 1.", 200, 1, 0.15, False, False),
        ContextMessage(
            MessageRole.USER,
            "Ensure invariant: complexity M<=6 and tuple equality assertions.",
            150,
            2,
            0.45,
            True,
            False,
        ),
        ContextMessage(MessageRole.ASSISTANT, "Working...", 300, 3, 0.65, False, False),
        ContextMessage(MessageRole.USER, "Done.", 100, 4, 0.90, False, False),
    ]
    auditor = ContextRotAuditor()
    summary = auditor.audit(messages, "middle_inv.jsonl")
    rules = [f.rule_id for f in summary.findings]
    assert ("ROT002" in rules, summary.findings[0].severity) == (True, RotSeverity.CRITICAL)


def test_auditor_observation_bloat_rot003() -> None:
    """Verify excessive tool output volume triggers ROT003."""
    messages = [
        ContextMessage(MessageRole.SYSTEM, "Assistant.", 100, 0, 0.0, False, False),
        ContextMessage(
            MessageRole.TOOL,
            "stdout: " + ("log line output data\n" * 500),
            3500,
            1,
            0.1,
            False,
            True,
        ),
        ContextMessage(MessageRole.ASSISTANT, "Processed.", 100, 2, 0.95, False, False),
    ]
    auditor = ContextRotAuditor()
    summary = auditor.audit(messages, "bloat.jsonl")
    rules = [f.rule_id for f in summary.findings]
    assert ("ROT003" in rules, len(summary.findings) >= 1) == (True, True)


def test_auditor_repetitive_error_chatter_rot004() -> None:
    """Verify repeated failure tracebacks trigger ROT004."""
    trace = "Traceback (most recent call last):\n  File 'app.py', line 10\nValueError: failed"
    messages = [
        ContextMessage(MessageRole.SYSTEM, "Assistant.", 100, 0, 0.0, False, False),
        ContextMessage(MessageRole.TOOL, trace, 150, 1, 0.2, False, True),
        ContextMessage(MessageRole.TOOL, trace, 150, 2, 0.5, False, True),
    ]
    auditor = ContextRotAuditor()
    summary = auditor.audit(messages, "chatter.jsonl")
    rules = [f.rule_id for f in summary.findings]
    assert ("ROT004" in rules, len(summary.findings) >= 1) == (True, True)


def test_auditor_signal_to_noise_deficit_rot005() -> None:
    """Verify noise-heavy transcript lacking invariant density triggers ROT005."""
    messages = [
        ContextMessage(MessageRole.SYSTEM, "Hello.", 20, 0, 0.0, False, False),
        ContextMessage(
            MessageRole.TOOL,
            "stdout: " + ("data " * 1000),
            2500,
            1,
            0.1,
            False,
            True,
        ),
    ]
    auditor = ContextRotAuditor()
    summary = auditor.audit(messages, "noise.jsonl")
    rules = [f.rule_id for f in summary.findings]
    assert ("ROT005" in rules, summary.effective_context_ratio < 0.10) == (True, True)


def test_active_context_compactor_observation_masking() -> None:
    """Verify verbose tool observations are truncated and masked."""
    long_output = "\n".join(f"line {i}" for i in range(50))
    messages = [
        ContextMessage(MessageRole.TOOL, long_output, 400, 0, 0.0, False, True)
    ]
    compactor = ActiveContextCompactor(max_observation_lines=6)
    compacted = compactor.compact(messages)
    masked_text = compacted.compacted_messages[0].content
    assert (
        compacted.masked_observations_count,
        "omitted by ActiveContextCompactor" in masked_text,
    ) == (1, True)


def test_active_context_compactor_invariant_repinning() -> None:
    """Verify critical invariants are re-anchored to the context suffix."""
    messages = [
        ContextMessage(
            role=MessageRole.USER,
            content="Mandatory invariant: complexity M<=6.",
            token_count=50,
            turn_index=0,
            position_ratio=0.1,
            is_invariant_bearing=True,
            is_tool_observation=False,
        ),
        ContextMessage(MessageRole.ASSISTANT, "Ok.", 20, 1, 0.9, False, False),
    ]
    compactor = ActiveContextCompactor()
    compacted = compactor.compact(messages)
    last_msg = compacted.compacted_messages[-1]
    assert (
        compacted.pinned_invariants_count,
        "### 🛡️ ANCHORED INVARIANT ENVELOPE (PINNED)" in last_msg.content,
    ) == (1, True)


def test_sarif_and_json_and_markdown_exports() -> None:
    """Verify SARIF 2.1.0, JSON, and Markdown exporters produce conformant schemas."""
    messages = [
        ContextMessage(
            MessageRole.USER,
            "Ensure invariant: M<=6.",
            100,
            0,
            0.5,
            True,
            False,
        )
    ]
    auditor = ContextRotAuditor()
    summary = auditor.audit(messages, "test_transcript.jsonl")

    sarif = export_sarif(summary, "test_transcript.jsonl")
    json_str = export_json(summary)
    parsed_json = json.loads(json_str)
    md_str = format_markdown_report(summary)

    assert (
        sarif["version"],
        len(sarif["runs"]),
        parsed_json["findings_count"],
        "# Attention Dilution & Context Rot Audit Report" in md_str,
    ) == ("2.1.0", 1, 1, True)


def test_main_cli_execution(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify main CLI execution against temporary transcript files."""
    transcript_file = tmp_path / "sample_transcript.jsonl"
    transcript_file.write_text(
        '{"role": "system", "content": "You are a coding assistant. Invariant M<=6."}\n'
        '{"role": "user", "content": "Run tests."}\n',
        encoding="utf-8",
    )

    sarif_out = tmp_path / "findings.sarif"
    rc = main([str(transcript_file), "--compact", "--export-sarif", str(sarif_out)])
    captured = capsys.readouterr()

    assert (
        rc,
        sarif_out.is_file(),
        "Attention Dilution & Context Rot Audit Report" in captured.out,
    ) == (0, True, True)
