"""Comprehensive test suite for Agentic IDE Hook Sentinel."""

# sentinel: allow[ZeroTrustSanitization] — fixture payloads asserting the IDE hook blocks private-network egress

from __future__ import annotations

from pathlib import Path
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
    """Ensure runaway subprocesses are terminated via process group kill on timeout."""
    sentinel = IdeHookSentinel(tmp_path)
    result = sentinel.run_isolated_command(
        ["python3", "-c", "import time; time.sleep(10)"],
        timeout_seconds=0.3,
    )
    assert (result.exit_code, result.timed_out) == (-1, True)


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
        "line 42" in err_eval.prescriptive_guidance,
        "Type 'str' is not assignable" in err_eval.prescriptive_guidance,
    ) == (False, 1, 1, True, True)

