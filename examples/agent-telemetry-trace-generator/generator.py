#!/usr/bin/env python3
"""OpenTelemetry Agent Waterfall Trace Generator.

Models multi-turn autonomous agent sessions and emits OpenTelemetry-compliant
spans with token spend attributes, model tier metadata, and ASCII waterfall visualizations.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence
import urllib.error
import urllib.request


STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2


@dataclass
class Span:
    """Represents a single OpenTelemetry distributed trace span."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_time_ms: float
    end_time_ms: float
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    # OTLP status: 0 UNSET, 1 OK, 2 ERROR. UNSET is what the specification means by
    # "no explicit judgement recorded". Exporting a hard-coded OK made every failed span
    # read as successful, which is the one thing a trace exists to tell you.
    status_code: int = STATUS_UNSET
    status_message: str = ""

    @property
    def duration_ms(self) -> float:
        """Calculate elapsed duration of span in milliseconds."""
        return self.end_time_ms - self.start_time_ms


@dataclass
class AgentTraceSession:
    """A collection of spans representing an entire agentic task lifecycle."""

    trace_id: str
    session_goal: str
    spans: list[Span] = field(default_factory=list)

    def total_duration_ms(self) -> float:
        """Calculate cumulative duration across all recorded session spans."""
        if not self.spans:
            return 0.0
        start = min(s.start_time_ms for s in self.spans)
        end = max(s.end_time_ms for s in self.spans)
        return end - start

    def total_tokens(self) -> dict[str, int]:
        """Aggregate prompt, completion, and cached token consumption."""
        prompt_tokens = sum(s.attributes.get("ai.tokens.prompt", 0) for s in self.spans)
        completion_tokens = sum(s.attributes.get("ai.tokens.completion", 0) for s in self.spans)
        cached_tokens = sum(s.attributes.get("ai.tokens.cached", 0) for s in self.spans)
        return {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "cached": cached_tokens,
            "total": prompt_tokens + completion_tokens,
        }


def generate_id(byte_count: int) -> str:
    """Generate cryptographically random hex identifier."""
    return secrets.token_hex(byte_count)


