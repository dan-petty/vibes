"""Unit tests for Go Concurrency & Goroutine Leak Sentinel.

Validates:
- Parsing multi-goroutine raw stack traces into structured models.
- State categorization (chan send, chan receive, select, io wait, running).
- System goroutine daemon filtering.
- Leak detection for blocked channels and context abandonment.
- Concurrency safety scoring and severity rating.
- CLI demo execution and JSON serialization.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

# Add directory to sys.path for direct imports
_curr_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_curr_dir))

import pytest

from go_leak_sentinel import (
    SAMPLE_CLEAN_TRACE,
    SAMPLE_LEAKY_TRACE,
    ConcurrencyAuditReport,
    GoroutineLeakSentinel,
    GoroutineStackParser,
    GoroutineState,
    LeakSeverity,
    main,
    run_demo,
)


def test_parse_clean_trace() -> None:
    """Verify parsing clean runtime stack trace returns correct counts and states."""
    profiles = GoroutineStackParser.parse_trace(SAMPLE_CLEAN_TRACE)
    assert len(profiles) == 2

    p1, p2 = profiles[0], profiles[1]
    actual_p1 = (p1.goroutine_id, p1.state_category, p1.is_system)
    expected_p1 = (1, GoroutineState.RUNNING, False)

    actual_p2 = (p2.goroutine_id, p2.is_system)
    expected_p2 = (2, True)

    assert (actual_p1, actual_p2) == (expected_p1, expected_p2)


def test_audit_clean_trace_score() -> None:
    """Verify clean trace achieves 100.0 concurrency score and CLEAN severity."""
    profiles = GoroutineStackParser.parse_trace(SAMPLE_CLEAN_TRACE)
    report = GoroutineLeakSentinel.audit(profiles)

    actual = (report.total_goroutines, report.system_goroutines, report.leaked_goroutines, report.score, report.severity)
    expected = (2, 1, 0, 100.0, LeakSeverity.CLEAN)
    assert actual == expected


def test_parse_leaky_trace_identifies_defects() -> None:
    """Verify leaky trace correctly diagnoses blocked channel send and select."""
    profiles = GoroutineStackParser.parse_trace(SAMPLE_LEAKY_TRACE)
    report = GoroutineLeakSentinel.audit(profiles)

    actual = (report.total_goroutines, report.leaked_goroutines, report.severity, report.score)
    expected = (4, 2, LeakSeverity.CRITICAL, 50.0)
    assert actual == expected

    # Verify structured remediation messages generated
    assert len(report.remediations) == 2
    rem_text = " ".join(report.remediations)
    assert ("Channel send blocked" in rem_text, "ctx.Done" in rem_text) == (True, True)


def test_report_json_serialization() -> None:
    """Verify audit report converts to valid, deserializable JSON."""
    profiles = GoroutineStackParser.parse_trace(SAMPLE_LEAKY_TRACE)
    report = GoroutineLeakSentinel.audit(profiles)
    data = report.to_dict()

    serialized = json.dumps(data)
    loaded = json.loads(serialized)

    actual = (loaded["leaked_goroutines"], loaded["severity"], loaded["score"])
    expected = (2, "CRITICAL", 50.0)
    assert actual == expected


def test_cli_demo_execution() -> None:
    """Verify run_demo executes successfully with returncode 0."""
    result = run_demo()
    assert result == 0


def test_cli_scan_file(tmp_path: Path) -> None:
    """Verify CLI --scan command correctly analyzes stack dump file on disk."""
    dump_file = tmp_path / "stack.dump"
    dump_file.write_text(SAMPLE_LEAKY_TRACE, encoding="utf-8")

    # Scan leaky file returns exit code 1 (defect present)
    rc_leak = main(["--scan", str(dump_file)])
    assert rc_leak == 1

    # Scan clean file returns exit code 0
    clean_file = tmp_path / "clean.dump"
    clean_file.write_text(SAMPLE_CLEAN_TRACE, encoding="utf-8")
    rc_clean = main(["--scan", str(clean_file), "--json"])
    assert rc_clean == 0
