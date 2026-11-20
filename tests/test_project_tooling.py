"""Unit tests for Autonomous Project Tooling and Recursive Engine."""

import sys
from pathlib import Path

# Add tools/ to sys.path for direct imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import pytest
from project_tooling import (
    audit_self_hardening,
    count_guardrail_lines,
    main,
)


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


def test_main_verify_hardening_exit_code_carries_the_verdict(tmp_path: Path) -> None:
    """recursive-hardening.yml branches on this exit code, so it must reflect compliance."""
    diff = tmp_path / "pr.diff"
    diff.write_text("+++ b/src/thing.py\n+def fixed(): ...\n", encoding="utf-8")
    assert main(["verify-hardening", "--diff-file", str(diff), "--is-defect"]) == 1

    diff.write_text(
        "+++ b/AGENTS.md\n"
        "+- **Mandate**: never interpolate event data into workflow shell; read it from env.\n",
        encoding="utf-8",
    )
    assert main(["verify-hardening", "--diff-file", str(diff), "--is-defect"]) == 0


def test_main_rejects_an_unknown_subcommand() -> None:
    """A mistyped subcommand must fail loudly rather than silently doing nothing."""
    with pytest.raises(SystemExit) as excinfo:
        main(["not-a-command"])
    assert excinfo.value.code != 0


# --- Hardening detection scope -------------------------------------------------------
#
# The audit ran against five literal keywords across the whole patch. It therefore counted
# a line containing "rule" in an unrelated file as hardening AGENTS.md, and rejected a real
# guardrail reading "Never rebuild text by splitting on a marker and re-joining it" because
# the imperative uses none of the five words -- then reported "AGENTS.md was not updated"
# about a pull request that had updated it. Found by running the workflow on PR #8.

_UNRELATED_KEYWORD_DIFF = (
    "diff --git a/tools/thing.py b/tools/thing.py\n"
    "--- a/tools/thing.py\n"
    "+++ b/tools/thing.py\n"
    "+# this rule has nothing to do with AGENTS.md\n"
)

_IMPERATIVE_GUARDRAIL_DIFF = (
    "diff --git a/AGENTS.md b/AGENTS.md\n"
    "--- a/AGENTS.md\n"
    "+++ b/AGENTS.md\n"
    "+   - **Never rebuild text by splitting on a marker and re-joining it.**\n"
)


def test_a_keyword_in_another_file_is_not_hardening(tmp_path: Path) -> None:
    """The count is scoped to AGENTS.md, so an unrelated file cannot satisfy the mandate."""
    result = audit_self_hardening(_UNRELATED_KEYWORD_DIFF, is_defect_fix=True)
    assert (result.agents_md_modified, result.new_guardrails_detected, result.is_compliant) == (
        False,
        0,
        False,
    )


def test_a_guardrail_written_in_the_imperative_counts(tmp_path: Path) -> None:
    """"Never do X" is an instruction; a keyword list that misses it rejects real hardening."""
    result = audit_self_hardening(_IMPERATIVE_GUARDRAIL_DIFF, is_defect_fix=True)
    assert (result.agents_md_modified, result.new_guardrails_detected, result.is_compliant) == (
        True,
        1,
        True,
    )


def test_both_diff_header_forms_are_recognised() -> None:
    """A patch fragment carries only `+++`; recognising one form would silently count zero."""
    abbreviated = "+++ b/AGENTS.md\n+   - **Never do the thing.**\n"
    full = _IMPERATIVE_GUARDRAIL_DIFF
    assert (count_guardrail_lines(abbreviated), count_guardrail_lines(full)) == (1, 1)


def test_touching_agents_md_without_adding_anything_says_so() -> None:
    """A verdict that misnames its own cause sends the reader to the wrong file."""
    deletion_only = "diff --git a/AGENTS.md b/AGENTS.md\n--- a/AGENTS.md\n+++ b/AGENTS.md\n-old line\n"
    result = audit_self_hardening(deletion_only, is_defect_fix=True)
    assert (result.agents_md_modified, result.is_compliant, "changed AGENTS.md" in result.summary) == (
        True,
        False,
        True,
    )


def test_blank_added_lines_are_not_instruction() -> None:
    """Whitespace added to AGENTS.md is not a guardrail, and must not satisfy the mandate."""
    blank_only = "diff --git a/AGENTS.md b/AGENTS.md\n+++ b/AGENTS.md\n+\n+   \n"
    assert audit_self_hardening(blank_only, is_defect_fix=True).is_compliant is False


def test_process_diff_line_helper() -> None:
    """Verify diff line processing state machine and addition extraction."""
    from project_tooling import _process_diff_line

    transitions = (
        _process_diff_line("diff --git a/AGENTS.md b/AGENTS.md", False),
        _process_diff_line("+++ b/AGENTS.md", False),
        _process_diff_line("+   - **Rule**: always test.", True),
        _process_diff_line("- old text", True),
        _process_diff_line(" unchanged context", True),
        _process_diff_line("diff --git a/other.py b/other.py", True),
        _process_diff_line("+ line in other", False),
    )

    assert transitions == (
        (True, None),
        (True, None),
        (True, "   - **Rule**: always test."),
        (True, None),
        (True, None),
        (False, None),
        (False, None),
    )

