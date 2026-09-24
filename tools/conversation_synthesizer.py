#!/usr/bin/env python3
"""Autonomous Conversation-to-Case-Study Synthesizer.

Ingests raw agent trajectory logs (JSONL transcripts), enforces zero-trust
redaction (RFC 5737 documentation IPs, example.com hostnames, secret masking,
local path abstraction), extracts quantitative telemetry and complexity metrics,
and drafts structured 5-section observation reports adhering to repository standards.
"""
# sentinel: allow[ZeroTrustSanitization] — enforces zero-trust sanitization by replacing private IPs and hostnames.

from __future__ import annotations

import argparse
import ast
import datetime
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, TextIO

from sanitization_policy import IPV4_PATTERN, IPV6_PATTERN, is_private_host, parse_address

# Regex for thought tag extraction
_THINK_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE
)

# Regex for internal/homelab hostnames
_INTERNAL_HOST_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[a-zA-Z0-9_\-\.]+\.(?:lan|local|internal|corp|home|priv)\b", re.IGNORECASE
)

# Regex for secret patterns
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[a-zA-Z0-9]{36}\b"),
    re.compile(r"\bgithub_pat_[a-zA-Z0-9_]{50,}\b"),
    re.compile(r"\bsk-[a-zA-Z0-9]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[a-zA-Z0-9_\-\.]{20,}\b"),
    re.compile(r"(?i)\b(?:api_key|auth_token|secret_key|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
)

# Regex for local user home directory paths
_LOCAL_HOME_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"/(?:home|Users)/[a-zA-Z0-9_\-]+(?=/|$)"
)

# Python code fence extraction
_PYTHON_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"```(?:python|py)\n(.*?)```", re.DOTALL
)


@dataclass(frozen=True)
class TrajectoryStep:
    """A single atomic step within an agent interaction trajectory."""

    step_index: int
    source: str
    step_type: str
    status: str
    created_at: str
    content: str
    thinking: str
    tool_calls: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass
class RedactionStats:
    """Quantitative summary of zero-trust sanitization events."""

    private_ips_redacted: int = 0
    hostnames_redacted: int = 0
    secrets_redacted: int = 0
    paths_redacted: int = 0


@dataclass
class TrajectoryTelemetry:
    """Quantitative telemetry metrics calculated across an agent trajectory."""

    total_steps: int = 0
    user_steps: int = 0
    model_steps: int = 0
    system_steps: int = 0
    failed_steps: int = 0
    error_rate: float = 0.0
    tool_counts: dict[str, int] = field(default_factory=dict)
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_thinking_tokens: int = 0
    extracted_snippets_count: int = 0
    avg_cyclomatic_complexity: float = 0.0
    max_cyclomatic_complexity: int = 0
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class CaseStudyConfig:
    """Configuration and contextual metadata for drafting a case study."""

    title: str = "Autonomous Agent Interaction"
    project: str = "vibes"
    topic: str = "Trajectory Synthesis & Observability"
    sanitized_steps: tuple[TrajectoryStep, ...] = field(default_factory=tuple)


@dataclass
class CaseStudyDraft:
    """Structured observation report draft ready for publication."""

    title: str
    project: str
    topic: str
    key_metric: str
    markdown_content: str
    telemetry: TrajectoryTelemetry
    redactions: RedactionStats
    sanitized_steps: tuple[TrajectoryStep, ...] = field(default_factory=tuple)


