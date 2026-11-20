#!/usr/bin/env python3
"""Autonomous GitHub Project Tooling & Recursive Development Engine.

Enforces the recursive self-hardening loop: it checks that a defect fix hardens AGENTS.md.
`recursive-hardening.yml` runs the `verify-hardening` audit on the diff of a merged pull request.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HardeningAuditResult:
    """Outcome of recursive self-hardening check on a PR diff."""

    is_defect_fix: bool
    agents_md_modified: bool
    new_guardrails_detected: int
    is_compliant: bool
    summary: str


AGENTS_FILENAME = "AGENTS.md"
# Both header forms. A real `git diff` emits `diff --git`, but a patch fragment or a plain
# `diff -u` carries only the `+++` line, and a parser that recognized one form would report
# zero additions for the other -- silently, which is the failure this function exists to end.
_DIFF_HEADER = re.compile(r"^diff --git a/(?P<path>\S+) b/\S+")
_TARGET_HEADER = re.compile(r"^\+\+\+ (?:b/)?(?P<path>\S+)")


def _file_under_diff(line: str) -> str | None:
    """Return the file a diff header introduces, tolerating both header forms."""
    match = _DIFF_HEADER.match(line) or _TARGET_HEADER.match(line)
    return match.group("path") if match else None


def _process_diff_line(line: str, in_agents: bool) -> tuple[bool, str | None]:
    """Return updated in_agents state and any line added to AGENTS.md."""
    current = _file_under_diff(line)
    if current is not None:
        return current == AGENTS_FILENAME, None
    if in_agents and line.startswith("+"):
        return in_agents, line[1:]
    return in_agents, None


def _agents_md_additions(diff_text: str) -> list[str]:
    """Return the lines a unified diff adds to AGENTS.md, and only those.

    Scoped to the file. The previous implementation scanned the whole patch for five
    literal keywords, so a line containing the word "rule" in any file at all counted as
    hardening AGENTS.md — including a diff that never touched it.
    """
    added: list[str] = []
    in_agents = False
    for line in diff_text.splitlines():
        in_agents, added_line = _process_diff_line(line, in_agents)
        if added_line is not None:
            added.append(added_line)
    return added


def count_guardrail_lines(diff_text: str) -> int:
    """Count the instruction lines a diff adds to AGENTS.md.

    Counts lines rather than matching keywords. The keyword list — `mandate`, `rule`,
    `guardrail`, `prohibited`, `must always` — rejected a guardrail reading "Never rebuild
    text by splitting on a marker and re-joining it", which is an instruction in the
    imperative containing none of those five words, and the workflow opened an issue
    demanding hardening that had already been written. A keyword scan over prose is a
    proxy with an unbounded false-negative surface and a one-word workaround; what the
    audit can actually observe is whether the file gained instruction.
    """
    return sum(1 for line in _agents_md_additions(diff_text) if line.strip())


def generate_hardening_summary(
    is_defect_fix: bool, is_compliant: bool, new_guardrails: int, agents_md_touched: bool = False
) -> str:
    """Produce an actionable summary that says which of the two failures occurred.

    "AGENTS.md was not updated" was reported for a pull request that had updated it,
    because the keyword scan found nothing. A verdict that misnames its own cause sends
    the reader to the wrong file, so the touched-but-empty case now says so.
    """
    if not is_defect_fix:
        return "Standard deliverable (no defect tag); AGENTS.md hardening is optional."
    if is_compliant:
        return f"Positive recursive hardening verified! AGENTS.md gained {new_guardrails} line(s)."
    if agents_md_touched:
        return (
            "Recursive hardening violation: This PR fixes a defect and changed AGENTS.md, but "
            "added no instruction to it. Codify the guardrail that prevents this defect class."
        )
    return (
        "Recursive hardening violation: This PR fixes a defect, but AGENTS.md was not updated. "
        "Autonomous engineering requires codifying preventative instructions on every defect fix."
    )


def audit_self_hardening(diff_text: str, is_defect_fix: bool = False) -> HardeningAuditResult:
    """Verify that a defect fix pull request includes updates to AGENTS.md."""
    agents_md_touched = any(
        _file_under_diff(line) == AGENTS_FILENAME for line in diff_text.splitlines()
    )
    new_guardrails = count_guardrail_lines(diff_text)
    is_compliant = not is_defect_fix or new_guardrails > 0
    summary = generate_hardening_summary(
        is_defect_fix, is_compliant, new_guardrails, agents_md_touched
    )

    return HardeningAuditResult(
        is_defect_fix=is_defect_fix,
        agents_md_modified=agents_md_touched,
        new_guardrails_detected=new_guardrails,
        is_compliant=is_compliant,
        summary=summary,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI dispatcher for autonomous project tooling."""
    parser = argparse.ArgumentParser(description="Autonomous GitHub Project Tooling & Recursive Engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: verify-hardening
    harden_p = subparsers.add_parser("verify-hardening", help="Verify self-hardening on a PR diff")
    harden_p.add_argument("--diff-file", type=Path, required=True, help="File containing git diff")
    harden_p.add_argument("--is-defect", action="store_true", help="The pull request fixes a defect (label type/bug)")

    args = parser.parse_args(argv)
    diff_text = args.diff_file.read_text(encoding="utf-8")
    audit = audit_self_hardening(diff_text, is_defect_fix=args.is_defect)
    print("🔄 Recursive Self-Hardening Audit:")
    print(f"   Compliant: {audit.is_compliant}")
    print(f"   Summary: {audit.summary}")
    return 0 if audit.is_compliant else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
