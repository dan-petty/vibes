import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest
from generator import (
    STATUS_ERROR,
    STATUS_UNSET,
    AgentTraceSession,
    Span,
    _format_otlp_status,
    build_synthetic_agent_session,
    export_otlp_http,
    main,
    render_ascii_waterfall,
    to_otlp_json,
)


def test_build_synthetic_agent_session() -> None:
    goal = "Fix issue #42 via TDD"
    session = build_synthetic_agent_session(goal)

    root_spans = [s for s in session.spans if s.parent_span_id is None]
    child_spans = [s for s in session.spans if s.parent_span_id is not None]

    assert (session.session_goal, len(session.trace_id), len(session.spans)) == (goal, 32, 5)
    assert (len(root_spans), root_spans[0].name, len(child_spans)) == (
        1, f"invoke_workflow {goal}", 4
    )


def test_session_token_aggregation() -> None:
    session = build_synthetic_agent_session("Token test")
    tokens = session.total_tokens()

    assert all(tokens[k] > 0 for k in ("input", "output", "cached"))
    assert tokens["total"] == tokens["input"] + tokens["output"]


def test_session_duration_calculation() -> None:
    s1 = Span("s1", "trace", "1", None, 100.0, 250.0)
    s2 = Span("s2", "trace", "2", "1", 120.0, 200.0)
    session = AgentTraceSession("trace", "duration test", [s1, s2])

    assert session.total_duration_ms() == 150.0


def test_render_ascii_waterfall() -> None:
    session = build_synthetic_agent_session("Render test")
    output = render_ascii_waterfall(session)

    expected_snippets = [
        "AGENT WATERFALL TRACE", "invoke_workflow", "chat claude-3-5-sonnet", "Tokens: Input=",
    ]
    assert all(snippet in output for snippet in expected_snippets)


def test_to_otlp_json_schema_conformance() -> None:
    session = build_synthetic_agent_session("OTLP Schema test")
    otlp = to_otlp_json(session, service_name="test-agent-mesh")

    assert "resourceSpans" in otlp
    resource_span = otlp["resourceSpans"][0]
    scope_span = resource_span["scopeSpans"][0]
    spans = scope_span["spans"]

    assert (len(spans), scope_span["scope"]["name"]) == (5, "vibes.agent.waterfall.generator")
    first_span = spans[0]
    assert (first_span["traceId"], first_span["kind"], first_span["status"]["code"]) == (session.trace_id, 1, 1)

    # The conventional keys, not a private vocabulary that only this repository can read.
    attr_keys = {attr["key"] for s in spans for attr in s.get("attributes", [])}
    assert {
        "gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.usage.input_tokens",
    }.issubset(attr_keys)
    assert not {key for key in attr_keys if key.startswith("ai.")}


def test_export_otlp_http_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session = build_synthetic_agent_session("HTTP export test")

    # Mock success HTTP 200
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=5.0: mock_resp)
    assert export_otlp_http(session, "http://localhost:4318/v1/traces") is True

    # Mock failure with URLError
    def mock_fail(req, timeout=5.0):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", mock_fail)
    assert export_otlp_http(session, "http://localhost:4318/v1/traces") is False


def test_main_cli_otlp_output(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--otlp", "--goal", "CLI OTLP Goal"])
    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert (exit_code, "resourceSpans" in data) == (0, True)


def test_main_cli_export_otlp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("generator.export_otlp_http", lambda s, endpoint: True)
    exit_code = main(["--export-otlp", "http://localhost:4318/v1/traces"])
    assert exit_code == 0


def test_span_status_defaults_to_unset_not_ok():
    """UNSET means no judgement was recorded; exporting OK by default asserts a lie."""
    span = Span(
        name="unjudged", trace_id="t", span_id="s", parent_span_id=None,
        start_time_ms=0.0, end_time_ms=1.0,
    )
    assert _format_otlp_status(span) == {"code": STATUS_UNSET}


def test_error_status_is_exported_with_its_message():
    """A failed span reporting success is the one thing a trace must never do."""
    span = Span(
        name="failed", trace_id="t", span_id="s", parent_span_id=None,
        start_time_ms=0.0, end_time_ms=1.0,
        status_code=STATUS_ERROR, status_message="tool call rejected by contract gate",
    )
    assert _format_otlp_status(span) == {
        "code": STATUS_ERROR,
        "message": "tool call rejected by contract gate",
    }


def test_main_cli_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify --json CLI flag emits parsed trace session dictionary."""
    exit_code = main(["--json", "--goal", "JSON CLI Goal"])
    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert (exit_code, data["session_goal"], "spans" in data) == (0, "JSON CLI Goal", True)


def test_main_cli_validate_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify --validate CLI flag runs conformance check and reports status."""
    exit_code = main(["--validate"])
    captured = capsys.readouterr().out
    assert (exit_code, "Convention snapshot fetched" in captured) == (0, True)


def test_main_cli_default_waterfall(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify default CLI execution without formatting flags prints ASCII waterfall."""
    exit_code = main(["--goal", "Default Waterfall Goal"])
    captured = capsys.readouterr().out
    assert (exit_code, "AGENT WATERFALL TRACE" in captured) == (0, True)


def test_main_cli_export_otlp_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify --export-otlp returns exit code 1 when transport fails."""
    monkeypatch.setattr("generator.export_otlp_http", lambda s, endpoint: False)
    exit_code = main(["--export-otlp", "http://localhost:4318/v1/traces"])
    assert exit_code == 1