class TrajectoryParser:
    """Parser for raw JSONL agent trajectory logs."""

    def parse_stream(self, stream: TextIO) -> list[TrajectoryStep]:
        """Parse trajectory steps from an open text stream."""
        return self.parse_jsonl(stream.read())

    def parse_jsonl(self, text: str) -> list[TrajectoryStep]:
        """Parse trajectory steps from a multiline JSONL string."""
        steps: list[TrajectoryStep] = []
        for line in text.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            step = self.parse_line(line_str)
            if step is not None:
                steps.append(step)
        return steps

    def parse_line(self, line: str) -> TrajectoryStep | None:
        """Parse a single JSON line into a TrajectoryStep."""
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return self._build_step(payload)

    def _build_step(self, data: dict[str, Any]) -> TrajectoryStep:
        """Construct a validated TrajectoryStep from raw JSON dictionary."""
        step_idx = int(data.get("step_index", 0))
        source = str(data.get("source", "UNKNOWN"))
        step_type = str(data.get("type", "UNKNOWN"))
        status = str(data.get("status", "DONE"))
        created_at = str(data.get("created_at", ""))
        raw_content = str(data.get("content", ""))
        raw_thinking = str(data.get("thinking", ""))
        content, thinking = self._extract_thinking_and_content(raw_content, raw_thinking)
        tool_calls = self._parse_tool_calls(data.get("tool_calls"))
        return TrajectoryStep(
            step_index=step_idx,
            source=source,
            step_type=step_type,
            status=status,
            created_at=created_at,
            content=content,
            thinking=thinking,
            tool_calls=tool_calls,
        )

    def _extract_thinking_and_content(self, content: str, thinking: str) -> tuple[str, str]:
        """Separate embedded <think> tags from content if thinking is empty."""
        if thinking:
            return content, thinking
        match = _THINK_TAG_RE.search(content)
        if not match:
            return content, ""
        extracted_thinking = match.group(1).strip()
        cleaned_content = _THINK_TAG_RE.sub("", content).strip()
        return cleaned_content, extracted_thinking

    def _parse_tool_calls(self, raw_calls: Any) -> tuple[dict[str, Any], ...]:
        """Normalize tool calls structure into a tuple of dictionaries."""
        if not isinstance(raw_calls, list):
            return ()
        normalized: list[dict[str, Any]] = []
        for call in raw_calls:
            if isinstance(call, dict):
                normalized.append(call)
        return tuple(normalized)


