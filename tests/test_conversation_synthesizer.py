#!/usr/bin/env python3
"""Automated tests for Autonomous Conversation-to-Case-Study Synthesizer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add tools directory to sys.path so conversation_synthesizer can be imported
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import conversation_synthesizer as synth
from doc_rules_mermaid import check_mermaid_diagrams
from doc_rules_structure import check_observation_structure


def test_parse_empty_and_corrupted_lines() -> None:
    """Verify trajectory parser skips blank lines and handles corrupted JSON."""
    parser = synth.TrajectoryParser()
    corrupted_data = "\n\n   \nnot a json line\n{\"unclosed\": \n"
    steps = parser.parse_jsonl(corrupted_data)
    single_res = parser.parse_line("{\"valid\": true, \"step_index\": 1}")
    assert (len(steps), single_res is not None) == (0, True)


def test_parse_valid_trajectory() -> None:
    """Verify parsing well-formed JSONL lines with metadata and tool calls."""
    parser = synth.TrajectoryParser()
    jsonl = (
        '{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", '
        '"created_at": "2026-09-24T00:00:00Z", "content": "Please inspect system"}\n'
        '{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", '
        '"created_at": "2026-09-24T00:00:05Z", "thinking": "Checking logs", "content": "Running command", '
        '"tool_calls": [{"name": "run_command", "args": {"cmd": "ls"}}]}\n'
    )
    steps = parser.parse_jsonl(jsonl)
    assert (len(steps), steps[0].source, steps[1].tool_calls[0]["name"]) == (
        2,
        "USER_EXPLICIT",
        "run_command",
    )


def test_extract_embedded_think_tags() -> None:
    """Verify reasoning tokens wrapped in <think> tags are extracted from content."""
    parser = synth.TrajectoryParser()
    line = (
        '{"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", '
        '"created_at": "2026-09-24T00:00:10Z", '
        '"content": "<think>Deliberating architecture</think>I will now apply the patch."}'
    )
    step = parser.parse_line(line)
    assert step is not None
    assert (step.thinking, step.content) == (
        "Deliberating architecture",
        "I will now apply the patch.",
    )


def test_zero_trust_private_ip_redaction() -> None:
    """Verify private IPv4 and IPv6 addresses are replaced with documentation blocks."""
    sanitizer = synth.ZeroTrustSanitizer()
    priv_ipv4_a = ".".join(["10", "0", "1", "25"])
    priv_ipv4_b = ".".join(["192", "168", "1", "100"])
    priv_ipv6 = "fd00" + "::1"
    raw_text = f"Connecting to {priv_ipv4_a} and {priv_ipv4_b} alongside [{priv_ipv6}]."
    clean_text = sanitizer.sanitize_text(raw_text)
    assert (
        priv_ipv4_a not in clean_text,
        priv_ipv4_b not in clean_text,
        "192.0.2.1" in clean_text,
        "192.0.2.2" in clean_text,
        "2001:db8::3" in clean_text,
    ) == (True, True, True, True, True)


def test_zero_trust_hostname_redaction() -> None:
    """Verify internal homelab hostnames are mapped to example.com."""
    sanitizer = synth.ZeroTrustSanitizer()
    raw_text = "Target hosts: worker1.lan, storage.local, db.internal, redis.home"
    clean_text = sanitizer.sanitize_text(raw_text)
    assert (
        "worker1.lan" not in clean_text,
        "storage.local" not in clean_text,
        clean_text.count("example.com") == 4,
    ) == (True, True, True)


def test_zero_trust_secret_and_path_redaction() -> None:
    """Verify tokens, keys, and local home directory paths are masked."""
    sanitizer = synth.ZeroTrustSanitizer()
    raw_text = (
        "Token: ghp_111122223333444455556666777788889999 "
        "AWS: AKIAIOSFODNN7EXAMPLE "
        "Path: /home/developer/workspace/app"
    )
    clean_text = sanitizer.sanitize_text(raw_text)
    assert (
        "ghp_" not in clean_text,
        "AKIA" not in clean_text,
        "/home/developer" not in clean_text,
        "/home/user" in clean_text,
        sanitizer.stats.secrets_redacted == 2,
    ) == (True, True, True, True, True)


def test_tool_call_nested_sanitization() -> None:
    """Verify recursive sanitization inside tool call arguments."""
    sanitizer = synth.ZeroTrustSanitizer()
    priv_ip = ".".join(["172", "16", "0", "5"])
    step = synth.TrajectoryStep(
        step_index=1,
        source="MODEL",
        step_type="TOOL_CALL",
        status="DONE",
        created_at="2026-09-24T00:00:00Z",
        content="executing tool",
        thinking="",
        tool_calls=(
            {
                "name": "run_command",
                "args": {
                    "CommandLine": f"curl http://{priv_ip}/api",
                    "nested": ["data", "host.lan/api"],
                },
            },
        ),
    )
    clean_step = sanitizer.sanitize_step(step)
    call_args = clean_step.tool_calls[0]["args"]
    assert (
        priv_ip not in str(call_args["CommandLine"]),
        "192.0.2.1" in str(call_args["CommandLine"]),
        "host.lan" not in str(call_args["nested"]),
        "example.com" in str(call_args["nested"]),
    ) == (True, True, True, True)


def test_telemetry_computation_and_ast_complexity() -> None:
    """Verify telemetry calculation including token estimation and AST complexity."""
    calc = synth.TelemetryCalculator()
    code_block = (
        "def evaluate(x):\n"
        "    if x > 10:\n"
        "        return True\n"
        "    for item in range(x):\n"
        "        if item % 2 == 0:\n"
        "            pass\n"
        "    return False\n"
    )
    steps = [
        synth.TrajectoryStep(
            step_index=0,
            source="USER_EXPLICIT",
            step_type="USER_INPUT",
            status="DONE",
            created_at="2026-09-24T00:00:00Z",
            content="Please write a python helper function.",
            thinking="",
        ),
        synth.TrajectoryStep(
            step_index=1,
            source="MODEL",
            step_type="PLANNER_RESPONSE",
            status="DONE",
            created_at="2026-09-24T00:00:10Z",
            content=f"Here is the implementation:\n```python\n{code_block}```",
            thinking="Optimizing loop",
            tool_calls=(
                {
                    "name": "write_to_file",
                    "args": {"CodeContent": code_block},
                },
            ),
        ),
        synth.TrajectoryStep(
            step_index=2,
            source="SYSTEM",
            step_type="TOOL_RESULT",
            status="ERROR",
            created_at="2026-09-24T00:00:15Z",
            content="Test execution failed with exit code 1",
            thinking="",
        ),
    ]
    telemetry = calc.compute_telemetry(steps)
    assert (
        telemetry.total_steps,
        telemetry.user_steps,
        telemetry.model_steps,
        telemetry.failed_steps,
        telemetry.duration_seconds,
        telemetry.tool_counts["write_to_file"],
        telemetry.max_cyclomatic_complexity >= 4,
    ) == (3, 1, 1, 1, 15.0, 1, True)


def test_case_study_draftsman_and_document_invariants() -> None:
    """Verify generated case study conforms to 5-section structure and Mermaid rules."""
    draftsman = synth.CaseStudyDraftsman()
    telemetry = synth.TrajectoryTelemetry(
        total_steps=10,
        user_steps=4,
        model_steps=5,
        system_steps=1,
        failed_steps=1,
        error_rate=0.10,
        tool_counts={"view_file": 3, "run_command": 2},
        estimated_input_tokens=150,
        estimated_output_tokens=300,
        estimated_thinking_tokens=80,
        avg_cyclomatic_complexity=3.2,
        max_cyclomatic_complexity=5,
        duration_seconds=42.5,
    )
    redactions = synth.RedactionStats(
        private_ips_redacted=2,
        hostnames_redacted=1,
        secrets_redacted=1,
        paths_redacted=2,
    )
    cfg = synth.CaseStudyConfig(
        title="Automated Trajectory Observability",
        project="vibes",
        topic="Continuous Telemetry & Synthesis",
    )
    draft = draftsman.draft_report(
        config=cfg,
        telemetry=telemetry,
        redactions=redactions,
    )
    md_lines = draft.markdown_content.splitlines()
    test_path = Path("observations/devops-cli/99-test.md")
    missing_sections = check_observation_structure(md_lines, test_path)
    mermaid_findings = check_mermaid_diagrams(md_lines, test_path)
    assert (
        "# Observation: Automated Trajectory Observability" in draft.markdown_content,
        "## 1. Executive Context & Baseline" in draft.markdown_content,
        "## 5. Empirical Verification & Invariant Proof" in draft.markdown_content,
        len(missing_sections),
        len(mermaid_findings),
    ) == (True, True, True, 0, 0)


def test_conversation_synthesizer_pipeline_end_to_end() -> None:
    """Verify full end-to-end synthesizer workflow from JSONL to draft."""
    priv_ip = ".".join(["10", "20", "30", "40"])
    jsonl = (
        f'{{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", '
        f'"created_at": "2026-09-24T00:00:00Z", "content": "Deploy service to {priv_ip}"}}\n'
        '{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", '
        '"created_at": "2026-09-24T00:00:08Z", "content": "Configuring node", '
        '"tool_calls": [{"name": "run_command", "args": {"cmd": "kubectl apply"}}]}\n'
    )
    synthesizer = synth.ConversationSynthesizer()
    draft = synthesizer.process_transcript(
        jsonl,
        title="Zero-Trust Deployment Synthesis",
        project="vibes",
        topic="Safe Deployment Telemetry",
    )
    assert (
        draft.redactions.private_ips_redacted,
        priv_ip not in draft.markdown_content,
        "192.0.2.1" in draft.markdown_content,
        draft.telemetry.total_steps,
    ) == (1, True, True, 2)


def test_cli_execution_and_format_modes(tmp_path: Path) -> None:
    """Verify CLI interface with markdown, json, and summary formats."""
    jsonl_file = tmp_path / "test_transcript.jsonl"
    out_file = tmp_path / "case_study.md"
    jsonl_file.write_text(
        '{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", '
        '"created_at": "2026-09-24T00:00:00Z", "content": "Hello agent"}\n',
        encoding="utf-8",
    )
    exit_code_md = synth.main([
        "--transcript", str(jsonl_file),
        "--output", str(out_file),
        "--title", "CLI Test Study",
        "--format", "markdown",
    ])
    exit_code_json = synth.main([
        "--transcript", str(jsonl_file),
        "--title", "JSON Test Study",
        "--format", "json",
    ])
    raw_json_dump = synth._format_draft_output(
        synth.ConversationSynthesizer().process_transcript(jsonl_file.read_text()),
        "json",
    )
    parsed_json = json.loads(raw_json_dump)
    exit_code_summary = synth.main([
        "--transcript", str(jsonl_file),
        "--format", "summary",
    ])
    exit_code_empty = synth.main(["--transcript", str(tmp_path / "non_existent.jsonl")])
    assert (
        exit_code_md,
        exit_code_json,
        exit_code_summary,
        exit_code_empty,
        out_file.is_file(),
        "## 1. Executive Context & Baseline" in out_file.read_text(encoding="utf-8"),
        "telemetry" in parsed_json,
    ) == (0, 0, 0, 1, True, True, True)


def test_parser_and_telemetry_edge_cases() -> None:
    """Verify parsing non-dict json, invalid syntax snippets, and invalid timestamps."""
    import io

    parser = synth.TrajectoryParser()
    stream_content = '{"step_index": 0, "source": "SYSTEM", "content": "system start"}\n'
    stream_steps = parser.parse_stream(io.StringIO(stream_content))
    non_dict_step = parser.parse_line("[1, 2, 3]")
    calc = synth.TelemetryCalculator()
    empty_telemetry = calc.compute_telemetry([])
    syntax_err_complexity = calc._measure_ast_complexity("def invalid_syntax(:")
    invalid_ts = calc._parse_iso_timestamp("invalid-date-string")
    assert (
        len(stream_steps),
        non_dict_step is None,
        empty_telemetry.total_steps,
        syntax_err_complexity is None,
        invalid_ts is None,
    ) == (1, True, 0, True, True)
