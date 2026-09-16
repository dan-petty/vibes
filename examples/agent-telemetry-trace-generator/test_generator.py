import json
from unittest.mock import MagicMock
import urllib.error
import urllib.request
import pytest

from generator import (
    AgentTraceSession,
    Span,
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
    assert (len(root_spans), root_spans[0].name, len(child_spans)) == (1, "AgentSession", 4)


def test_session_token_aggregation() -> None:
    session = build_synthetic_agent_session("Token test")
    tokens = session.total_tokens()

    assert all(tokens[k] > 0 for k in ("prompt", "completion", "cached"))
    assert tokens["total"] == tokens["prompt"] + tokens["completion"]


def test_session_duration_calculation() -> None:
    s1 = Span("s1", "trace", "1", None, 100.0, 250.0)
    s2 = Span("s2", "trace", "2", "1", 120.0, 200.0)
    session = AgentTraceSession("trace", "duration test", [s1, s2])

    assert session.total_duration_ms() == 150.0


def test_render_ascii_waterfall() -> None:
    session = build_synthetic_agent_session("Render test")
    output = render_ascii_waterfall(session)

    expected_snippets = ["AGENT WATERFALL TRACE", "AgentSession", "FrontierPlanning", "Tokens: Prompt="]
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

    # Verify semantic attributes presence
    attr_keys = {attr["key"] for s in spans for attr in s.get("attributes", [])}
    assert {"ai.model.tier", "agent.persona", "ai.tokens.prompt"}.issubset(attr_keys)


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
