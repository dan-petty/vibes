"""Unit tests for OpenTelemetry Agent Waterfall Trace Generator."""

from generator import (
    AgentTraceSession,
    Span,
    build_synthetic_agent_session,
    render_ascii_waterfall,
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
