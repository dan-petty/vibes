#!/usr/bin/env python3
"""Go Concurrency & Goroutine Leak Sentinel.

Analyzes Go runtime goroutine stack traces (pprof / runtime.Stack), classifies
concurrency states, detects orphaned channel deadlocks and abandoned context
workers, and calculates a deterministic concurrency safety score.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

# Go treats a goroutine as a system goroutine when its entry function, the function that
# the `go` statement started, is in package runtime and is not `runtime.main`
# (runtime.isSystemGoroutine in src/runtime/traceback.go). A default dump and
# runtime.NumGoroutine leave these goroutines out; GOTRACEBACK=system shows them. Do not
# use `runtime.gopark` for this: every blocked goroutine parks there, user leaks included.
RUNTIME_PREFIX = "runtime."
RUNTIME_USER_ENTRIES = frozenset({"runtime.main"})
# GOTRACEBACK=system shows this frame below the entry function of every goroutine.
GOEXIT_FUNCTION = "runtime.goexit"

# Default filters of go.uber.org/goleak (isTestStack, isStdLibStack and isTraceStack in
# its options.go). They match the top function that a default dump shows. The testing
# runner functions are skipped only while they wait on a channel receive.
TEST_RUNNER_TOP_FUNCTIONS = frozenset(
    {
        "testing.RunTests",
        "testing.(*T).Run",
        "testing.(*T).Parallel",
        "testing.runFuzzing",
        "testing.runFuzzTests",
    }
)
SIGNAL_TOP_FUNCTIONS = frozenset({"os/signal.signal_recv", "os/signal.loop"})
TRACE_READER_FUNCTION = "runtime.ReadTrace"


class GoroutineState(StrEnum):
    """Categorized Go runtime execution state."""

    RUNNING = "running"
    CHAN_SEND = "chan send"
    CHAN_RECEIVE = "chan receive"
    SELECT = "select"
    IO_WAIT = "IO wait"
    SLEEP = "sleep"
    UNKNOWN = "unknown"


STATE_KEYWORDS: tuple[tuple[tuple[str, ...], GoroutineState], ...] = (
    (("chan send",), GoroutineState.CHAN_SEND),
    (("chan receive",), GoroutineState.CHAN_RECEIVE),
    (("select",), GoroutineState.SELECT),
    (("io wait", "syscall"), GoroutineState.IO_WAIT),
    (("sleep", "timer"), GoroutineState.SLEEP),
    (("running",), GoroutineState.RUNNING),
)

REMEDIATION_TEMPLATES: dict[GoroutineState, str] = {
    GoroutineState.CHAN_SEND: "[{fn}] Channel send blocked indefinitely. Ensure channel has capacity or active reader.",
    GoroutineState.SELECT: "[{fn}] Select blocked without termination. Add 'case <-ctx.Done(): return'.",
    GoroutineState.CHAN_RECEIVE: "[{fn}] Channel receive blocked. Ensure sender closes channel on termination.",
}


class LeakSeverity(StrEnum):
    """Severity classification of identified concurrency defects."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    CLEAN = "CLEAN"


@dataclass
class StackFrame:
    """Individual stack frame in a goroutine trace."""

    function: str
    file_path: str
    line: int


@dataclass
class GoroutineProfile:
    """Captured runtime profile of a single Go goroutine."""

    goroutine_id: int
    raw_state: str
    state_category: GoroutineState
    is_system: bool
    frames: list[StackFrame] = field(default_factory=list)
    created_by: str | None = None
    age_minutes: float = 0.0


