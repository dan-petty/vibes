"""Tests for Automated Seccomp BPF Profile Synthesizer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import seccomp_synthesizer as synth


def test_baseline_runtime_syscalls_completeness() -> None:
    """Essential execution and exit syscalls are present in baseline."""
    essentials = ("read", "write", "exit_group", "brk", "mmap", "futex")
    present = tuple(name in synth.BASELINE_RUNTIME_SYSCALLS for name in essentials)
    assert present == (True,) * len(essentials)


def test_ast_symbol_analysis_detects_expected_syscalls() -> None:
    """Python AST visitor extracts syscalls from imports and call expressions."""
    snippet = """
import socket
import subprocess
from os import mkdir

def run():
    with open("sample.txt", "w") as f:
        f.write("hello")
    mkdir("new_dir")
    subprocess.run(["echo", "1"])
    s = socket.socket()
    s.connect(("127.0.0.1", 80))
"""
    result = synth.analyze_source_code(snippet)
    total = result.total_syscalls()

    assert (
        "socket" in total,
        "connect" in total,
        "clone" in total,
        "mkdirat" in total,
        "openat" in total,
    ) == (True, True, True, True, True)


def test_ast_symbol_analysis_handles_syntax_error() -> None:
    """Malformed syntax returns empty analysis result without raising."""
    result = synth.analyze_source_code("def broken(:\n pass")
    assert (len(result.modules), len(result.symbols), len(result.syscalls)) == (0, 0, 0)


def test_analyze_path_file_and_directory(tmp_path: Path) -> None:
    """Scanning single file and directory tree discovers target symbols."""
    file_a = tmp_path / "module_a.py"
    file_b = tmp_path / "subdir" / "module_b.py"
    file_b.parent.mkdir(parents=True)

    file_a.write_text("import socket\n", encoding="utf-8")
    file_b.write_text("import shutil\n", encoding="utf-8")

    single_res = synth.analyze_path(file_a)
    tree_res = synth.analyze_path(tmp_path)

    assert (
        "socket" in single_res.total_syscalls(),
        "copy_file_range" in single_res.total_syscalls(),
        "socket" in tree_res.total_syscalls(),
        "copy_file_range" in tree_res.total_syscalls(),
    ) == (True, False, True, True)


def test_parse_strace_lines() -> None:
    """Strace lines are correctly extracted, skipping signals and exits."""
    lines = [
        'openat(AT_FDCWD, "/etc/ld.so.cache", O_RDONLY|O_CLOEXEC) = 3',
        '[pid 1234] read(3, "\\177ELF...", 832) = 832',
        'sys_clone(0, 0, 0, 0, 0) = 4567',
        '--- SIGCHLD {si_signo=SIGCHLD} ---',
        '+++ exited with 0 +++',
        '',
    ]
    parsed = synth.parse_trace_lines(lines)
    assert ("openat" in parsed, "read" in parsed, "clone" in parsed) == (True, True, True)


def test_parse_json_trace_lines() -> None:
    """JSON event trace records yield extracted syscalls."""
    lines = [
        json.dumps({"syscall": "connect", "fd": 3}),
        json.dumps({"name": "epoll_wait", "timeout": 100}),
        json.dumps({"call": "bind"}),
        json.dumps({"irrelevant": "payload"}),
        "invalid json line",
    ]
    parsed = synth.parse_trace_lines(lines)
    assert ("connect" in parsed, "epoll_wait" in parsed, "bind" in parsed) == (True, True, True)


def test_synthesize_profile_with_overrides() -> None:
    """Profile synthesis merges detected, extra allowances, and exclusions."""
    detected = ("socket", "connect")
    allow_extra = ("epoll_create1",)
    deny_extra = ("connect", "brk")

    cfg = synth.SynthesisConfig(default_action=synth.ACT_ERRNO)
    profile = synth.synthesize_profile(
        detected,
        config=cfg,
        allow_extra=allow_extra,
        deny_extra=deny_extra,
    )
    allowed = profile.allowed_syscalls()

    assert (
        profile.default_action,
        "socket" in allowed,
        "epoll_create1" in allowed,
        "connect" in allowed,
        "brk" in allowed,
    ) == (synth.ACT_ERRNO, True, True, False, False)


def test_synthesize_profile_invalid_action() -> None:
    """Invalid default action raises ValueError."""
    cfg = synth.SynthesisConfig(default_action="INVALID_ACTION")
    with pytest.raises(ValueError, match="Invalid default action"):
        synth.synthesize_profile((), config=cfg)


def test_to_oci_json_roundtrip() -> None:
    """Emitted OCI seccomp JSON roundtrips through load_profile_from_json."""
    cfg = synth.SynthesisConfig(default_action=synth.ACT_KILL)
    profile = synth.synthesize_profile(("socket",), config=cfg)
    rendered = profile.to_json()
    loaded = synth.load_profile_from_json(rendered)

    assert (
        loaded.default_action,
        loaded.architectures,
        "socket" in loaded.allowed_syscalls(),
    ) == (synth.ACT_KILL, synth.SUPPORTED_ARCHITECTURES, True)


def test_audit_profile_detects_risks_and_deficits() -> None:
    """Auditing flags high risk calls, missing baselines, and open defaults."""
    cfg = synth.SynthesisConfig(
        default_action=synth.ACT_ALLOW,
        include_baseline=False,
    )
    risky_profile = synth.synthesize_profile(("ptrace", "bpf"), config=cfg)
    findings = synth.audit_profile(risky_profile)

    has_high_risk = any("HIGH_RISK" in f and "ptrace" in f for f in findings)
    has_baseline_deficit = any("BASELINE_DEFICIT" in f for f in findings)
    has_insecure_default = any("INSECURE_DEFAULT" in f for f in findings)

    assert (has_high_risk, has_baseline_deficit, has_insecure_default) == (True, True, True)


def test_diff_profiles_reports_changes() -> None:
    """Profile diff reveals added, removed, and action changes."""
    cfg1 = synth.SynthesisConfig(default_action=synth.ACT_ERRNO)
    cfg2 = synth.SynthesisConfig(default_action=synth.ACT_KILL)

    prof1 = synth.synthesize_profile(("socket",), config=cfg1)
    prof2 = synth.synthesize_profile(("bind",), config=cfg2)

    diff = synth.diff_profiles(prof1, prof2)

    assert (
        diff["action_changed"],
        diff["base_action"],
        diff["cand_action"],
        "bind" in diff["added_syscalls"],
        "socket" in diff["removed_syscalls"],
    ) == (True, synth.ACT_ERRNO, synth.ACT_KILL, True, True)


def test_cli_execution_with_source(tmp_path: Path) -> None:
    """CLI successfully synthesizes and writes JSON profile for target source."""
    py_file = tmp_path / "agent_tool.py"
    py_file.write_text("import socket\ns = socket.socket()\n", encoding="utf-8")
    out_file = tmp_path / "seccomp.json"

    exit_code = synth.main([
        "--source", str(py_file),
        "--out", str(out_file),
        "--default-action", synth.ACT_ERRNO,
        "--audit",
    ])

    loaded = synth.load_profile_from_json(out_file.read_text(encoding="utf-8"))
    assert (exit_code, "socket" in loaded.allowed_syscalls()) == (0, True)


def test_cli_strict_mode_blocks_high_risk(capsys: pytest.CaptureFixture[str]) -> None:
    """Strict CLI mode fails when a high risk syscall is permitted."""
    exit_code = synth.main(["--allow", "ptrace", "--strict"])
    captured = capsys.readouterr()
    assert (exit_code, "Strict audit failed" in captured.err) == (1, True)


def test_cli_missing_arguments() -> None:
    """CLI invocation with no inputs exits with usage status 2."""
    assert synth.main([]) == 2


def test_cli_diff_option(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI diff option reads reference profile and prints comparison."""
    ref_file = tmp_path / "reference.json"
    ref_profile = synth.synthesize_profile(())
    ref_file.write_text(ref_profile.to_json(), encoding="utf-8")

    exit_code = synth.main([
        "--allow", "socket",
        "--diff", str(ref_file),
    ])
    captured = capsys.readouterr()
    assert (exit_code, "[DIFF] Added:" in captured.err) == (0, True)
