#!/usr/bin/env python3
"""Autonomous GitHub Project Tooling & Recursive Development Engine.

Enables autonomous agents and automated workflows to:
1. Triage, categorize, and label issues based on repository taxonomy.
2. Verify acceptance criteria checklists.
3. Enforce the recursive self-hardening loop (checking that defect fixes harden AGENTS.md).
4. Run client-side rate-managed GitHub API queries and comments.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Canonical taxonomic labels used across vibes
TAXONOMY_PATTERNS: dict[str, Sequence[str]] = {
    "observation": ("observation", "case study", "empirical", "investigation", "breakthrough"),
    "pattern": ("pattern", "playbook", "cegis", "fifo", "hardening", "epistemic"),
    "artifact": ("artifact", "prompt", "schema", "manifest", "template"),
    "bug": ("bug", "defect", "regression", "crash", "error", "failure", "broken"),
    "roadmap": ("roadmap", "milestone", "future", "vision", "epic"),
}

# Task checkbox pattern: - [ ] or - [x]
CHECKBOX_PATTERN = re.compile(r"^\s*-\s*\[([ xX])\]\s+(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class TriageResult:
    """Outcome of automated issue or task triage."""

    issue_number: int
    matched_labels: list[str]
    total_criteria: int
    completed_criteria: int
    is_fully_certified: bool
    suggested_comment: str


@dataclass
class HardeningAuditResult:
    """Outcome of recursive self-hardening check on a PR diff."""

    is_defect_fix: bool
    agents_md_modified: bool
    new_guardrails_detected: int
    is_compliant: bool
    summary: str


class GitHubAPIClient:
    """Rate-managed client for GitHub REST and GraphQL APIs."""

    def __init__(self, token: str | None = None, base_url: str = "https://api.github.com") -> None:
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        self.base_url = base_url.rstrip("/")

    def request(self, endpoint: str, method: str = "GET", data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute an authenticated HTTP request against GitHub API."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        payload = json.dumps(data).encode("utf-8") if data else None

        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "vibes-autonomous-project-tooling/0.2.0")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"GitHub API error HTTP {err.code}: {err_body[:256]}") from err


def classify_content(title: str, body: str) -> list[str]:
    """Classify text against repository taxonomy patterns."""
    text = f"{title}\n{body}".lower()
    labels: list[str] = []

    for label, keywords in TAXONOMY_PATTERNS.items():
        if any(kw in text for kw in keywords):
            labels.append(label)

    return labels or ["triage-needed"]


def parse_acceptance_criteria(body: str) -> tuple[int, int]:
    """Extract total and completed checkbox counts from markdown body."""
    matches = CHECKBOX_PATTERN.findall(body)
    total = len(matches)
    completed = sum(1 for mark, _ in matches if mark.strip().lower() == "x")
    return total, completed


def triage_issue_content(issue_number: int, title: str, body: str) -> TriageResult:
    """Analyze issue content and generate automated onboarding and classification plan."""
    labels = classify_content(title, body)
    total_criteria, completed_criteria = parse_acceptance_criteria(body)

    is_certified = total_criteria > 0 and total_criteria == completed_criteria

    comment_lines = [
        f"### 🤖 Autonomous Project Grounding — Issue #{issue_number}",
        "",
        f"- **Taxonomic Classification**: {', '.join(f'`{lbl}`' for lbl in labels)}",
        f"- **Acceptance Criteria**: {completed_criteria}/{total_criteria} completed",
        "",
    ]

    if total_criteria == 0:
        comment_lines.extend(
            [
                "> [!NOTE]",
                "> No markdown acceptance criteria checkboxes (`- [ ]`) were detected in this issue.",
                "> To engage the autonomous TDD loop, please author verifiable acceptance criteria.",
            ]
        )
    elif not is_certified:
        comment_lines.append("⏳ **Status**: Work in progress. Awaiting certification of all acceptance criteria.")
    else:
        comment_lines.append("✅ **Status**: All acceptance criteria are marked complete. Ready for verification.")

    return TriageResult(
        issue_number=issue_number,
        matched_labels=labels,
        total_criteria=total_criteria,
        completed_criteria=completed_criteria,
        is_fully_certified=is_certified,
        suggested_comment="\n".join(comment_lines),
    )


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

    # Subcommand: triage-issue
    triage_p = subparsers.add_parser("triage-issue", help="Triage and classify an issue")
    triage_p.add_argument("--number", type=int, required=True, help="Issue number")
    triage_p.add_argument("--title", type=str, default="", help="Issue title")
    triage_p.add_argument("--body-file", type=Path, help="File containing issue markdown body")
    triage_p.add_argument("--dry-run", action="store_true", help="Print plan without posting to GitHub API")

    # Subcommand: verify-hardening
    harden_p = subparsers.add_parser("verify-hardening", help="Verify self-hardening on a PR diff")
    harden_p.add_argument("--diff-file", type=Path, required=True, help="File containing git diff")
    harden_p.add_argument("--is-defect", action="store_true", help="Flag if PR addresses a bug or regression")

    args = parser.parse_args(argv)

    if args.command == "triage-issue":
        body = args.body_file.read_text(encoding="utf-8") if args.body_file else ""
        result = triage_issue_content(args.number, args.title, body)

        print(f"🎯 Triage plan for Issue #{result.issue_number}:")
        print(f"   Labels: {result.matched_labels}")
        print(f"   Criteria: {result.completed_criteria}/{result.total_criteria}")
        print(f"\n{result.suggested_comment}")
        return 0

    if args.command == "verify-hardening":
        diff_text = args.diff_file.read_text(encoding="utf-8")
        audit = audit_self_hardening(diff_text, is_defect_fix=args.is_defect)
        print("🔄 Recursive Self-Hardening Audit:")
        print(f"   Compliant: {audit.is_compliant}")
        print(f"   Summary: {audit.summary}")
        return 0 if audit.is_compliant else 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
