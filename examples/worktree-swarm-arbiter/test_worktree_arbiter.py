"""Unit tests for the Autonomous Git Worktree Fleet Allocator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from worktree_arbiter import (
    SwarmFleetStatus,
    WorktreeLease,
    allocate_worktree,
    audit_fleet_health,
    build_worktree_lease,
    generate_lease_id,
    is_index_locked,
    is_lease_expired,
    main,
    prune_stale_leases,
    refresh_lease_heartbeat,
    release_worktree,
    run_git_command,
    terminate_lease_process_group,
    to_markdown,
    to_sarif,
    wait_for_index_lock,
)


def test_generate_lease_id() -> None:
    """Verify deterministic lease ID generation."""
    lid1 = generate_lease_id("Subagent-Reviewer", 1700000000.0)
    lid2 = generate_lease_id("Worker_1", 1700000000.0)
    assert (lid1, lid2) == ("wt-subagent-reviewer-1700000000", "wt-worker-1-1700000000")


def test_is_index_locked(tmp_path: Path) -> None:
    """Verify index lock detection."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    lock_file = git_dir / "index.lock"

    assert (is_index_locked(tmp_path), False) == (False, False)
    lock_file.write_text("lock")
    assert (is_index_locked(tmp_path), True) == (True, True)


def test_wait_for_index_lock(tmp_path: Path) -> None:
    """Verify lock polling wait logic."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    assert (wait_for_index_lock(tmp_path, max_attempts=2), True) == (True, True)

    (git_dir / "index.lock").write_text("lock")
    assert (wait_for_index_lock(tmp_path, max_attempts=2, delay=0.01), False) == (False, False)


def test_build_worktree_lease(tmp_path: Path) -> None:
    """Verify WorktreeLease synthesis and TTL."""
    lease = build_worktree_lease("worker-a", tmp_path, ttl_seconds=300.0)
    assert (lease.agent_id, lease.expires_at > lease.allocated_at) == ("worker-a", True)


def test_is_lease_expired_and_refresh() -> None:
    """Verify expiration predicate and heartbeat extension."""
    now = 1000.0
    lease = WorktreeLease(
        lease_id="test-1",
        agent_id="agent-1",
        branch_name="agent/test-1",
        worktree_path="/tmp/wt",
        allocated_at=now - 500.0,
        expires_at=now - 100.0,
        last_heartbeat=now - 500.0,
    )
    assert (is_lease_expired(lease, now), True) == (True, True)

    refreshed = refresh_lease_heartbeat(lease, extension_seconds=600.0)
    assert (refreshed.expires_at > now, refreshed.last_heartbeat >= now) == (True, True)


def test_audit_fleet_health() -> None:
    """Verify fleet health classification into active and stale leases."""
    now = 1000.0
    l_active = WorktreeLease("l1", "a1", "b1", "/p1", now - 100, now + 100, now - 100)
    l_stale = WorktreeLease("l2", "a2", "b2", "/p2", now - 200, now - 50, now - 200)

    status = audit_fleet_health([l_active, l_stale], now)
    assert (len(status.active_leases), len(status.stale_leases)) == (1, 1)


def test_terminate_lease_process_group() -> None:
    """Verify process group termination safety."""
    lease_zero = WorktreeLease("l0", "a0", "b0", "/p0", 0, 0, 0, process_group_id=0)
    lease_invalid = WorktreeLease("l1", "a1", "b1", "/p1", 0, 0, 0, process_group_id=9999999)
    assert (terminate_lease_process_group(lease_zero), True) == (True, True)
    assert (terminate_lease_process_group(lease_invalid), False) == (False, False)


def test_allocate_and_release_worktree_mocked(tmp_path: Path, monkeypatch: Any) -> None:
    """Verify worktree allocation and release with mocked git execution."""
    def fake_git(cmd: list[str], cwd: Path) -> tuple[int, str]:
        return 0, "ok"

    monkeypatch.setattr("worktree_arbiter.run_git_command", fake_git)
    lease = build_worktree_lease("worker-mock", tmp_path)
    ok_alloc, msg_alloc = allocate_worktree(tmp_path, lease)
    assert (ok_alloc, msg_alloc) == (True, lease.lease_id)

    ok_rel, _ = release_worktree(tmp_path, lease)
    assert (ok_rel, True) == (True, True)


def test_prune_stale_leases_mocked(tmp_path: Path, monkeypatch: Any) -> None:
    """Verify pruning of expired leases."""
    def fake_git(cmd: list[str], cwd: Path) -> tuple[int, str]:
        return 0, "pruned"

    monkeypatch.setattr("worktree_arbiter.run_git_command", fake_git)
    now = 1000.0
    l_stale = WorktreeLease("s1", "a1", "b1", "/tmp/wt1", now - 200, now - 10, now - 200)
    l_active = WorktreeLease("a1", "a2", "b2", "/tmp/wt2", now - 50, now + 500, now - 50)

    retained, count = prune_stale_leases(tmp_path, [l_stale, l_active], now)
    assert (len(retained), count) == (1, 1)


def test_sarif_and_markdown_formatting() -> None:
    """Verify SARIF 2.1.0 and Markdown report format contracts."""
    stale = WorktreeLease("s1", "agent-x", "b1", "/tmp/stale", 0, 100, 0)
    status = SwarmFleetStatus(stale_leases=[stale], total_allocated=1)
    sarif = to_sarif(status)
    md = to_markdown(status)

    assert (sarif["version"], len(sarif["runs"][0]["results"])) == ("2.1.0", 1)
    assert ("# Worktree Swarm Arbiter Status Report" in md, "agent-x" in str(sarif)) == (True, True)


def test_run_git_command_real(tmp_path: Path) -> None:
    """Verify real git command execution."""
    code, output = run_git_command(["--version"], tmp_path)
    assert (code, "git version" in output) == (0, True)


def test_main_cli_status(capsys: Any) -> None:
    """Verify CLI entrypoint for status commands."""
    rc = main(["status", "--format", "json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert (rc, "active_leases" in payload) == (0, True)
