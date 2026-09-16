"""Unit tests for Autonomous Project Tooling and Recursive Engine."""

import sys
from pathlib import Path

# Add tools/ to sys.path for direct imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from project_tooling import (
    audit_self_hardening,
    classify_content,
    parse_acceptance_criteria,
    triage_issue_content,
)


def test_classify_content_multiple_matches():
    title = "Investigate performance defect in AST repomap"
    body = "Here is an empirical observation of the memory regression."
    labels = classify_content(title, body)
    assert "observation" in labels
    assert "bug" in labels


def test_classify_content_fallback():
    title = "Hello world"
    body = "Just saying hi"
    labels = classify_content(title, body)
    assert labels == ["triage-needed"]


def test_parse_acceptance_criteria_mixed():
    markdown = """
## Acceptance Criteria
- [ ] Implement parser
- [x] Write unit tests
- [X] Verify cyclomatic complexity
- [ ] Add documentation
"""
    total, completed = parse_acceptance_criteria(markdown)
    assert total == 4
    assert completed == 2


def test_triage_issue_content_incomplete():
    body = "- [x] Step 1\n- [ ] Step 2"
    res = triage_issue_content(42, "New observation on rate limiting", body)
    assert (
        res.issue_number,
        "observation" in res.matched_labels,
        res.total_criteria,
        res.completed_criteria,
        res.is_fully_certified,
        "Work in progress" in res.suggested_comment,
    ) == (42, True, 2, 1, False, True)


def test_triage_issue_content_complete():
    body = "- [x] Step 1\n- [x] Step 2"
    res = triage_issue_content(101, "New pattern: CEGIS debugging", body)
    assert (res.is_fully_certified, "All acceptance criteria are marked complete" in res.suggested_comment) == (True, True)


def test_audit_self_hardening_defect_without_agents_md_fails():
    diff = """
diff --git a/src/parser.py b/src/parser.py
--- a/src/parser.py
+++ b/src/parser.py
@@ -10,3 +10,3 @@
- old_buggy_line()
+ fixed_line()
"""
    result = audit_self_hardening(diff, is_defect_fix=True)
    assert (
        result.is_compliant,
        result.agents_md_modified,
        "Recursive hardening violation" in result.summary,
    ) == (False, False, True)


def test_audit_self_hardening_defect_with_agents_md_succeeds():
    diff = """
diff --git a/src/parser.py b/src/parser.py
--- a/src/parser.py
+++ b/src/parser.py
@@ -10,3 +10,3 @@
- old_buggy_line()
+ fixed_line()
diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -50,3 +50,4 @@
+ - Mandatory Guardrail: Agents must always sanitize inputs before parsing.
"""
    result = audit_self_hardening(diff, is_defect_fix=True)
    assert (
        result.is_compliant,
        result.agents_md_modified,
        result.new_guardrails_detected >= 1,
        "Positive recursive hardening verified" in result.summary,
    ) == (True, True, True, True)


def test_audit_self_hardening_non_defect_always_compliant():
    diff = "diff --git a/README.md b/README.md"
    result = audit_self_hardening(diff, is_defect_fix=False)
    assert result.is_compliant is True
