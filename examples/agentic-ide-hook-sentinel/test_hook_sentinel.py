"""Comprehensive test suite for Agentic IDE Hook Sentinel."""

# sentinel: allow[ZeroTrustSanitization] — fixture payloads asserting the IDE hook blocks private-network egress

from __future__ import annotations

import signal
import time
from pathlib import Path

import hook_sentinel
import pytest
from hook_sentinel import (
    HookDecision,
    IdeHookSentinel,
)


def test_evaluate_tool_call_safe(tmp_path: Path) -> None:
    """Ensure safe tool calls with allowed domains and documentation IPs are permitted."""
    sentinel = IdeHookSentinel(tmp_path)
    res = sentinel.evaluate_tool_call(
        tool_name="http_get",
        arguments={"url": "http://example.com/api", "host": "192.0.2.10"},
    )
    assert (res.decision, "verified safe" in res.reason) == (HookDecision.ALLOW, True)


def test_evaluate_tool_call_blocks_private_ip(tmp_path: Path) -> None:
    """Ensure tool calls targeting private RFC 1918 IPs are strictly denied."""
    sentinel = IdeHookSentinel(tmp_path)
    r1 = sentinel.evaluate_tool_call("deploy", {"endpoint": "http://10.0.1.5:8080"})
    r2 = sentinel.evaluate_tool_call("ping", {"ip": "192.168.1.100"})
    r3 = sentinel.evaluate_tool_call("query", {"target": "172.16.5.20"})

    assert (
        r1.decision,
        r2.decision,
        r3.decision,
        "10.0.1.5" in r1.reason,
        "192.168.1.100" in r2.reason,
        "172.16.5.20" in r3.reason,
    ) == (
        HookDecision.DENY,
        HookDecision.DENY,
        HookDecision.DENY,
        True,
        True,
        True,
    )


def test_evaluate_tool_call_blocks_protected_paths(tmp_path: Path) -> None:
    """Ensure tool calls attempting to read or write credentials/system paths are denied."""
    sentinel = IdeHookSentinel(tmp_path)
    r1 = sentinel.evaluate_tool_call("read_file", {"path": "/workspace/.env.local"})
    r2 = sentinel.evaluate_tool_call("copy_file", {"path": "~/.ssh/id_rsa"})
    r3 = sentinel.evaluate_tool_call("cat", {"path": "/etc/shadow"})

    assert (
        r1.decision,
        r2.decision,
        r3.decision,
        ".env.local" in r1.reason,
        "id_rsa" in r2.reason,
        "/etc/shadow" in r3.reason,
    ) == (
        HookDecision.DENY,
        HookDecision.DENY,
        HookDecision.DENY,
        True,
        True,
        True,
    )


def test_evaluate_file_write_safe(tmp_path: Path) -> None:
    """Ensure normal file writes within workspace root are allowed."""
    sentinel = IdeHookSentinel(tmp_path)
    target = tmp_path / "src" / "module.py"
    res = sentinel.evaluate_file_write(target, "print('hello world')\n")
    assert (res.decision, "verified safe" in res.reason) == (HookDecision.ALLOW, True)


def test_evaluate_file_write_blocks_oversized(tmp_path: Path) -> None:
    """Ensure file writes exceeding the 5MB size cap are blocked (CWE-400 mitigation)."""
    sentinel = IdeHookSentinel(tmp_path)
    target = tmp_path / "bundle.min.js"
    oversized = b"x" * (6 * 1024 * 1024)  # 6MB
    res = sentinel.evaluate_file_write(target, oversized)
    assert (res.decision, "exceeds maximum cap" in res.reason) == (HookDecision.DENY, True)