def build_synthetic_agent_session(goal: str) -> AgentTraceSession:
    """Construct an exemplary agent session trace spanning frontier and local tiers."""
    trace_id = generate_id(16)
    base_time = time.time() * 1000.0

    session = AgentTraceSession(trace_id=trace_id, session_goal=goal)

    # 1. Root Session Span
    root_span_id = generate_id(8)

    # 2. Frontier Planning Span
    plan_start = base_time + 10.0
    plan_end = plan_start + 450.0
    plan_span = Span(
        name="FrontierPlanning",
        trace_id=trace_id,
        span_id=generate_id(8),
        parent_span_id=root_span_id,
        start_time_ms=plan_start,
        end_time_ms=plan_end,
        attributes={
            "ai.model.tier": "frontier",
            "ai.model.name": "claude-3-5-sonnet",
            "ai.tokens.prompt": 3400,
            "ai.tokens.completion": 820,
            "ai.tokens.cached": 18000,
            "ai.step": "architectural_plan",
            "agent.persona": "architect",
            "agent.cache_hit": True,
        },
        status_code=STATUS_OK,
    )

    # 3. Sub-Agent Offloaded AST Symbol Exploration (Local Tier)
    ast_start = plan_end + 20.0
    ast_end = ast_start + 180.0
    ast_span = Span(
        name="LocalSubAgentOffload:ASTExplore",
        trace_id=trace_id,
        span_id=generate_id(8),
        parent_span_id=root_span_id,
        start_time_ms=ast_start,
        end_time_ms=ast_end,
        attributes={
            "ai.model.tier": "local",
            "ai.model.name": "qwen2.5-coder:14b",
            "ai.tokens.prompt": 42000,
            "ai.tokens.completion": 1200,
            "ai.step": "symbol_extraction",
            "local.offloaded": True,
            "agent.persona": "explorer",
            "agent.tool.call_name": "ast_symbol_extractor",
            "agent.cache_hit": False,
        },
        status_code=STATUS_OK,
    )

    # 4. CEGIS Defect Synthesis Loop
    cegis_start = ast_end + 30.0
    cegis_end = cegis_start + 320.0
    cegis_span = Span(
        name="CEGISConstraintLoop",
        trace_id=trace_id,
        span_id=generate_id(8),
        parent_span_id=root_span_id,
        start_time_ms=cegis_start,
        end_time_ms=cegis_end,
        attributes={
            "ai.model.tier": "frontier",
            "ai.model.name": "claude-3-5-sonnet",
            "ai.tokens.prompt": 6200,
            "ai.tokens.completion": 510,
            "cegis.rounds": 3,
            "cegis.converged": True,
            "agent.persona": "coder",
            "agent.tool.call_name": "cegis_synthesizer",
            "agent.verification_result": "PASS",
        },
        status_code=STATUS_OK,
    )

    # 5. AST Sentinel Invariant Gate
    gate_start = cegis_end + 15.0
    gate_end = gate_start + 45.0
    gate_span = Span(
        name="ASTInvariantSentinel",
        trace_id=trace_id,
        span_id=generate_id(8),
        parent_span_id=root_span_id,
        start_time_ms=gate_start,
        end_time_ms=gate_end,
        attributes={
            "sentinel.complexity.max": 4,
            "sentinel.nesting.max": 3,
            "sentinel.status": "APPROVED",
            "agent.persona": "reviewer",
            "agent.tool.call_name": "ast_invariant_sentinel",
            "agent.verification_result": "APPROVED",
        },
        status_code=STATUS_OK,
    )

    # Root span encompasses all children
    root_span = Span(
        name="AgentSession",
        trace_id=trace_id,
        span_id=root_span_id,
        parent_span_id=None,
        start_time_ms=base_time,
        end_time_ms=gate_end + 10.0,
        attributes={
            "session.goal": goal,
            "session.status": "COMPLETED",
        },
        status_code=STATUS_OK,
    )

    session.spans = [root_span, plan_span, ast_span, cegis_span, gate_span]
    return session


def render_ascii_waterfall(session: AgentTraceSession, bar_width: int = 40) -> str:
    """Render an aesthetic ASCII waterfall timeline of agent spans."""
    if not session.spans:
        return "No spans recorded."

    total_ms = session.total_duration_ms() or 1.0
    min_time = min(s.start_time_ms for s in session.spans)

    lines = [
        "=" * 78,
        f"📊 AGENT WATERFALL TRACE [TraceID: {session.trace_id[:8]}...]",
        f"Goal: {session.session_goal}",
        f"Total Duration: {total_ms:.1f}ms",
        "=" * 78,
        f"{'SPAN NAME':<34} | {'DURATION':<9} | TIMELINE WATERFALL",
        "-" * 78,
    ]

    for s in session.spans:
        indent = "  " if s.parent_span_id else ""
        label = f"{indent}{s.name}"[:34]

        offset_fraction = (s.start_time_ms - min_time) / total_ms
        duration_fraction = s.duration_ms / total_ms

        offset_spaces = int(offset_fraction * bar_width)
        bar_chars = max(1, int(duration_fraction * bar_width))

        bar = " " * offset_spaces + "█" * bar_chars
        lines.append(f"{label:<34} | {s.duration_ms:6.1f}ms | {bar}")

    lines.append("-" * 78)
    tokens = session.total_tokens()
    lines.append(
        f"Tokens: Prompt={tokens['prompt']}, Completion={tokens['completion']}, "
        f"Cached={tokens['cached']} (Total: {tokens['total']})"
    )
    lines.append("=" * 78)
    return "\n".join(lines)


def _format_otlp_attribute_value(val: Any) -> dict[str, Any]:
    """Convert Python primitive into OTLP AnyValue object."""
    if isinstance(val, bool):
        return {"boolValue": val}
    if isinstance(val, int):
        return {"intValue": val}
    if isinstance(val, float):
        return {"doubleValue": val}
    return {"stringValue": str(val)}


