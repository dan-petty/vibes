#!/usr/bin/env python3
"""OpenTelemetry Agent Waterfall Trace Generator.

Models multi-turn autonomous agent sessions and emits spans that follow the OpenTelemetry
GenAI semantic conventions, with token spend, provider metadata and ASCII waterfalls.

It did not always. For a long time this generator emitted valid OTLP envelopes carrying a
private vocabulary — `ai.tokens.prompt`, `ai.model.name`, `agent.persona` — and hard-coded
every span kind to INTERNAL. Every collector accepted the payload and no backend could
chart it, because conformance to the wire format is not conformance to the convention and
nothing in the payload tells them apart. A span with the wrong key is never rejected; it
simply never appears in the dashboard built to read it, and an empty dashboard reads as
*no traffic*.

The conventional attributes now come from [`semconv.py`](./semconv.py), which reads a
snapshot derived from the upstream model at a pinned commit. Attributes outside the
`gen_ai.` namespace remain this repository's own — the convention permits them, and what it
does not permit is a private key standing in for a conventional one.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Importable whether this file is run from its own directory, imported by a test runner
# rooted elsewhere, or copied out of the repository, which is what an exhibit is for.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import semconv

STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2

# OTLP SpanKind. Every span used to be exported as INTERNAL, which removes a client call to
# a model from every service graph that is built by following CLIENT edges.
SPAN_KINDS: dict[str, int] = {"internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}


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
    # The convention's span type, and the kind it declares. Carried so the span can be
    # checked against the convention rather than merely asserted to follow it.
    span_type: str = "gen_ai.invoke_workflow.internal"
    kind: str = "internal"

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
        """Aggregate input, output and cache-read token consumption.

        Reads the conventional keys. The previous version summed `ai.tokens.prompt`, which
        is the name the convention deprecated in favour of `gen_ai.usage.input_tokens` —
        so an aggregate over conformant spans returned zero, and zero reads as no traffic.
        """
        input_tokens = sum(s.attributes.get("gen_ai.usage.input_tokens", 0) for s in self.spans)
        output_tokens = sum(s.attributes.get("gen_ai.usage.output_tokens", 0) for s in self.spans)
        cached = sum(s.attributes.get("gen_ai.usage.cache_read.input_tokens", 0) for s in self.spans)
        return {
            "input": input_tokens,
            "output": output_tokens,
            "cached": cached,
            "total": input_tokens + output_tokens,
        }


def generate_id(byte_count: int) -> str:
    """Generate cryptographically random hex identifier."""
    return secrets.token_hex(byte_count)


# One row per span in the exemplary session. A table rather than five near-identical
# constructor calls, so the conventional attributes of each span are readable side by side
# and a missing one is visible as a gap in a column.
SESSION_STEPS: tuple[dict[str, Any], ...] = (
    {
        "step": "plan",
        "duration_ms": 450.0,
        "span_type": "gen_ai.inference.client",
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.request.model": "claude-3-5-sonnet",
            "gen_ai.response.model": "claude-3-5-sonnet",
            "gen_ai.response.finish_reasons": ["stop"],
            "gen_ai.usage.input_tokens": 3400,
            "gen_ai.usage.output_tokens": 820,
            "gen_ai.usage.cache_read.input_tokens": 18000,
            "server.address": "example.com",
            "vibes.model.tier": "frontier",
            "vibes.agent.persona": "architect",
        },
    },
    {
        "step": "explore",
        "duration_ms": 180.0,
        "span_type": "gen_ai.inference.client",
        "attributes": {
            "gen_ai.operation.name": "chat",
            # Not a well-known value, which the validator reports as a *warning* rather than
            # an error: OpenTelemetry enums are open unless declared closed, so a local
            # runtime is allowed here and a typo would look exactly the same. The exhibit
            # keeps it so that distinction is visible in the output rather than described.
            "gen_ai.provider.name": "ollama",
            "gen_ai.request.model": "qwen2.5-coder:14b",
            "gen_ai.response.finish_reasons": ["stop"],
            "gen_ai.usage.input_tokens": 42000,
            "gen_ai.usage.output_tokens": 1200,
            "server.address": "localhost",
            "vibes.model.tier": "local",
            "vibes.agent.persona": "explorer",
        },
    },
    {
        "step": "synthesize",
        "duration_ms": 320.0,
        "span_type": "gen_ai.inference.client",
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.request.model": "claude-3-5-sonnet",
            "gen_ai.response.finish_reasons": ["stop"],
            "gen_ai.usage.input_tokens": 6200,
            "gen_ai.usage.output_tokens": 510,
            "server.address": "example.com",
            "vibes.model.tier": "frontier",
            "vibes.agent.persona": "coder",
            "vibes.cegis.rounds": 3,
            "vibes.cegis.converged": True,
        },
    },
    {
        "step": "gate",
        "duration_ms": 45.0,
        "span_type": "gen_ai.execute_tool.internal",
        "attributes": {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "ast_invariant_sentinel",
            "gen_ai.tool.type": "function",
            "vibes.agent.persona": "reviewer",
            "vibes.sentinel.complexity.max": 4,
            "vibes.sentinel.nesting.max": 3,
            "vibes.sentinel.status": "APPROVED",
        },
    },
)

GAP_MS: float = 20.0


def conventional_name(span_type: str, attributes: dict[str, Any]) -> str:
    """Render the span name the convention declares for this type.

    Derived rather than written. A hand-written name agrees with the convention until
    someone changes the model, and `FrontierPlanning` never agreed with it at all.
    """
    rendered = semconv.Convention.load().span_name(span_type, attributes)
    return rendered or span_type


def build_synthetic_agent_session(goal: str) -> AgentTraceSession:
    """Construct an exemplary agent session whose spans follow the GenAI conventions."""
    trace_id = generate_id(16)
    base_time = time.time() * 1000.0
    root_span_id = generate_id(8)
    session = AgentTraceSession(trace_id=trace_id, session_goal=goal)
    convention = semconv.Convention.load()

    spans: list[Span] = []
    cursor = base_time + 10.0
    for entry in SESSION_STEPS:
        attributes = dict(entry["attributes"])
        attributes["gen_ai.conversation.id"] = trace_id
        span_type = str(entry["span_type"])
        spans.append(Span(
            name=convention.span_name(span_type, attributes) or span_type,
            trace_id=trace_id,
            span_id=generate_id(8),
            parent_span_id=root_span_id,
            start_time_ms=cursor,
            end_time_ms=cursor + float(entry["duration_ms"]),
            attributes=attributes,
            status_code=STATUS_OK,
            span_type=span_type,
            kind=str(convention.spans[span_type]["kind"]),
        ))
        cursor += float(entry["duration_ms"]) + GAP_MS

    root_attributes: dict[str, Any] = {
        "gen_ai.operation.name": "invoke_workflow",
        "gen_ai.workflow.name": goal,
        "gen_ai.conversation.id": trace_id,
        "vibes.session.status": "COMPLETED",
    }
    root = Span(
        name=convention.span_name("gen_ai.invoke_workflow.internal", root_attributes) or "invoke_workflow",
        trace_id=trace_id,
        span_id=root_span_id,
        parent_span_id=None,
        start_time_ms=base_time,
        end_time_ms=cursor + 10.0,
        attributes=root_attributes,
        status_code=STATUS_OK,
        span_type="gen_ai.invoke_workflow.internal",
        kind="internal",
    )
    session.spans = [root, *spans]
    return session


def validate_session(session: AgentTraceSession) -> list[tuple[str, semconv.Finding]]:
    """Check every span in a session against the convention, returning `(span name, finding)`.

    Exposed because the claim "this generator is conformant" is one an exhibit should let
    its reader execute rather than read.
    """
    convention = semconv.Convention.load()
    return [
        (span.name, finding)
        for span in session.spans
        for finding in semconv.validate(
            convention, span.span_type, span.name, span.kind, span.attributes
        )
    ]


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
        f"Tokens: Input={tokens['input']}, Output={tokens['output']}, "
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
    if isinstance(val, list):
        # OTLP carries arrays as `arrayValue`. Falling through to `str(val)` exported
        # `gen_ai.response.finish_reasons` as the string "['stop']" — a Python repr in a
        # field a backend parses as text, which no query for a finish reason will match.
        return {"arrayValue": {"values": [_format_otlp_attribute_value(item) for item in val]}}
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
        "kind": SPAN_KINDS.get(span.kind, 1),
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
    parser.add_argument("--validate", action="store_true",
                        help="Check every span against the GenAI semantic conventions")
    parser.add_argument("--otlp", action="store_true", help="Emit trace spans as standard OTLP Protobuf-JSON")
    parser.add_argument(
        "--export-otlp",
        type=str,
        metavar="ENDPOINT",
        help="Stream spans over OTLP/HTTP to collector endpoint (e.g. http://localhost:4318/v1/traces)",
    )
    args = parser.parse_args(argv)

    session = build_synthetic_agent_session(args.goal)

    if args.validate:
        return _print_validation(session)

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


def _print_validation(session: AgentTraceSession) -> int:
    """Report conformance, and exit non-zero only on an error.

    A warning is a real signal and not a failure: OpenTelemetry enums are open, so a
    provider outside the well-known list is permitted. Exiting non-zero on one would train
    its reader to pass `--ignore-warnings`, which is how a gate stops being read at all.
    """
    findings = validate_session(session)
    source = semconv.Convention.load().source
    pinned = ", ".join(f"{f['repo']}@{f['commit'][:12]}" for f in source["files"])
    print(f"Convention snapshot fetched {source['fetched']} from {pinned}")
    for span_name, finding in findings:
        print(f"  [{finding.level}] {span_name}: {finding.message}")
    errors = sum(1 for _, finding in findings if finding.level == semconv.ERROR)
    warnings = len(findings) - errors
    print(f"{len(session.spans)} span(s): {errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