def test_evaluate_file_write_blocks_workspace_escape(tmp_path: Path) -> None:
    """Ensure path traversal attempts escaping the workspace boundary are denied."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    sentinel = IdeHookSentinel(workspace)

    escape_target = tmp_path / "escaped.txt"
    res = sentinel.evaluate_file_write(escape_target, "malicious payload")
    assert (res.decision, "escapes workspace root" in res.reason) == (HookDecision.DENY, True)


def test_evaluate_file_write_blocks_protected_path(tmp_path: Path) -> None:
    """Ensure attempts to overwrite .env or credentials inside workspace are blocked."""
    sentinel = IdeHookSentinel(tmp_path)
    target = tmp_path / ".env"
    res = sentinel.evaluate_file_write(target, "SECRET=compromised")
    assert (res.decision, "protected path" in res.reason) == (HookDecision.DENY, True)


def test_run_isolated_command_success(tmp_path: Path) -> None:
    """Ensure commands execute cleanly in their process group."""
    sentinel = IdeHookSentinel(tmp_path)
    result = sentinel.run_isolated_command(["python3", "-c", "print('isolated execution')"])
    assert (
        result.exit_code,
        result.stdout.strip(),
        result.timed_out,
    ) == (0, "isolated execution", False)


def test_run_isolated_command_timeout(tmp_path: Path) -> None:
    """Ensure runaway subprocesses are terminated via process group kill on timeout.

    The expected exit code changed from `-1` to `-SIGTERM`. It was a placeholder: the
    success branch reports `proc.returncode`, where POSIX spells a signal death as `-N`, so
    `-1` claimed the child died of SIGHUP — a signal nothing here sends. This assertion had
    been pinning that.
    """
    sentinel = IdeHookSentinel(tmp_path)
    result = sentinel.run_isolated_command(
        ["python3", "-c", "import time; time.sleep(10)"],
        timeout_seconds=0.3,
    )
    assert (result.exit_code, result.timed_out) == (-signal.SIGTERM, True)


def test_evaluate_lsp_diagnostics() -> None:
    """Verify evaluation of compiler diagnostics into actionable self-healing guidance."""
    # Clean diagnostics
    clean_eval = IdeHookSentinel.evaluate_lsp_diagnostics("main.py", [])
    assert (clean_eval.is_clean, clean_eval.error_count, clean_eval.warning_count) == (True, 0, 0)

    # Diagnostic with compiler error
    err_diag = [
        {
            "range": {"start": {"line": 42, "character": 5}},
            "severity": 1,
            "message": "Type 'str' is not assignable to type 'int'",
        },
        {
            "range": {"start": {"line": 50, "character": 1}},
            "severity": 2,
            "message": "Unused variable 'x'",
        },
    ]
    err_eval = IdeHookSentinel.evaluate_lsp_diagnostics("main.py", err_diag)
    assert (
        err_eval.is_clean,
        err_eval.error_count,
        err_eval.warning_count,
        # 43, not 42: LSP `Position.line` is zero-based and editors display one-based
        # numbers, so the raw value pointed the self-healing loop one line above the defect.
        "line 43" in err_eval.prescriptive_guidance,
        "Type 'str' is not assignable" in err_eval.prescriptive_guidance,
    ) == (False, 1, 1, True, True)



# --- Notations a resolver accepts and a strict parser does not -----------------------------


@pytest.mark.parametrize(
    ("literal", "private"),
    [
        ("172.16.0.2", True),
        ("172.020.0.2", True),
        ("0xac.0x10.0.2", True),
        ("10.0.0.1", True),
        ("192.0.2.5", False),
        ("8.8.8.8", False),
        ("not.an.ip.at.all", False),
    ],
)
def test_an_octal_or_hex_octet_is_still_a_private_address(literal: str, private: bool) -> None:
    """The guard parsed strictly and treated a rejection as "not an address", failing open.

    `ipaddress.ip_address` refuses any octet with a leading zero, because `0177` meant octal
    historically and decimal now. `inet_aton` and every libc-backed client still accept it —
    `curl` given the octal form connects to the same private host the decimal form names,
    so the one notation an attacker would choose was the one that sailed through. The
    parametrized cases below carry the literals; this sentence deliberately does not,
    because `supply_chain_audit.py` reads a prose `http://` URL as a real plaintext
    endpoint and it is right to — an illustrative URL and a configured one look identical.
    """
    assert hook_sentinel._check_private_ip(literal) is private


def test_a_child_that_ignores_sigterm_still_returns_within_the_grace_period() -> None:
    """SIGTERM is a request; `communicate()` with no timeout waits for a reply that never comes.

    Observed before this: a 0.5-second contract ran past 25 seconds and would have blocked
    for the child's full 120. SIGKILL cannot be caught, blocked or ignored.
    """
    sentinel = hook_sentinel.IdeHookSentinel(workspace_root=Path("."))
    started = time.monotonic()
    result = sentinel.run_isolated_command(
        ["python3", "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"],
        timeout_seconds=0.5,
    )
    elapsed = time.monotonic() - started
    assert (result.timed_out, elapsed < 30.0) == (True, True)


# --- LSP conformance ---------------------------------------------------------------------


def test_a_diagnostic_with_no_severity_is_treated_as_an_error() -> None:
    """LSP 3.17: an omitted severity is the client's to interpret, and Error is recommended.

    The membership test matched neither the error list nor the warning list, so such a
    diagnostic was dropped from both counts and a file carrying a hard compile error came
    back `is_clean=True`. The gate inverted: a server declining to grade its own finding
    read as a passing file, and the documented consumer therefore never called
    `send_feedback`.
    """
    evaluation = hook_sentinel.IdeHookSentinel.evaluate_lsp_diagnostics(
        "m.py", [{"message": "undefined name", "range": {"start": {"line": 0}}}]
    )
    assert (evaluation.is_clean, evaluation.error_count) == (False, 1)


def test_a_graded_warning_still_only_counts_as_a_warning() -> None:
    """Defaulting to Error must not promote everything a server did grade."""
    evaluation = hook_sentinel.IdeHookSentinel.evaluate_lsp_diagnostics(
        "m.py", [{"severity": 2, "message": "w", "range": {"start": {"line": 0}}}]
    )
    assert (evaluation.is_clean, evaluation.warning_count) == (True, 1)


def test_a_zero_based_lsp_line_is_reported_the_way_an_editor_shows_it() -> None:
    """`Position.line` is zero-based; every editor displays one-based line numbers.

    Passing the raw value through pointed the self-healing loop one line above the defect.
    Verified against a real `ruff server`, which reports line 42 for a defect on file line 43.
    """
    evaluation = hook_sentinel.IdeHookSentinel.evaluate_lsp_diagnostics(
        "m.py", [{"severity": 1, "message": "boom", "range": {"start": {"line": 42}}}]
    )
    assert "line 43" in evaluation.prescriptive_guidance


def test_a_timed_out_command_reports_the_signal_that_reaped_it() -> None:
    """The success branch reports `proc.returncode`, where `-N` spells a signal death.

    A hardcoded `-1` therefore claimed the child died of SIGHUP — a signal nothing here
    sends — so a reader could not tell "timed out and was terminated" from "was hung up on".
    """
    sentinel = hook_sentinel.IdeHookSentinel(workspace_root=Path("."))
    result = sentinel.run_isolated_command(["python3", "-c", "import time; time.sleep(30)"],
                                           timeout_seconds=0.4)
    assert (result.timed_out, result.exit_code) == (True, -signal.SIGTERM)
