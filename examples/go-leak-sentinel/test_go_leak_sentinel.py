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
import sys
from pathlib import Path

import pytest

# Add directory to sys.path for direct imports
_curr_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_curr_dir))


from go_leak_sentinel import (
    SAMPLE_CLEAN_TRACE,
    SAMPLE_LEAKY_TRACE,
    GoroutineLeakSentinel,
    GoroutineStackParser,
    GoroutineState,
    LeakSeverity,
    assert_no_goroutine_leaks,
    main,
    run_demo,
    verify_test_run,
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


def test_a_system_traceback_header_is_parsed() -> None:
    """`GOTRACEBACK=system` adds runtime fields between the id and the state.

    Requiring the bracket to follow the id with only whitespace between rejected every
    header those levels emit, so a dump full of blocked goroutines parsed to nothing and the
    sentinel reported CLEAN 100.0/100 — a leak detector that could not read the dump.
    """
    header = "goroutine 1 gp=0xc0000061c0 m=0 mp=0x5f8a40 [chan send]:"
    match = GoroutineStackParser._HEADER_RE.match(header)
    assert match is not None
    assert (match.group(1), match.group(2)) == ("1", "chan send")


@pytest.mark.parametrize(
    ("frame", "name"),
    [
        ("testing.(*T).Run(0xc0000b8340, {0x545b69?, 0x0?}, 0x54f830)", "testing.(*T).Run"),
        ("main.worker(0xc000112000)", "main.worker"),
        ("runtime.gopark(0x0?, 0x0?)", "runtime.gopark"),
    ],
)
def test_a_method_frame_keeps_its_receiver(frame: str, name: str) -> None:
    """Go spells a pointer receiver `(*T)` inside the name, so the first `(` is not the args.

    Splitting there truncated `testing.(*T).Run` to `testing.`, which meant the exclusion
    list for runtime frames could never match and the runtime's own goroutines were counted
    as leaks.
    """
    assert GoroutineStackParser._function_name(frame) == name


# A user goroutine blocked on a channel receive, in a dump that shows runtime frames.
BLOCKED_RECEIVE_DUMP = """
goroutine 18 [chan receive]:
runtime.gopark(0x0?, 0x0?, 0x0?, 0x0?, 0x0?)
\t/usr/local/go/src/runtime/proc.go:424 +0xce
runtime.chanrecv(0xc000100060, 0x0, 0x1)
\t/usr/local/go/src/runtime/chan.go:639 +0x3bc
runtime.chanrecv1(0x0?, 0x0?)
\t/usr/local/go/src/runtime/chan.go:489 +0x12
main.worker(0xc000100060)
\t/app/main.go:7 +0x25
created by main.main in goroutine 1
\t/app/main.go:11 +0x66
"""

BLOCKED_SELECT_DUMP = """
goroutine 21 [select]:
runtime.gopark(0xc00004a790?, 0x2?, 0x0?, 0x0?, 0xc00004a77c?)
\t/usr/local/go/src/runtime/proc.go:424 +0xce
runtime.selectgo(0xc00004a790, 0xc00004a778, 0x0?, 0x0, 0x0?, 0x1)
\t/usr/local/go/src/runtime/select.go:335 +0x7a5
main.poller(0xc000100060, 0xc0001000c0)
\t/app/main.go:15 +0x7c
created by main.main in goroutine 1
\t/app/main.go:12 +0x9e
"""

# Go before 1.14 ran timers in a runtime goroutine with the entry runtime.timerproc.
RUNTIME_TIMER_DUMP = """
goroutine 4 [select]:
runtime.gopark(0x4c1f28, 0x0, 0x4b0a2b, 0x6, 0x18, 0x1)
\t/usr/local/go/src/runtime/proc.go:303 +0xeb
runtime.selectgo(0xc00003e758, 0xc00003e770, 0x2, 0x1, 0x0)
\t/usr/local/go/src/runtime/select.go:313 +0xc8f
runtime.timerproc(0x5781e0)
\t/usr/local/go/src/runtime/time.go:255 +0x2cd
created by runtime.(*timersBucket).addtimerLocked
\t/usr/local/go/src/runtime/time.go:160 +0x107
"""

# The main goroutine of `go test` waits in testing.(*T).Run for each test to end.
TEST_RUNNER_DUMP = """
goroutine 1 [chan receive]:
testing.(*T).Run(0xc0000b8340, {0x545b69?, 0x0?}, 0x54f830)
\t/usr/local/go/src/testing/testing.go:1751 +0x3ab
testing.runTests.func1(0xc0000b8340)
\t/usr/local/go/src/testing/testing.go:2168 +0x37
testing.tRunner(0xc0000b8340, 0xc0000b5c70)
\t/usr/local/go/src/testing/testing.go:1690 +0xf4
testing.runTests(0xc000010078, {0x6342a0, 0x1, 0x1}, {0x0?, 0xc0000b5d50?, 0x63ce80?})
\t/usr/local/go/src/testing/testing.go:2166 +0x43d
testing.(*M).Run(0xc0000c40a0)
\t/usr/local/go/src/testing/testing.go:2034 +0x64a
main.main()
\t_testmain.go:45 +0x9b
"""

# The same goroutine with GOTRACEBACK=system: runtime frames, and fp/sp/pc on each frame line.
TEST_RUNNER_SYSTEM_DUMP = """
goroutine 1 gp=0xc000002380 m=nil [chan receive]:
runtime.gopark(0x0?, 0x0?, 0x0?, 0x0?, 0x0?)
\t/usr/local/go/src/runtime/proc.go:424 +0xce fp=0xc0000b5a28 sp=0xc0000b5a08 pc=0x46f8ce
runtime.chanrecv(0xc0000a4070, 0x0, 0x1)
\t/usr/local/go/src/runtime/chan.go:639 +0x3bc fp=0xc0000b5aa0 sp=0xc0000b5a28 pc=0x40885c
runtime.chanrecv1(0x0?, 0x0?)
\t/usr/local/go/src/runtime/chan.go:489 +0x12 fp=0xc0000b5ac8 sp=0xc0000b5aa0 pc=0x408472
testing.(*T).Run(0xc0000b8340, {0x545b69?, 0x0?}, 0x54f830)
\t/usr/local/go/src/testing/testing.go:1751 +0x3ab fp=0xc0000b5bf0 sp=0xc0000b5ac8 pc=0x4c6f2b
testing.(*M).Run(0xc0000c40a0)
\t/usr/local/go/src/testing/testing.go:2034 +0x64a fp=0xc0000b5ed0 sp=0xc0000b5bf0 pc=0x4c8eca
main.main()
\t_testmain.go:45 +0x9b fp=0xc0000b5f50 sp=0xc0000b5ed0 pc=0x4d0a1b
runtime.main()
\t/usr/local/go/src/runtime/proc.go:272 +0x28b fp=0xc0000b5fe0 sp=0xc0000b5f50 pc=0x43ad8b
runtime.goexit({})
\t/usr/local/go/src/runtime/asm_amd64.s:1700 +0x1 fp=0xc0000b5fe8 sp=0xc0000b5fe0 pc=0x472c01
"""


def test_a_goroutine_blocked_on_a_channel_receive_is_a_leak() -> None:
    """Every blocked goroutine parks in `runtime.gopark`, so that frame does not mark a daemon.

    The parser marked any goroutine with a `runtime.gopark` frame as a system daemon. A
    dump that shows runtime frames (GOTRACEBACK=system) gives every blocked goroutine that
    frame, so the leaks the sentinel targets were excluded and the audit said CLEAN.
    """
    profiles = GoroutineStackParser.parse_trace(BLOCKED_RECEIVE_DUMP)
    report = GoroutineLeakSentinel.audit(profiles)
    actual = ([p.is_system for p in profiles], report.leaked_goroutines, report.severity, report.remediations)
    expected = (
        [False],
        1,
        LeakSeverity.MEDIUM,
        ["[main.worker] Channel receive blocked. Ensure sender closes channel on termination."],
    )
    assert actual == expected


@pytest.mark.parametrize(
    ("dump", "expected"),
    [
        pytest.param(BLOCKED_SELECT_DUMP, (False, 1, LeakSeverity.HIGH), id="user-select"),
        pytest.param(RUNTIME_TIMER_DUMP, (True, 0, LeakSeverity.CLEAN), id="runtime-entry"),
        pytest.param(TEST_RUNNER_DUMP, (True, 0, LeakSeverity.CLEAN), id="test-runner"),
        pytest.param(TEST_RUNNER_SYSTEM_DUMP, (True, 0, LeakSeverity.CLEAN), id="test-runner-system"),
    ],
)
def test_the_entry_function_decides_a_system_goroutine(dump: str, expected: tuple[bool, int, LeakSeverity]) -> None:
    """A goroutine is a runtime goroutine when its entry function is in package runtime.

    goleak's default filters also skip the testing runner that waits for a test to end.
    """
    profiles = GoroutineStackParser.parse_trace(dump)
    report = GoroutineLeakSentinel.audit(profiles)
    assert (profiles[0].is_system, report.leaked_goroutines, report.severity) == expected


# runtime.Stack(buf, true) with GOTRACEBACK=system, from a program that calls signal.Notify
# and starts `go worker(ch)` on a channel that no goroutine writes to.
SYSTEM_TRACEBACK_DUMP = """
goroutine 1 gp=0xc000002380 m=0 mp=0x5a4f40 [running]:
runtime.Stack({0xc000180000, 0x100000, 0x100000}, 0x1)
\t/usr/local/go/src/runtime/mprof.go:1607 +0x8f fp=0xc000059ec8 sp=0xc000059e78 pc=0x46742f
main.main()
\t/app/main.go:17 +0xb5 fp=0xc000059f50 sp=0xc000059ec8 pc=0x49ac35
runtime.main()
\t/usr/local/go/src/runtime/proc.go:272 +0x28b fp=0xc000059fe0 sp=0xc000059f50 pc=0x43ad8b
runtime.goexit({})
\t/usr/local/go/src/runtime/asm_amd64.s:1700 +0x1 fp=0xc000059fe8 sp=0xc000059fe0 pc=0x472c01

goroutine 2 gp=0xc000002e00 m=nil [force gc (idle)]:
runtime.gopark(0x0?, 0x0?, 0x0?, 0x0?, 0x0?)
\t/usr/local/go/src/runtime/proc.go:424 +0xce fp=0xc00004afa8 sp=0xc00004af88 pc=0x46d0ae
runtime.goparkunlock(...)
\t/usr/local/go/src/runtime/proc.go:430
runtime.forcegchelper()
\t/usr/local/go/src/runtime/proc.go:337 +0xb3 fp=0xc00004afe0 sp=0xc00004afa8 pc=0x43b0d3
runtime.goexit({})
\t/usr/local/go/src/runtime/asm_amd64.s:1700 +0x1 fp=0xc00004afe8 sp=0xc00004afe0 pc=0x472c01
created by runtime.init.7 in goroutine 1
\t/usr/local/go/src/runtime/proc.go:325 +0x1a

goroutine 3 gp=0xc000003340 m=nil [GC sweep wait]:
runtime.gopark(0x1?, 0x0?, 0x0?, 0x0?, 0x0?)
\t/usr/local/go/src/runtime/proc.go:424 +0xce fp=0xc00004b780 sp=0xc00004b760 pc=0x46d0ae
runtime.goparkunlock(...)
\t/usr/local/go/src/runtime/proc.go:430
runtime.bgsweep(0xc000076000)
\t/usr/local/go/src/runtime/mgcsweep.go:317 +0xdf fp=0xc00004b7c8 sp=0xc00004b780 pc=0x425b5f
runtime.gcenable.gowrap1()
\t/usr/local/go/src/runtime/mgc.go:204 +0x25 fp=0xc00004b7e0 sp=0xc00004b7c8 pc=0x41a3c5
runtime.goexit({})
\t/usr/local/go/src/runtime/asm_amd64.s:1700 +0x1 fp=0xc00004b7e8 sp=0xc00004b7e0 pc=0x472c01
created by runtime.gcenable in goroutine 1
\t/usr/local/go/src/runtime/mgc.go:204 +0x66

goroutine 5 gp=0xc000003c00 m=nil [finalizer wait]:
runtime.gopark(0x0?, 0x0?, 0x0?, 0x0?, 0x0?)
\t/usr/local/go/src/runtime/proc.go:424 +0xce fp=0xc00004a620 sp=0xc00004a600 pc=0x46d0ae
runtime.runfinq()
\t/usr/local/go/src/runtime/mfinal.go:193 +0x107 fp=0xc00004a7e0 sp=0xc00004a620 pc=0x418ec7
runtime.goexit({})
\t/usr/local/go/src/runtime/asm_amd64.s:1700 +0x1 fp=0xc00004a7e8 sp=0xc00004a7e0 pc=0x472c01
created by runtime.createfing in goroutine 1
\t/usr/local/go/src/runtime/mfinal.go:163 +0x3d

goroutine 6 gp=0xc000003dc0 m=nil [select, locked to thread]:
runtime.gopark(0xc00004b7a8?, 0x2?, 0x0?, 0x0?, 0xc00004b794?)
\t/usr/local/go/src/runtime/proc.go:424 +0xce fp=0xc00004b638 sp=0xc00004b618 pc=0x46d0ae
runtime.selectgo(0xc00004b7a8, 0xc00004b790, 0x0?, 0x0, 0x0?, 0x1)
\t/usr/local/go/src/runtime/select.go:335 +0x7a5 fp=0xc00004b760 sp=0xc00004b638 pc=0x449a65
runtime.ensureSigM.func1()
\t/usr/local/go/src/runtime/signal_unix.go:1060 +0x192 fp=0xc00004b7e0 sp=0xc00004b760 pc=0x463df2
runtime.goexit({})
\t/usr/local/go/src/runtime/asm_amd64.s:1700 +0x1 fp=0xc00004b7e8 sp=0xc00004b7e0 pc=0x472c01
created by runtime.ensureSigM in goroutine 1
\t/usr/local/go/src/runtime/signal_unix.go:1043 +0xc8

goroutine 7 gp=0xc000102000 m=nil [syscall]:
runtime.notetsleepg(0x5a5a20, 0xffffffffffffffff)
\t/usr/local/go/src/runtime/lock_futex.go:246 +0x29 fp=0xc00004bfa0 sp=0xc00004bf78 pc=0x40c249
os/signal.signal_recv()
\t/usr/local/go/src/runtime/sigqueue.go:152 +0x29 fp=0xc00004bfc0 sp=0xc00004bfa0 pc=0x46ee49
os/signal.loop()
\t/usr/local/go/src/os/signal/signal_unix.go:23 +0x13 fp=0xc00004bfe0 sp=0xc00004bfc0 pc=0x49a313
runtime.goexit({})
\t/usr/local/go/src/runtime/asm_amd64.s:1700 +0x1 fp=0xc00004bfe8 sp=0xc00004bfe0 pc=0x472c01
created by os/signal.Notify.func1.1 in goroutine 1
\t/usr/local/go/src/os/signal/signal.go:151 +0x1f

goroutine 18 gp=0xc000102380 m=nil [chan receive]:
runtime.gopark(0x0?, 0x0?, 0x0?, 0x0?, 0x0?)
\t/usr/local/go/src/runtime/proc.go:424 +0xce fp=0xc00004a700 sp=0xc00004a6e0 pc=0x46d0ae
runtime.chanrecv(0xc000100060, 0x0, 0x1)
\t/usr/local/go/src/runtime/chan.go:639 +0x3bc fp=0xc00004a778 sp=0xc00004a700 pc=0x40885c
runtime.chanrecv1(0x0?, 0x0?)
\t/usr/local/go/src/runtime/chan.go:489 +0x12 fp=0xc00004a7a0 sp=0xc00004a778 pc=0x408472
main.worker(0xc000100060)
\t/app/main.go:7 +0x25 fp=0xc00004a7c8 sp=0xc00004a7a0 pc=0x49ab25
main.main.gowrap1()
\t/app/main.go:11 +0x25 fp=0xc00004a7e0 sp=0xc00004a7c8 pc=0x49ab85
runtime.goexit({})
\t/usr/local/go/src/runtime/asm_amd64.s:1700 +0x1 fp=0xc00004a7e8 sp=0xc00004a7e0 pc=0x472c01
created by main.main in goroutine 1
\t/app/main.go:11 +0x66
"""


def test_a_system_traceback_dump_counts_only_user_leaks() -> None:
    """GOTRACEBACK=system adds fp/sp/pc to each frame line and shows the runtime goroutines.

    The frame pattern rejected those lines, so the parser lost the frames: the runtime's
    signal goroutine (blocked in `select`) counted as a leak, and each remediation named
    `runtime.goexit`, the only frame left, instead of the blocked function.
    """
    profiles = GoroutineStackParser.parse_trace(SYSTEM_TRACEBACK_DUMP)
    report = GoroutineLeakSentinel.audit(profiles)
    actual = (
        {p.goroutine_id: p.is_system for p in profiles},
        [f.function for f in profiles[-1].frames],
        (report.system_goroutines, report.user_goroutines, report.leaked_goroutines),
        report.remediations,
    )
    expected = (
        {1: False, 2: True, 3: True, 5: True, 6: True, 7: True, 18: False},
        [
            "runtime.gopark",
            "runtime.chanrecv",
            "runtime.chanrecv1",
            "main.worker",
            "main.main.gowrap1",
            "runtime.goexit",
        ],
        (5, 2, 1),
        ["[main.worker] Channel receive blocked. Ensure sender closes channel on termination."],
    )
    assert actual == expected


def test_verify_test_run_clean_passes() -> None:
    """Verify test harness integration passes clean runs without raising."""
    report = verify_test_run(SAMPLE_CLEAN_TRACE, fail_on_leak=True)
    actual = (report.leaked_goroutines, report.severity, report.score)
    expected = (0, LeakSeverity.CLEAN, 100.0)
    assert actual == expected


def test_verify_test_run_leaky_raises_assertion_error() -> None:
    """Verify test harness integration fails ordinary test run by raising AssertionError."""
    with pytest.raises(AssertionError, match="Test runner concurrency leak failure"):
        verify_test_run(SAMPLE_LEAKY_TRACE, fail_on_leak=True)


def test_verify_test_run_fail_on_leak_disabled() -> None:
    """Verify fail_on_leak=False returns report without raising."""
    report = verify_test_run(SAMPLE_LEAKY_TRACE, fail_on_leak=False)
    actual = (report.leaked_goroutines, report.severity)
    expected = (2, LeakSeverity.CRITICAL)
    assert actual == expected


def test_assert_no_goroutine_leaks_helper() -> None:
    """Verify assertion helper passes on clean trace."""
    report = assert_no_goroutine_leaks(SAMPLE_CLEAN_TRACE)
    assert (report.leaked_goroutines, report.score) == (0, 100.0)


def test_cli_fail_on_leak_flag(tmp_path: Path) -> None:
    """Verify --fail-on-leak flag exits with returncode 1 on leaks."""
    dump_file = tmp_path / "leaky.dump"
    dump_file.write_text(SAMPLE_LEAKY_TRACE, encoding="utf-8")
    rc = main(["--scan", str(dump_file), "--fail-on-leak"])
    assert rc == 1