@dataclass
class ConcurrencyAuditReport:
    """Consolidated audit results across all examined goroutines."""

    total_goroutines: int
    system_goroutines: int
    user_goroutines: int
    leaked_goroutines: int
    severity: LeakSeverity
    score: float
    remediations: list[str] = field(default_factory=list)
    profiles: list[GoroutineProfile] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize report to a dictionary with stringified enum values."""
        data = asdict(self)
        data["severity"] = self.severity.value
        for p in data["profiles"]:
            p["state_category"] = p["state_category"].value
        return data


def _is_hidden_runtime_function(function: str) -> bool:
    """Return True for an unexported runtime function, which a default dump does not show."""
    name = function.removeprefix(RUNTIME_PREFIX)
    return name != function and not name[:1].isupper()


def entry_function(frames: list[StackFrame]) -> str | None:
    """Return the function that the `go` statement started.

    Go prints the innermost frame first, so the entry function is the last frame, or the
    frame above `runtime.goexit` when the dump shows runtime frames.
    """
    names = [f.function for f in frames if f.function != GOEXIT_FUNCTION]
    return names[-1] if names else None


def top_function(frames: list[StackFrame]) -> str | None:
    """Return the top frame that a default dump shows, skipping frames such as `runtime.gopark`."""
    names = [f.function for f in frames]
    shown = [n for n in names if not _is_hidden_runtime_function(n)]
    return (shown or names or [None])[0]


def _is_goleak_default_ignore(frames: list[StackFrame], raw_state: str) -> bool:
    """Return True for a goroutine that the default goleak filters ignore."""
    top = top_function(frames)
    if top in TEST_RUNNER_TOP_FUNCTIONS:
        return raw_state.startswith("chan receive")
    return top in SIGNAL_TOP_FUNCTIONS or any(f.function == TRACE_READER_FUNCTION for f in frames)


def is_system_goroutine(frames: list[StackFrame], raw_state: str) -> bool:
    """Return True for a runtime goroutine, or a goroutine that goleak ignores by default."""
    entry = entry_function(frames)
    if entry is None:
        return False
    if entry.startswith(RUNTIME_PREFIX) and entry not in RUNTIME_USER_ENTRIES:
        return True
    return _is_goleak_default_ignore(frames, raw_state)


class GoroutineStackParser:
    """Parses raw text stack traces into structured GoroutineProfile models."""

    # `GOTRACEBACK=system` and `=crash` add runtime fields between the id and the state —
    # `goroutine 1 gp=0xc0000061c0 m=0 mp=0x5f8a40 [running]:`. Requiring the bracket to
    # follow the id with only whitespace between rejected every header those levels emit,
    # so a dump full of blocked goroutines parsed to nothing and the sentinel reported a
    # CLEAN 100.0/100 — the failure mode of a leak detector that cannot read the dump.
    _HEADER_RE = re.compile(r"^goroutine\s+(\d+)\s+(?:[^\[\]]*\s)?\[([^\]]+)\]:")
    # The same levels add `fp=0x... sp=0x... pc=0x...` to each frame line that is not inlined.
    # A pattern that ended at the `+0x` offset dropped those frames, so the parser kept only
    # inlined frames and the file line after `created by`.
    _FRAME_RE = re.compile(
        r"^\s+(.+):(\d+)(?:\s+\+0x[0-9a-f]+)?(?:\s+fp=0x[0-9a-f]+\s+sp=0x[0-9a-f]+\s+pc=0x[0-9a-f]+)?$"
    )
    _CREATED_RE = re.compile(r"^created by (.+) in goroutine \d+")

    @classmethod
    def parse_trace(cls, raw_text: str) -> list[GoroutineProfile]:
        """Parse multi-goroutine text dump into list of GoroutineProfiles."""
        blocks = cls._split_into_blocks(raw_text)
        return [p for b in blocks if (p := cls._parse_single_block(b)) is not None]

    @classmethod
    def _split_into_blocks(cls, raw_text: str) -> list[list[str]]:
        blocks: list[list[str]] = []
        current: list[str] = []
        for line in raw_text.strip().splitlines():
            trimmed = line.strip()
            if not trimmed:
                continue
            if trimmed.startswith("goroutine ") and current:
                blocks.append(current)
                current = []
            current.append(line)
        if current:
            blocks.append(current)
        return blocks

    @classmethod
    def _parse_single_block(cls, lines: list[str]) -> GoroutineProfile | None:
        if not lines:
            return None
        match = cls._HEADER_RE.match(lines[0].strip())
        if not match:
            return None

        gid = int(match.group(1))
        raw_state = match.group(2).strip()
        state_cat = cls._categorize_state(raw_state)

        frames, created_by = cls._extract_frames(lines[1:])
        is_sys = is_system_goroutine(frames, raw_state)

        return GoroutineProfile(
            goroutine_id=gid,
            raw_state=raw_state,
            state_category=state_cat,
            is_system=is_sys,
            frames=frames,
            created_by=created_by,
        )

    @staticmethod
    def _categorize_state(raw_state: str) -> GoroutineState:
        lower = raw_state.lower()
        for keywords, state in STATE_KEYWORDS:
            if any(k in lower for k in keywords):
                return state
        return GoroutineState.UNKNOWN

    @classmethod
    def _extract_frames(cls, lines: list[str]) -> tuple[list[StackFrame], str | None]:
        frames: list[StackFrame] = []
        created_by: str | None = None
        current_fn: str | None = None

        for line in lines:
            trimmed = line.strip()
            if trimmed.startswith("created by "):
                created_by = cls._parse_created_by(trimmed)
                continue
            current_fn = cls._handle_trace_line(line, current_fn, frames)

        return frames, created_by

    @staticmethod
    def _function_name(line: str) -> str:
        """Return the qualified function name, dropping only its argument list.

        Splitting on the *first* `(` truncated every method frame to its package:
        `testing.(*T).Run(0xc0000b8340, ...)` became `testing.`, because Go spells a pointer
        receiver `(*T)` inside the name itself. The exclusion list that skips
        `testing.(*T).Run` could therefore never match, and the runtime's own frames were
        counted as leaked goroutines. The argument list is the last parenthesised group, so
        the last `(` is the one to cut at.
        """
        head, opener, _ = line.strip().rpartition("(")
        return (head if opener else line).strip()

    @classmethod
    def _handle_trace_line(cls, line: str, current_fn: str | None, frames: list[StackFrame]) -> str | None:
        if cls._is_function_line(line):
            return cls._function_name(line)
        if current_fn:
            return cls._append_frame_if_match(line, current_fn, frames)
        return current_fn

    @classmethod
    def _parse_created_by(cls, line: str) -> str:
        match = cls._CREATED_RE.match(line)
        return match.group(1) if match else line

    @staticmethod
    def _is_function_line(line: str) -> bool:
        return not line.startswith(("\t", "    ", "  ")) and "(" in line

    @classmethod
    def _append_frame_if_match(cls, line: str, current_fn: str, frames: list[StackFrame]) -> str | None:
        match = cls._FRAME_RE.match(line)
        if match:
            frames.append(
                StackFrame(
                    function=current_fn,
                    file_path=match.group(1),
                    line=int(match.group(2)),
                )
            )
            return None
        return current_fn


class GoroutineLeakSentinel:
    """Audits Goroutine profiles and diagnoses concurrency defects."""

    @classmethod
    def _filter_leaks(cls, profiles: list[GoroutineProfile]) -> tuple[int, int, list[GoroutineProfile]]:
        """Partition profiles into system/user counts and identify leaked goroutines."""
        system_count = 0
        user_count = 0
        leaks: list[GoroutineProfile] = []
        for p in profiles:
            if p.is_system:
                system_count += 1
                continue
            user_count += 1
            if cls._is_leaked(p):
                leaks.append(p)
        return system_count, user_count, leaks

    @classmethod
    def audit(cls, profiles: list[GoroutineProfile]) -> ConcurrencyAuditReport:
        """Evaluate profiles and compute concurrency safety scorecard."""
        total = len(profiles)
        system_count, user_count, leaks = cls._filter_leaks(profiles)
        leak_count = len(leaks)

        severity = cls._determine_severity(leaks)
        score = max(0.0, 100.0 - (leak_count * 25.0))
        remediations = cls._generate_remediations(leaks)

        return ConcurrencyAuditReport(
            total_goroutines=total,
            system_goroutines=system_count,
            user_goroutines=user_count,
            leaked_goroutines=leak_count,
            severity=severity,
            score=round(score, 1),
            remediations=remediations,
            profiles=profiles,
        )

    @staticmethod
    def _is_leaked(profile: GoroutineProfile) -> bool:
        leaked_states = (
            GoroutineState.CHAN_SEND,
            GoroutineState.CHAN_RECEIVE,
            GoroutineState.SELECT,
        )
        return profile.state_category in leaked_states

    @staticmethod
    def _determine_severity(leaks: list[GoroutineProfile]) -> LeakSeverity:
        if not leaks:
            return LeakSeverity.CLEAN
        states = {p.state_category for p in leaks}
        if GoroutineState.CHAN_SEND in states:
            return LeakSeverity.CRITICAL
        if GoroutineState.SELECT in states:
            return LeakSeverity.HIGH
        return LeakSeverity.MEDIUM

    @classmethod
    def _generate_remediations(cls, leaks: list[GoroutineProfile]) -> list[str]:
        rems: list[str] = []
        for p in leaks:
            fn = top_function(p.frames) or f"goroutine_{p.goroutine_id}"
            template = REMEDIATION_TEMPLATES.get(p.state_category)
            if template:
                rems.append(template.format(fn=fn))
        return rems


def verify_test_run(
    trace_or_path: str | Path,
    fail_on_leak: bool = True,
    tolerated_delta: int = 0,
) -> ConcurrencyAuditReport:
    """Verify goroutine traces from a test run, failing if leaks outlive the workload.

    Designed for direct integration into test runners (pytest, unittest) to turn
    latent background concurrency leaks into deterministic test failures.
    """
    raw_trace = (
        trace_or_path.read_text(encoding="utf-8") if isinstance(trace_or_path, Path) else trace_or_path
    )
    profiles = GoroutineStackParser.parse_trace(raw_trace)
    report = GoroutineLeakSentinel.audit(profiles)

    if fail_on_leak and report.leaked_goroutines > tolerated_delta:
        rems = "\n".join(f"  - {r}" for r in report.remediations)
        raise AssertionError(
            f"Test runner concurrency leak failure: {report.leaked_goroutines} leaked goroutines "
            f"(severity {report.severity.value}, score {report.score}/100.0).\nRemediations:\n{rems}"
        )
    return report


def assert_no_goroutine_leaks(trace: str, tolerated_delta: int = 0) -> ConcurrencyAuditReport:
    """Assertion helper for test runners to fail ordinary test runs on goroutine leaks."""
    return verify_test_run(trace, fail_on_leak=True, tolerated_delta=tolerated_delta)


# Sample Go stack trace fixtures for demo and testing
SAMPLE_CLEAN_TRACE = """
goroutine 1 [running]:
main.main()
\t/app/main.go:15 +0x24
goroutine 2 [force gc (idle)]:
runtime.gopark(0x0, 0x0, 0x0, 0x0)
\t/usr/local/go/src/runtime/proc.go:400 +0x30
"""

SAMPLE_LEAKY_TRACE = """
goroutine 1 [running]:
main.main()
\t/app/main.go:20 +0x24
goroutine 19 [chan send]:
example.com/go-leak-sentinel.LeakyChannelWorker.func1()
\t/app/sentinel.go:88 +0x34
created by example.com/go-leak-sentinel.LeakyChannelWorker in goroutine 1
\t/app/sentinel.go:86 +0x80
goroutine 20 [select]:
example.com/go-leak-sentinel.LeakyContextWorker.func1()
\t/app/sentinel.go:108 +0x68
created by example.com/go-leak-sentinel.LeakyContextWorker in goroutine 1
\t/app/sentinel.go:106 +0x50
goroutine 2 [force gc (idle)]:
runtime.gopark(0x0, 0x0, 0x0, 0x0)
\t/usr/local/go/src/runtime/proc.go:400 +0x30
"""


def _print_report(report: ConcurrencyAuditReport) -> None:
    print("=" * 80)
    print("🐹 GO CONCURRENCY & GOROUTINE LEAK SENTINEL — AUDIT REPORT")
    print("=" * 80)
    print(f"Total Goroutines:    {report.total_goroutines}")
    print(f"System Daemons:      {report.system_goroutines}")
    print(f"User Goroutines:     {report.user_goroutines}")
    print(f"Leaked Goroutines:   {report.leaked_goroutines}")
    print(f"Severity Rating:     {report.severity.value}")
    print(f"Concurrency Score:   {report.score}/100.0")
    print("-" * 80)
    if report.remediations:
        print("🚨 IDENTIFIED CONCURRENCY LEAKS & REMEDIATIONS:")
        for r in report.remediations:
            print(f"  • {r}")
    else:
        print("✅ CLEAN EXECUTION: Zero leaked goroutines detected.")
    print("=" * 80)


def run_demo() -> int:
    """Execute demonstration comparing clean vs leaky Go goroutine profiles."""
    print("\n--- Scenario A: Clean Go Concurrency Execution ---")
    clean_profiles = GoroutineStackParser.parse_trace(SAMPLE_CLEAN_TRACE)
    clean_report = GoroutineLeakSentinel.audit(clean_profiles)
    _print_report(clean_report)

    print("\n--- Scenario B: Leaky Channel & Context Worker Execution ---")
    leaky_profiles = GoroutineStackParser.parse_trace(SAMPLE_LEAKY_TRACE)
    leaky_report = GoroutineLeakSentinel.audit(leaky_profiles)
    _print_report(leaky_report)

    return 0


def _emit_scan_report(report: ConcurrencyAuditReport, as_json: bool) -> None:
    """Print the audit report either as JSON or formatted text."""
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_report(report)


def _handle_scan_command(scan_path: Path, as_json: bool, fail_on_leak: bool) -> int:
    """Process and audit a raw stack dump file."""
    if not scan_path.is_file():
        print(f"Error: File not found: {scan_path}", file=sys.stderr)
        return 1
    content = scan_path.read_text(encoding="utf-8")
    profiles = GoroutineStackParser.parse_trace(content)
    report = GoroutineLeakSentinel.audit(profiles)
    _emit_scan_report(report, as_json)
    if fail_on_leak and report.leaked_goroutines > 0:
        print(
            f"TEST HARNESS FAILURE: {report.leaked_goroutines} leaked goroutines detected!", file=sys.stderr
        )
        return 1
    return 0 if report.leaked_goroutines == 0 else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for Go Goroutine Leak Sentinel."""
    parser = argparse.ArgumentParser(description="Go Concurrency & Goroutine Leak Sentinel")
    parser.add_argument("--demo", action="store_true", help="Run simulated leak detection demo")
    parser.add_argument("--scan", type=Path, help="Path to raw stack dump file to analyze")
    parser.add_argument("--json", action="store_true", help="Emit report in structured JSON format")
    parser.add_argument(
        "--fail-on-leak",
        action="store_true",
        help="Fail ordinary test run with exit code 1 when leaks are present",
    )

    args = parser.parse_args(argv)

    if args.demo:
        return run_demo()
    if args.scan:
        return _handle_scan_command(args.scan, args.json, args.fail_on_leak)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