def _format_otlp_status(span: Span) -> dict[str, Any]:
    """Render a span's status, omitting the message when none was recorded."""
    status: dict[str, Any] = {"code": span.status_code}
    if span.status_message:
        status["message"] = span.status_message
    return status


def _format_otlp_span(span: Span) -> dict[str, Any]:
    """Convert an internal Span into an OTLP-compliant span dictionary."""
    start_nano = str(int(span.start_time_ms * 1_000_000))
    end_nano = str(int(span.end_time_ms * 1_000_000))

    attrs = [
        {"key": k, "value": _format_otlp_attribute_value(v)}
        for k, v in sorted(span.attributes.items())
    ]

    otlp_span: dict[str, Any] = {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "name": span.name,
        "kind": 1,
        "startTimeUnixNano": start_nano,
        "endTimeUnixNano": end_nano,
        "attributes": attrs,
        "status": _format_otlp_status(span),
    }
    if span.parent_span_id:
        otlp_span["parentSpanId"] = span.parent_span_id
    return otlp_span


def to_otlp_json(session: AgentTraceSession, service_name: str = "vibes-agent-mesh") -> dict[str, Any]:
    """Serialize trace session into standard OpenTelemetry OTLP/HTTP Protobuf-JSON schema."""
    otlp_spans = [_format_otlp_span(s) for s in session.spans]

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service_name}},
                        {"key": "service.version", "value": {"stringValue": "0.3.0"}},
                        {"key": "telemetry.sdk.language", "value": {"stringValue": "python"}},
                        {"key": "session.goal", "value": {"stringValue": session.session_goal}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "vibes.agent.waterfall.generator",
                            "version": "0.3.0",
                        },
                        "spans": otlp_spans,
                    }
                ],
            }
        ]
    }


def export_otlp_http(
    session: AgentTraceSession,
    endpoint: str = "http://localhost:4318/v1/traces",
    service_name: str = "vibes-agent-mesh",
    timeout: float = 5.0,
) -> bool:
    """Stream trace spans over OTLP/HTTP to an OpenTelemetry collector or Jaeger."""
    payload = json.dumps(to_otlp_json(session, service_name=service_name)).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        print(f"OTLP export notice: Collector at {endpoint} unavailable: {err}", file=sys.stderr)
        return False


def main(argv: Sequence[str] | None = None) -> int:
    """CLI runner for telemetry generator."""
    parser = argparse.ArgumentParser(description="OpenTelemetry Agent Waterfall Trace Generator")
    parser.add_argument(
        "--goal",
        type=str,
        default="Refactor legacy parser with CEGIS & AST complexity constraints",
        help="Session goal description",
    )
    parser.add_argument("--json", action="store_true", help="Emit trace spans as OpenTelemetry JSON")
    parser.add_argument("--otlp", action="store_true", help="Emit trace spans as standard OTLP Protobuf-JSON")
    parser.add_argument(
        "--export-otlp",
        type=str,
        metavar="ENDPOINT",
        help="Stream spans over OTLP/HTTP to collector endpoint (e.g. http://localhost:4318/v1/traces)",
    )
    args = parser.parse_args(argv)

    session = build_synthetic_agent_session(args.goal)

    if args.export_otlp:
        success = export_otlp_http(session, endpoint=args.export_otlp)
        print(f"OTLP Export {'succeeded' if success else 'failed'} -> {args.export_otlp}")
        return 0 if success else 1

    if args.otlp:
        print(json.dumps(to_otlp_json(session), indent=2))
        return 0

    if args.json:
        data = {
            "trace_id": session.trace_id,
            "session_goal": session.session_goal,
            "tokens": session.total_tokens(),
            "spans": [asdict(s) for s in session.spans],
        }
        print(json.dumps(data, indent=2))
        return 0

    print(render_ascii_waterfall(session))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
