"""Unit tests for Autonomous Project Tooling and Recursive Engine."""

import sys
from pathlib import Path

# Add tools/ to sys.path for direct imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import json
import urllib.error

import pytest
from project_tooling import (
    GitHubAPIClient,
    audit_self_hardening,
    classify_content,
    main,
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


def test_triage_without_acceptance_criteria_says_so(tmp_path: Path) -> None:
    """An issue with no checkboxes cannot engage the TDD loop, and must say why."""
    result = triage_issue_content(42, "Add a thing", "Prose only, no checkboxes.")
    assert (result.completed_criteria, result.total_criteria) == (0, 0)
    assert "acceptance criteria" in result.suggested_comment.lower()


def test_main_triage_reads_body_from_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI path autonomous-triage.yml invokes: body arrives as a file, never inline."""
    body = tmp_path / "issue.md"
    body.write_text("An empirical observation.\n- [x] done\n- [ ] pending\n", encoding="utf-8")

    exit_code = main(["triage-issue", "--number", "7", "--title", "Observation report", "--body-file", str(body)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Issue #7" in out and "1/2" in out


def test_main_triage_tolerates_a_missing_body(capsys: pytest.CaptureFixture[str]) -> None:
    """`--body-file` is optional; omitting it must not crash the triage workflow."""
    assert main(["triage-issue", "--number", "9", "--title", "defect in parser"]) == 0
    assert "Issue #9" in capsys.readouterr().out


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


def test_github_client_token_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit token wins; otherwise GITHUB_TOKEN, then GH_TOKEN, then unauthenticated."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert GitHubAPIClient(token="explicit").token == "explicit"
    assert GitHubAPIClient().token == ""

    monkeypatch.setenv("GH_TOKEN", "from-gh")
    assert GitHubAPIClient().token == "from-gh"
    monkeypatch.setenv("GITHUB_TOKEN", "from-github")
    assert GitHubAPIClient().token == "from-github"


def test_github_client_sends_authenticated_json_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the request contract without touching the network."""
    captured: dict[str, object] = {}

    class _Response:
        def read(self) -> bytes:
            return json.dumps({"number": 1}).encode("utf-8")

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def _fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["auth"] = request.get_header("Authorization")
        captured["body"] = request.data
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    client = GitHubAPIClient(token="secret", base_url="https://example.com/")

    assert client.request("/issues/1", method="POST", data={"body": "hi"}) == {"number": 1}
    assert captured["url"] == "https://example.com/issues/1"
    assert (captured["method"], captured["auth"]) == ("POST", "Bearer secret")
    assert json.loads(captured["body"]) == {"body": "hi"}


def test_github_client_bounds_error_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Error bodies are caller-controlled, so the detail must be length-capped (CWE-209)."""

    def _raise(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 422, "Unprocessable", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    with pytest.raises(RuntimeError, match="HTTP 422"):
        GitHubAPIClient(token="t").request("/issues")