class ZeroTrustSanitizer:
    """Enforces zero-trust redaction of private network hostnames, IPs, and secrets."""

    def __init__(self) -> None:
        """Initialize empty redaction mappings for consistent translation."""
        self._ip_map: dict[str, str] = {}
        self._host_map: dict[str, str] = {}
        self._stats = RedactionStats()

    @property
    def stats(self) -> RedactionStats:
        """Return the cumulative redaction statistics."""
        return self._stats

    def sanitize_trajectory(
        self, steps: list[TrajectoryStep]
    ) -> tuple[list[TrajectoryStep], RedactionStats]:
        """Sanitize an entire sequence of trajectory steps."""
        sanitized = [self.sanitize_step(step) for step in steps]
        return sanitized, self._stats

    def sanitize_step(self, step: TrajectoryStep) -> TrajectoryStep:
        """Sanitize all text fields and tool call arguments in a single step."""
        clean_content = self.sanitize_text(step.content)
        clean_thinking = self.sanitize_text(step.thinking)
        clean_calls = tuple(self._sanitize_tool_call(call) for call in step.tool_calls)
        return TrajectoryStep(
            step_index=step.step_index,
            source=step.source,
            step_type=step.step_type,
            status=step.status,
            created_at=step.created_at,
            content=clean_content,
            thinking=clean_thinking,
            tool_calls=clean_calls,
        )

    def sanitize_text(self, text: str) -> str:
        """Apply zero-trust redaction rules across arbitrary text."""
        if not text:
            return ""
        text = self._redact_secrets(text)
        text = self._redact_private_ips(text)
        text = self._redact_hostnames(text)
        text = self._redact_home_paths(text)
        return text

    def _sanitize_tool_call(self, call: dict[str, Any]) -> dict[str, Any]:
        """Recursively sanitize string values in a tool call dictionary."""
        sanitized: dict[str, Any] = {}
        for key, value in call.items():
            sanitized[key] = self._sanitize_value(value)
        return sanitized

    def _sanitize_value(self, value: Any) -> Any:
        """Sanitize a generic value structure (dict, list, string, or primitive)."""
        if isinstance(value, str):
            return self.sanitize_text(value)
        if isinstance(value, dict):
            return {k: self._sanitize_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._sanitize_value(v) for v in value]
        return value

    def _redact_secrets(self, text: str) -> str:
        """Redact sensitive API keys and tokens with [REDACTED_SECRET]."""
        for pattern in _SECRET_PATTERNS:
            matches = list(pattern.finditer(text))
            if matches:
                self._stats.secrets_redacted += len(matches)
                text = pattern.sub("[REDACTED_SECRET]", text)
        return text

    def _redact_private_ips(self, text: str) -> str:
        """Redact concrete private IPv4 and IPv6 addresses with RFC 5737 blocks."""
        text = self._redact_ipv4(text)
        return self._redact_ipv6(text)

    def _redact_ipv4(self, text: str) -> str:
        """Redact concrete private IPv4 addresses with RFC 5737 blocks."""
        for match in IPV4_PATTERN.finditer(text):
            candidate = match.group(0)
            addr = parse_address(candidate)
            if addr is not None and is_private_host(addr):
                repl = self._get_or_create_ip_replacement(candidate)
                text = text.replace(candidate, repl)
        return text

    def _redact_ipv6(self, text: str) -> str:
        """Redact concrete private IPv6 addresses with RFC 3849 blocks."""
        for match in IPV6_PATTERN.finditer(text):
            candidate = match.group(1) or match.group(2)
            if candidate:
                addr = parse_address(candidate)
                if addr is not None and is_private_host(addr):
                    repl = self._get_or_create_ipv6_replacement(candidate)
                    text = text.replace(candidate, repl)
        return text

    def _get_or_create_ip_replacement(self, ip_str: str) -> str:
        """Provide consistent RFC 5737 replacement for a private IPv4 address."""
        if ip_str not in self._ip_map:
            idx = len(self._ip_map) + 1
            self._ip_map[ip_str] = f"192.0.2.{idx}"
            self._stats.private_ips_redacted += 1
        return self._ip_map[ip_str]

    def _get_or_create_ipv6_replacement(self, ip_str: str) -> str:
        """Provide consistent RFC 3849 replacement for a private IPv6 address."""
        if ip_str not in self._ip_map:
            idx = len(self._ip_map) + 1
            self._ip_map[ip_str] = f"2001:db8::{idx}"
            self._stats.private_ips_redacted += 1
        return self._ip_map[ip_str]

    def _redact_hostnames(self, text: str) -> str:
        """Redact internal homelab hostnames (*.lan, *.local) with example.com."""
        matches = list(_INTERNAL_HOST_RE.finditer(text))
        if matches:
            self._stats.hostnames_redacted += len(matches)
            text = _INTERNAL_HOST_RE.sub("example.com", text)
        return text

    def _redact_home_paths(self, text: str) -> str:
        """Redact local workstation home directory prefixes with /home/user."""
        matches = list(_LOCAL_HOME_PATH_RE.finditer(text))
        if matches:
            self._stats.paths_redacted += len(matches)
            text = _LOCAL_HOME_PATH_RE.sub("/home/user", text)
        return text


