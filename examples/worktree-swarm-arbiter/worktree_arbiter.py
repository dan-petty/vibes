#!/usr/bin/env python3
"""Autonomous Git Worktree Fleet Allocator & Swarm Arbiter.

Coordinates collision-free git worktree checkouts, lease heartbeats,
index-lock contention mitigation, and process group containment across
concurrent AI coding subagents.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

SCHEMA_SARIF_VERSION: Final[str] = "2.1.0"
SCHEMA_SARIF_URI: Final[str] = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
)

DEFAULT_LEASE_TTL_SECONDS: Final[float] = 3600.0  # 1 hour
MAX_INDEX_LOCK_RETRIES: Final[int] = 5

RULE_CODES: Final[dict[str, str]] = {
    "LOCK_CONTENTION": "WT001",
    "LEASE_EXPIRED": "WT002",
    "ORPHANED_WORKTREE": "WT003",
    "PROCESS_LEAK": "WT004",
}


@dataclass(frozen=True)
class WorktreeLease:
    """Record of an active worktree lease assigned to an agent."""

    lease_id: str
    agent_id: str
    branch_name: str
    worktree_path: str
    allocated_at: float
    expires_at: float
    last_heartbeat: float
    process_group_id: int = 0


@dataclass
class SwarmFleetStatus:
    """Status report of the worktree fleet."""

    active_leases: list[WorktreeLease] = field(default_factory=list)
    stale_leases: list[WorktreeLease] = field(default_factory=list)
    lock_contention_events: int = 0
    total_allocated: int = 0
    pruned_count: int = 0


def generate_lease_id(agent_id: str, timestamp: float | None = None) -> str:
    """Generate a deterministic worktree lease identifier."""
    ts = int(timestamp or time.time())
    sanitized = "".join(ch if ch.isalnum() else "-" for ch in agent_id.lower())
    return f"wt-{sanitized}-{ts}"


def is_index_locked(repo_root: Path) -> bool:
    """Check if git index lock file exists."""
    lock_path = repo_root / ".git" / "index.lock"
    return lock_path.is_file()


def wait_for_index_lock(repo_root: Path, max_attempts: int = 5, delay: float = 0.05) -> bool:
    """Wait for git index lock release with bounded backoff."""
    for _ in range(max_attempts):
        if not is_index_locked(repo_root):
            return True
        time.sleep(delay)
    return False


def run_git_command(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """Execute a git command within bounded timeout."""
    try:
        proc = subprocess.run(
            ["git", *cmd],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
        return proc.returncode, proc.stdout.strip() or proc.stderr.strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, f"GIT_EXEC_ERROR: {exc}"


def build_worktree_lease(
    agent_id: str,
    base_dir: Path,
    ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
) -> WorktreeLease:
    """Synthesize a new WorktreeLease value object."""
    now = time.time()
    lease_id = generate_lease_id(agent_id, now)
    branch = f"agent/{lease_id}"
    wt_path = base_dir / lease_id
    return WorktreeLease(
        lease_id=lease_id,
        agent_id=agent_id,
        branch_name=branch,
        worktree_path=str(wt_path.resolve()),
        allocated_at=now,
        expires_at=now + ttl_seconds,
        last_heartbeat=now,
        process_group_id=0,
    )


def allocate_worktree(
    repo_root: Path,
    lease: WorktreeLease,
) -> tuple[bool, str]:
    """Allocate an isolated git worktree for an agent."""
    if not wait_for_index_lock(repo_root):
        return False, "INDEX_LOCK_TIMEOUT"

    wt_path = Path(lease.worktree_path)
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["worktree", "add", "-b", lease.branch_name, str(wt_path), "HEAD"]
    code, output = run_git_command(cmd, repo_root)
    if code != 0:
        return False, f"WORKTREE_ADD_FAILED: {output}"
    return True, lease.lease_id


def release_worktree(
    repo_root: Path,
    lease: WorktreeLease,
) -> tuple[bool, str]:
    """Safely release and remove a worktree."""
    terminate_lease_process_group(lease)
    wt_path = Path(lease.worktree_path)
    cmd = ["worktree", "remove", "--force", str(wt_path)]
    code, output = run_git_command(cmd, repo_root)
    run_git_command(["worktree", "prune"], repo_root)

    branch_cmd = ["branch", "-D", lease.branch_name]
    run_git_command(branch_cmd, repo_root)
    return (code == 0, output)


def terminate_lease_process_group(lease: WorktreeLease) -> bool:
    """Terminate the POSIX process group associated with a lease."""
    if lease.process_group_id <= 0:
        return True
    try:
        os.killpg(lease.process_group_id, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def is_lease_expired(lease: WorktreeLease, now: float | None = None) -> bool:
    """Predicate evaluating whether a lease has passed its TTL."""
    current_time = now or time.time()
    return current_time >= lease.expires_at


def refresh_lease_heartbeat(
    lease: WorktreeLease,
    extension_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
) -> WorktreeLease:
    """Extend lease expiration with an updated heartbeat timestamp."""
    now = time.time()
    return WorktreeLease(
        lease_id=lease.lease_id,
        agent_id=lease.agent_id,
        branch_name=lease.branch_name,
        worktree_path=lease.worktree_path,
        allocated_at=lease.allocated_at,
        expires_at=now + extension_seconds,
        last_heartbeat=now,
        process_group_id=lease.process_group_id,
    )


def audit_fleet_health(
    leases: list[WorktreeLease],
    now: float | None = None,
) -> SwarmFleetStatus:
    """Audit active leases and partition active vs stale checkouts."""
    current_time = now or time.time()
    status = SwarmFleetStatus(total_allocated=len(leases))
    for lease in leases:
        if is_lease_expired(lease, current_time):
            status.stale_leases.append(lease)
        else:
            status.active_leases.append(lease)
    return status


def prune_stale_leases(
    repo_root: Path,
    leases: list[WorktreeLease],
    now: float | None = None,
) -> tuple[list[WorktreeLease], int]:
    """Prune all expired worktrees and return retained active leases."""
    current_time = now or time.time()
    retained: list[WorktreeLease] = []
    pruned_count = 0
    for lease in leases:
        if is_lease_expired(lease, current_time):
            ok, _ = release_worktree(repo_root, lease)
            if ok:
                pruned_count += 1
        else:
            retained.append(lease)
    return retained, pruned_count


def to_sarif(status: SwarmFleetStatus) -> dict[str, Any]:
    """Export fleet status to OASIS SARIF 2.1.0 telemetry."""
    rules = [
        {
            "id": code,
            "name": name,
            "shortDescription": {"text": f"Worktree fleet invariant: {name}"},
        }
        for name, code in RULE_CODES.items()
    ]
    results = [
        {
            "ruleId": RULE_CODES["LEASE_EXPIRED"],
            "level": "warning",
            "message": {
                "text": (
                    f"Stale worktree lease '{lease.lease_id}' for agent '{lease.agent_id}' "
                    f"expired at {lease.expires_at}."
                )
            },
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": lease.worktree_path},
                        "region": {"startLine": 1, "startColumn": 1},
                    }
                }
            ],
        }
        for lease in status.stale_leases
    ]
    return {
        "$schema": SCHEMA_SARIF_URI,
        "version": SCHEMA_SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "worktree-swarm-arbiter",
                        "version": "1.0.0",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def to_markdown(status: SwarmFleetStatus) -> str:
    """Format fleet status as a Markdown summary."""
    lines = [
        "# Worktree Swarm Arbiter Status Report",
        "",
        f"- **Total Allocated**: {status.total_allocated}",
        f"- **Active Leases**: {len(status.active_leases)}",
        f"- **Stale Leases**: {len(status.stale_leases)}",
        f"- **Lock Contention Events**: {status.lock_contention_events}",
        "",
        "## Active Leases",
        "",
        "| Lease ID | Agent ID | Branch | Path | Expires In (s) |",
        "|---|---|---|---|---|",
    ]
    now = time.time()
    for lease in status.active_leases:
        rem = max(0.0, round(lease.expires_at - now, 1))
        lines.append(f"| {lease.lease_id} | {lease.agent_id} | {lease.branch_name} | {lease.worktree_path} | {rem} |")
    return "\n".join(lines) + "\n"


def parse_args(args: list[str]) -> argparse.Namespace:
    """Parse CLI arguments for worktree fleet manager."""
    parser = argparse.ArgumentParser(
        description="Autonomous Git Worktree Fleet Allocator & Swarm Arbiter",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    alloc = sub.add_parser("allocate", help="Allocate a new worktree lease")
    alloc.add_argument("--agent", required=True, help="Agent identifier")
    alloc.add_argument("--ttl", type=float, default=DEFAULT_LEASE_TTL_SECONDS, help="Lease TTL in seconds")
    alloc.add_argument("--dir", type=Path, default=Path(".data/agent/worktrees"), help="Base worktree directory")

    stat = sub.add_parser("status", help="Display fleet status")
    stat.add_argument("--format", choices=["text", "json", "markdown", "sarif"], default="text")

    return parser.parse_args(args)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for standalone execution."""
    opts = parse_args(argv or sys.argv[1:])
    repo_root = Path.cwd()

    if opts.command == "allocate":
        lease = build_worktree_lease(opts.agent, opts.dir, opts.ttl)
        ok, msg = allocate_worktree(repo_root, lease)
        print(f"Allocated: {lease.lease_id}" if ok else f"Failed: {msg}")
        return 0 if ok else 1

    status = SwarmFleetStatus()
    if opts.format == "json":
        print(json.dumps(asdict(status), indent=2))
    elif opts.format == "sarif":
        print(json.dumps(to_sarif(status), indent=2))
    elif opts.format == "markdown":
        print(to_markdown(status))
    else:
        print(f"Fleet Status: {len(status.active_leases)} active, {len(status.stale_leases)} stale")
    return 0


if __name__ == "__main__":
    sys.exit(main())
