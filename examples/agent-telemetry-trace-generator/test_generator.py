"""Unit tests for OpenTelemetry Agent Waterfall Trace Generator."""

from generator import (
    AgentTraceSession,
    Span,
    build_synthetic_agent_session,
    render_ascii_waterfall,
)


def test_build_synthetic_agent_session():
    goal = "Fix issue #42 via TDD"
    session = build_synthetic_agent_session(goal)

    assert session.session_goal == goal
    assert len(session.trace_id) == 32  # 16 bytes hex
    assert len(session.spans) == 5

    # Verify root span and child spans
    root_spans = [s for s in session.spans if s.parent_span_id is None]
    child_spans = [s for s in session.spans if s.parent_span_id is not None]

    assert len(root_spans) == 1
    assert root_spans[0].name == "AgentSession"
    assert len(child_spans) == 4


def test_session_token_aggregation():
    session = build_synthetic_agent_session("Token test")
    tokens = session.total_tokens()

    assert tokens["prompt"] > 0
    assert tokens["completion"] > 0
    assert tokens["cached"] > 0
    assert tokens["total"] == tokens["prompt"] + tokens["completion"]


def test_session_duration_calculation():
    s1 = Span("s1", "trace", "1", None, 100.0, 250.0)
    s2 = Span("s2", "trace", "2", "1", 120.0, 200.0)
    session = AgentTraceSession("trace", "duration test", [s1, s2])

    assert session.total_duration_ms() == 150.0


def test_render_ascii_waterfall():
    session = build_synthetic_agent_session("Render test")
    output = render_ascii_waterfall(session)

    assert "AGENT WATERFALL TRACE" in output
    assert "AgentSession" in output
    assert "FrontierPlanning" in output
    assert "Tokens: Prompt=" in output