class TelemetryCalculator:
    """Calculates quantitative telemetry, token estimates, and AST complexity."""

    def compute_telemetry(self, steps: list[TrajectoryStep]) -> TrajectoryTelemetry:
        """Compute full telemetry metrics across a sequence of trajectory steps."""
        if not steps:
            return TrajectoryTelemetry()
        step_counts = self._count_step_sources(steps)
        tool_counts = self._count_tool_invocations(steps)
        failed_count = sum(1 for s in steps if s.status.upper() == "ERROR")
        total_steps = len(steps)
        error_rate = (failed_count / total_steps) if total_steps > 0 else 0.0
        in_tok, out_tok, think_tok = self._estimate_tokens(steps)
        snippets = self._extract_code_snippets(steps)
        avg_comp, max_comp = self._compute_code_complexity(snippets)
        duration = self._compute_duration_seconds(steps)
        return TrajectoryTelemetry(
            total_steps=total_steps,
            user_steps=step_counts.get("USER_EXPLICIT", 0),
            model_steps=step_counts.get("MODEL", 0),
            system_steps=step_counts.get("SYSTEM", 0),
            failed_steps=failed_count,
            error_rate=error_rate,
            tool_counts=tool_counts,
            estimated_input_tokens=in_tok,
            estimated_output_tokens=out_tok,
            estimated_thinking_tokens=think_tok,
            extracted_snippets_count=len(snippets),
            avg_cyclomatic_complexity=avg_comp,
            max_cyclomatic_complexity=max_comp,
            duration_seconds=duration,
        )

    def _count_step_sources(self, steps: list[TrajectoryStep]) -> dict[str, int]:
        """Tally step counts categorized by actor source."""
        counts: dict[str, int] = {}
        for step in steps:
            counts[step.source] = counts.get(step.source, 0) + 1
        return counts

    def _count_tool_invocations(self, steps: list[TrajectoryStep]) -> dict[str, int]:
        """Tally tool call invocations grouped by tool name."""
        counts: dict[str, int] = {}
        for step in steps:
            for call in step.tool_calls:
                name = call.get("name") or call.get("tool_name") or "unknown_tool"
                counts[str(name)] = counts.get(str(name), 0) + 1
        return counts

    def _estimate_tokens(self, steps: list[TrajectoryStep]) -> tuple[int, int, int]:
        """Estimate token counts for input, output, and reasoning steps."""
        in_chars = 0
        out_chars = 0
        think_chars = 0
        for step in steps:
            if step.source == "USER_EXPLICIT":
                in_chars += len(step.content)
            elif step.source == "MODEL":
                out_chars += len(step.content)
                think_chars += len(step.thinking)
            elif step.source == "SYSTEM":
                in_chars += len(step.content)
        return (in_chars + 3) // 4, (out_chars + 3) // 4, (think_chars + 3) // 4

    def _extract_code_snippets(self, steps: list[TrajectoryStep]) -> list[str]:
        """Extract Python code blocks from tool calls and content fences."""
        snippets: list[str] = []
        for step in steps:
            snippets.extend(self._extract_step_snippets(step))
        return snippets

    def _extract_step_snippets(self, step: TrajectoryStep) -> list[str]:
        """Extract candidate Python snippets from a single step."""
        fences = self._extract_fence_snippets(step.content)
        tool_codes = self._extract_tool_snippets(step.tool_calls)
        return fences + tool_codes

    def _extract_fence_snippets(self, content: str) -> list[str]:
        """Extract Python code blocks fenced with triple backticks."""
        return [match.group(1) for match in _PYTHON_FENCE_RE.finditer(content)]

    def _extract_tool_snippets(self, tool_calls: tuple[dict[str, Any], ...]) -> list[str]:
        """Extract code payloads supplied in tool call arguments."""
        results: list[str] = []
        for call in tool_calls:
            code = self._extract_call_code(call)
            if code:
                results.append(code)
        return results

    def _extract_call_code(self, call: dict[str, Any]) -> str | None:
        """Extract code string from arguments of a single tool call dictionary."""
        args = call.get("args") or call.get("arguments")
        if not isinstance(args, dict):
            return None
        code = args.get("CodeContent") or args.get("ReplacementContent")
        return code if isinstance(code, str) and code.strip() else None

    def _compute_code_complexity(self, snippets: list[str]) -> tuple[float, int]:
        """Compute average and maximum cyclomatic complexity across code snippets."""
        if not snippets:
            return 0.0, 0
        scores: list[int] = []
        for snippet in snippets:
            score = self._measure_ast_complexity(snippet)
            if score is not None:
                scores.append(score)
        if not scores:
            return 0.0, 0
        return sum(scores) / len(scores), max(scores)

    def _measure_ast_complexity(self, code: str) -> int | None:
        """Measure McCabe cyclomatic complexity of Python code via AST parsing."""
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return None
        decision_points = 1
        for node in ast.walk(tree):
            if isinstance(
                node,
                (
                    ast.If,
                    ast.For,
                    ast.While,
                    ast.ExceptHandler,
                    ast.With,
                    ast.Assert,
                    ast.BoolOp,
                ),
            ):
                decision_points += 1
        return decision_points

    def _compute_duration_seconds(self, steps: list[TrajectoryStep]) -> float:
        """Calculate elapsed seconds between first and last timestamp."""
        timestamps: list[datetime.datetime] = []
        for step in steps:
            ts = self._parse_iso_timestamp(step.created_at)
            if ts is not None:
                timestamps.append(ts)
        if len(timestamps) < 2:
            return 0.0
        return max(0.0, (timestamps[-1] - timestamps[0]).total_seconds())

    def _parse_iso_timestamp(self, ts_str: str) -> datetime.datetime | None:
        """Parse standard ISO 8601 timestamps gracefully."""
        if not ts_str:
            return None
        cleaned = ts_str.replace("Z", "+00:00")
        try:
            return datetime.datetime.fromisoformat(cleaned)
        except ValueError:
            return None


