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

    @property
    def duration_ms(self) -> float:
        return self.end_time_ms - self.start_time_ms


@dataclass
class AgentTraceSession:
    """A collection of spans representing an entire agentic task lifecycle."""

    trace_id: str
    session_goal: str
    spans: list[Span] = field(default_factory=list)

    def total_duration_ms(self) -> float:
        if not self.spans:
            return 0.0
        start = min(s.start_time_ms for s in self.spans)
        end = max(s.end_time_ms for s in self.spans)
        return end - start

    def total_tokens(self) -> dict[str, int]:
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
        },
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
        },
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
        },
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
        },
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
    args = parser.parse_args(argv)

    session = build_synthetic_agent_session(args.goal)

    if args.json:
        data = {
            "trace_id": session.trace_id,
            "session_goal": session.session_goal,
            "tokens": session.total_tokens(),
            "spans": [asdict(s) for s in session.spans],
        }
        print(json.dumps(data, indent=2))
    else:
        print(render_ascii_waterfall(session))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