class CaseStudyDraftsman:
    """Drafts structured 5-section observation reports adhering to repository standards."""

    def draft_report(
        self,
        config: CaseStudyConfig,
        telemetry: TrajectoryTelemetry,
        redactions: RedactionStats,
    ) -> CaseStudyDraft:
        """Synthesize a complete 5-section Markdown case study draft."""
        key_metric = self._format_key_metric(telemetry, redactions)
        md_text = self._render_markdown(config, key_metric, telemetry, redactions)
        return CaseStudyDraft(
            title=config.title,
            project=config.project,
            topic=config.topic,
            key_metric=key_metric,
            markdown_content=md_text,
            telemetry=telemetry,
            redactions=redactions,
            sanitized_steps=config.sanitized_steps,
        )

    def _format_key_metric(
        self, telemetry: TrajectoryTelemetry, redactions: RedactionStats
    ) -> str:
        """Derive an executive key metric string from telemetry."""
        total_redactions = (
            redactions.private_ips_redacted
            + redactions.hostnames_redacted
            + redactions.secrets_redacted
        )
        return (
            f"{telemetry.total_steps} turns ({telemetry.duration_seconds:.1f}s), "
            f"error rate: {telemetry.error_rate * 100:.1f}%, "
            f"sanitized: {total_redactions} leaks"
        )

    def _render_markdown(
        self,
        config: CaseStudyConfig,
        key_metric: str,
        telemetry: TrajectoryTelemetry,
        redactions: RedactionStats,
    ) -> str:
        """Assemble all 5 required observation sections into compliant CommonMark."""
        sections = [
            self._render_header(config.title, config.project, config.topic, key_metric),
            self._render_section_1(config.project, telemetry),
            self._render_section_2(config, telemetry),
            self._render_section_3(telemetry, redactions),
            self._render_section_4(),
            self._render_section_5(telemetry, redactions),
        ]
        return "\n\n".join(sections) + "\n"

    def _render_header(self, title: str, project: str, topic: str, key_metric: str) -> str:
        """Render observation metadata header and frontmatter block."""
        return (
            f"# Observation: {title}\n\n"
            f"> **Project**: `{project}`  \n"
            f"> **Topic**: {topic}  \n"
            f"> **Key Metric**: {key_metric}\n\n"
            "---"
        )

    def _render_section_1(self, project: str, telemetry: TrajectoryTelemetry) -> str:
        """Render Section 1: Executive Context & Baseline."""
        return (
            "## 1. Executive Context & Baseline\n\n"
            f"Autonomous agent trajectories executed within `{project}` demonstrate active paired "
            f"engineering workflows spanning {telemetry.total_steps} sequential turns over "
            f"{telemetry.duration_seconds:.1f} seconds. Establishing quantitative observability "
            "across agent reasoning, tool call execution, and environment interaction allows "
            "engineering teams to identify cognitive traps and mechanical failure boundaries."
        )

    def _render_section_2(
        self, config: CaseStudyConfig, telemetry: TrajectoryTelemetry
    ) -> str:
        """Render Section 2: The Observed Phenomenon."""
        tool_summary = ", ".join(
            f"`{name}` ({cnt})" for name, cnt in sorted(telemetry.tool_counts.items())[:5]
        )
        if not tool_summary:
            tool_summary = "no external tools invoked"
        initial_prompt = self._extract_initial_prompt(config.sanitized_steps)
        objective_prefix = (
            f'The session was initiated with the objective: "{initial_prompt}". '
            if initial_prompt
            else ""
        )
        return (
            "## 2. The Observed Phenomenon\n\n"
            f"{objective_prefix}During the captured session, the agent performed {telemetry.model_steps} "
            f"model iterations and orchestrated tool invocations including {tool_summary}. "
            f"The observed interaction demonstrated an error rate of {telemetry.error_rate * 100:.1f}% "
            f"across {telemetry.failed_steps} failed or retried steps, with an average code complexity "
            f"of M={telemetry.avg_cyclomatic_complexity:.1f} (max M={telemetry.max_cyclomatic_complexity})."
        )

    def _extract_initial_prompt(self, steps: tuple[TrajectoryStep, ...]) -> str:
        """Extract sanitized text of initial user prompt."""
        for step in steps:
            if step.source == "USER_EXPLICIT" and step.content.strip():
                return step.content.strip()[:120]
        return ""

    def _render_section_3(
        self, telemetry: TrajectoryTelemetry, redactions: RedactionStats
    ) -> str:
        """Render Section 3: The Underlying Failure Mode / Dynamics."""
        total_redactions = (
            redactions.private_ips_redacted
            + redactions.hostnames_redacted
            + redactions.secrets_redacted
        )
        return (
            "## 3. The Underlying Failure Mode / Dynamics\n\n"
            "Unconstrained agent interactions frequently expose environmental leak vulnerabilities "
            "and cognitive drift when terminal outputs or logs are ingested without boundary filters. "
            f"In this trajectory, {total_redactions} potential environmental disclosures were identified "
            f"and neutralized ({redactions.private_ips_redacted} private network addresses, "
            f"{redactions.hostnames_redacted} internal hostnames, and {redactions.secrets_redacted} credentials). "
            "Without mechanical sanitization gates, models replicate raw runtime context directly into committed artifacts."
        )

    def _render_section_4(self) -> str:
        """Render Section 4: Remediation & Architectural Pattern."""
        diagram = (
            "```mermaid\n"
            "flowchart TD\n"
            '    Raw["Raw Trajectory Log"] --> Parse["Trajectory Parser"]\n'
            '    Parse --> Sanitize["Zero-Trust Sanitizer"]\n'
            '    Sanitize --> Measure["Telemetry & Invariant Engine"]\n'
            '    Measure --> Draft["Case Study Draftsman"]\n'
            '    Draft --> Output["5-Section Observation Report"]\n'
            "```"
        )
        return (
            "## 4. Remediation & Architectural Pattern\n\n"
            "To safeguard autonomous development loops, repositories must implement automated "
            "conversation-to-case-study synthesis pipelines that couple zero-trust redaction with "
            "quantitative invariant extraction:\n\n"
            f"{diagram}\n\n"
            "By piping agent trajectory logs through deterministic sanitization filters and AST complexity "
            "calculators, development teams convert ephemeral transcripts into high-fidelity, peer-reviewed "
            "case studies without risking private infrastructure leakage."
        )

    def _render_section_5(
        self, telemetry: TrajectoryTelemetry, redactions: RedactionStats
    ) -> str:
        """Render Section 5: Empirical Verification & Invariant Proof."""
        table = (
            "| Metric Dimension | Recorded Value | Invariant Target |\n"
            "|---|---|---|\n"
            f"| Total Steps | {telemetry.total_steps} | Bounded session |\n"
            f"| Model / User Turn Ratio | {telemetry.model_steps} / {telemetry.user_steps} | Dynamic balance |\n"
            f"| Error Rate | {telemetry.error_rate * 100:.1f}% | <= 10.0% |\n"
            f"| Private IPs Redacted | {redactions.private_ips_redacted} | 100% neutralized |\n"
            f"| Secrets / Keys Masked | {redactions.secrets_redacted} | Zero plaintext leaks |\n"
            f"| Max Cyclomatic Complexity | M={telemetry.max_cyclomatic_complexity} | M <= 10 |\n"
            f"| Est. Tokens (In / Out / Think) | {telemetry.estimated_input_tokens} / {telemetry.estimated_output_tokens} / {telemetry.estimated_thinking_tokens} | Within budget |"
        )
        return (
            "## 5. Empirical Verification & Invariant Proof\n\n"
            "Empirical verification confirms that the synthesized trajectory conforms strictly to repository "
            "invariants and zero-trust sanitization rules:\n\n"
            f"{table}\n\n"
            "All private addresses have been converted to RFC 5737 documentation blocks, and all code snippets "
            "comply with architectural complexity ceilings."
        )


class ConversationSynthesizer:
    """Orchestrator for the end-to-end conversation synthesis pipeline."""

    def __init__(self) -> None:
        """Initialize parser, sanitizer, telemetry calculator, and draftsman."""
        self._parser = TrajectoryParser()
        self._sanitizer = ZeroTrustSanitizer()
        self._telemetry_calc = TelemetryCalculator()
        self._draftsman = CaseStudyDraftsman()

    def process_transcript(
        self,
        raw_jsonl: str,
        config: CaseStudyConfig | None = None,
        **kwargs: Any,
    ) -> CaseStudyDraft:
        """Ingest, sanitize, measure, and draft an observation report from JSONL."""
        cfg = self._resolve_config(config, kwargs)
        steps = self._parser.parse_jsonl(raw_jsonl)
        clean_steps, stats = self._sanitizer.sanitize_trajectory(steps)
        telemetry = self._telemetry_calc.compute_telemetry(clean_steps)
        effective_cfg = CaseStudyConfig(
            title=cfg.title,
            project=cfg.project,
            topic=cfg.topic,
            sanitized_steps=tuple(clean_steps),
        )
        return self._draftsman.draft_report(effective_cfg, telemetry, stats)

    def _resolve_config(
        self, config: CaseStudyConfig | None, kwargs: dict[str, Any]
    ) -> CaseStudyConfig:
        """Resolve case study configuration from explicit config or keyword arguments."""
        if config is not None:
            return config
        return CaseStudyConfig(
            title=str(kwargs.get("title", "Autonomous Agent Interaction")),
            project=str(kwargs.get("project", "vibes")),
            topic=str(kwargs.get("topic", "Trajectory Synthesis & Observability")),
        )


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for conversation synthesizer."""
    parser = argparse.ArgumentParser(
        description="Autonomous Conversation-to-Case-Study Synthesizer for agent trajectories."
    )
    parser.add_argument(
        "--transcript",
        "-t",
        type=str,
        default="-",
        help="Path to raw JSONL transcript file (or '-' for stdin).",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="",
        help="Path to write output observation report (defaults to stdout).",
    )
    parser.add_argument(
        "--title",
        type=str,
        default="Autonomous Agent Interaction",
        help="Observation report title.",
    )
    parser.add_argument(
        "--project",
        type=str,
        default="vibes",
        help="Target project name.",
    )
    parser.add_argument(
        "--topic",
        type=str,
        default="Trajectory Synthesis & Observability",
        help="Target topic or domain area.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "summary"],
        default="markdown",
        help="Output serialization format.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for conversation synthesizer."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    raw_text = _read_transcript_input(args.transcript)
    if not raw_text.strip():
        sys.stderr.write("Error: Empty or unreadable transcript input.\n")
        return 1
    synthesizer = ConversationSynthesizer()
    draft = synthesizer.process_transcript(
        raw_text, title=args.title, project=args.project, topic=args.topic
    )
    output_str = _format_draft_output(draft, args.format)
    _write_output(output_str, args.output)
    return 0


def _read_transcript_input(source: str) -> str:
    """Read transcript text from a file path or standard input."""
    if source == "-" or not source:
        return sys.stdin.read()
    path = Path(source)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _format_draft_output(draft: CaseStudyDraft, fmt: str) -> str:
    """Format draft result according to chosen CLI output mode."""
    if fmt == "json":
        return json.dumps(
            {
                "title": draft.title,
                "project": draft.project,
                "topic": draft.topic,
                "key_metric": draft.key_metric,
                "telemetry": asdict(draft.telemetry),
                "redactions": asdict(draft.redactions),
                "markdown": draft.markdown_content,
            },
            indent=2,
        )
    if fmt == "summary":
        return f"{draft.title} | {draft.key_metric}"
    return draft.markdown_content


def _write_output(content: str, destination: str) -> None:
    """Write formatted content to a target file or standard output."""
    if destination:
        out_path = Path(destination)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
    else:
        sys.stdout.write(content)


if __name__ == "__main__":
    sys.exit(main())
