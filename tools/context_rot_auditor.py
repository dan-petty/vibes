#!/usr/bin/env python3
"""Attention Dilution & Context Rot Auditor with Multi-Scale Active Compactor.

Provides automated inspection of long-horizon AI agent transcripts and prompt payloads
to detect context rot, attention dilution, lost-in-the-middle degradation, and observation
bloat. Features active context compaction: observation masking, traceback deduplication,
and invariant envelope re-pinning to restore high signal-to-noise density.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

AUDITOR_VERSION: Final[str] = "v1.0.0"

# Heuristic token estimation ratio for source and markdown text
CHARS_PER_TOKEN: Final[float] = 3.8
INVARIANT_KEYWORDS: Final[tuple[str, ...]] = (
    "invariant",
    "constraint",
    "mccabe",
    "cyclomatic",
    "complexity",
    "depth",
    "zero-trust",
    "rfc 1918",
    "assertion",
    "tuple equality",
    "no private ip",
)

SARIF_RULES: Final[dict[str, dict[str, Any]]] = {
    "ROT001": {
        "id": "ROT001",
        "name": "ContextDepthExceeded",
        "shortDescription": {"text": "Total context token depth exceeds effective retention horizon."},
        "defaultConfiguration": {"level": "warning"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "ROT002": {
        "id": "ROT002",
        "name": "LostInTheMiddleInvariant",
        "shortDescription": {"text": "Critical invariant constraint trapped in the lost-in-the-middle valley."},
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "ROT003": {
        "id": "ROT003",
        "name": "ObservationBloat",
        "shortDescription": {"text": "Single tool observation or command output consumes excessive token budget."},
        "defaultConfiguration": {"level": "warning"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "ROT004": {
        "id": "ROT004",
        "name": "RepetitiveErrorChatter",
        "shortDescription": {"text": "Redundant failure tracebacks repeating without active pruning."},
        "defaultConfiguration": {"level": "warning"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
    "ROT005": {
        "id": "ROT005",
        "name": "SignalToNoiseDeficit",
        "shortDescription": {"text": "Ratio of actionable instruction tokens to raw chatter is critically low."},
        "defaultConfiguration": {"level": "error"},
        "helpUri": "https://github.com/dan-petty/vibes",
    },
}


class MessageRole(StrEnum):
    """Role classification for transcript messages."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    OBSERVATION = "observation"


class RotSeverity(StrEnum):
    """Severity classification of detected context rot issues."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class AuditorPreset(StrEnum):
    """Presets configuring sensitivity thresholds for context rot detection."""

    STANDARD = "standard"
    STRICT = "strict"
    PEDANTIC = "pedantic"


@dataclass(frozen=True)
class RotThresholds:
    """Numerical boundaries for context rot detection."""

    max_effective_tokens: int
    valley_start_ratio: float
    valley_end_ratio: float
    max_observation_tokens: int
    min_signal_to_noise_ratio: float
    max_observation_share: float


PRESET_THRESHOLDS: Final[dict[AuditorPreset, RotThresholds]] = {
    AuditorPreset.STANDARD: RotThresholds(
        max_effective_tokens=64000,
        valley_start_ratio=0.25,
        valley_end_ratio=0.75,
        max_observation_tokens=4000,
        min_signal_to_noise_ratio=0.10,
        max_observation_share=0.20,
    ),
    AuditorPreset.STRICT: RotThresholds(
        max_effective_tokens=32000,
        valley_start_ratio=0.20,
        valley_end_ratio=0.80,
        max_observation_tokens=2500,
        min_signal_to_noise_ratio=0.15,
        max_observation_share=0.15,
    ),
    AuditorPreset.PEDANTIC: RotThresholds(
        max_effective_tokens=16000,
        valley_start_ratio=0.15,
        valley_end_ratio=0.85,
        max_observation_tokens=1200,
        min_signal_to_noise_ratio=0.25,
        max_observation_share=0.10,
    ),
}


@dataclass(frozen=True)
class ContextMessage:
    """Structured representation of a single message in a transcript or context window."""

    role: MessageRole
    content: str
    token_count: int
    turn_index: int
    position_ratio: float = 0.0
    is_invariant_bearing: bool = False
    is_tool_observation: bool = False


@dataclass(frozen=True)
class RotFinding:
    """Finding identifying a context rot or attention dilution hazard."""

    rule_id: str
    severity: RotSeverity
    message: str
    location: str
    line_number: int
    token_depth: int
    recommendation: str
    finding_hash: str


@dataclass(frozen=True)
class CompactedContext:
    """Outcome of active context compaction."""

    original_token_count: int
    compacted_token_count: int
    compression_ratio: float
    masked_observations_count: int
    deduplicated_errors_count: int
    pinned_invariants_count: int
    compacted_messages: tuple[ContextMessage, ...]


@dataclass(frozen=True)
class AuditSummary:
    """Comprehensive summary of context rot inspection and compaction."""

    total_messages: int
    total_tokens: int
    attention_dilution_index: float
    effective_context_ratio: float
    findings: tuple[RotFinding, ...]
    compacted: CompactedContext | None


def _compute_hash(content: str) -> str:
    """Compute hexadecimal SHA-256 hash of text."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Estimate token count based on standard character-to-token ratio."""
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def _has_invariant_content(text: str) -> bool:
    """Return True if message content contains architectural invariant keywords."""
    lowered = text.lower()
    return any(kw in lowered for kw in INVARIANT_KEYWORDS)


def _is_tool_observation(role: MessageRole, text: str) -> bool:
    """Return True if message represents a tool or command execution output."""
    if role in (MessageRole.TOOL, MessageRole.OBSERVATION):
        return True
    lowered = text.lower()
    return "stdout:" in lowered or "command exited" in lowered or "tool output" in lowered


def _extract_content(data: dict[str, Any]) -> str:
    """Extract content string from multiple dictionary candidate keys."""
    for key in ("content", "text", "message"):
        val = data.get(key)
        if val:
            return str(val)
    return ""


def _resolve_role_from_string(raw_role: str) -> MessageRole:
    """Map string role indicator to MessageRole enum."""
    lowered = raw_role.lower()
    if "system" in lowered:
        return MessageRole.SYSTEM
    if any(k in lowered for k in ("assistant", "model")):
        return MessageRole.ASSISTANT
    if any(k in lowered for k in ("tool", "observation")):
        return MessageRole.TOOL
    return MessageRole.USER


def _parse_dict_to_message(data: dict[str, Any], turn_idx: int) -> ContextMessage | None:
    """Construct ContextMessage from dictionary keys."""
    content = _extract_content(data)
    if not content:
        return None
    raw_role = str(data.get("role") or data.get("source") or "user")
    role = _resolve_role_from_string(raw_role)
    tokens = estimate_tokens(content)
    is_inv = _has_invariant_content(content)
    is_obs = _is_tool_observation(role, content)
    return ContextMessage(
        role=role,
        content=content,
        token_count=tokens,
        turn_index=turn_idx,
        is_invariant_bearing=is_inv,
        is_tool_observation=is_obs,
    )


def _parse_json_items(items: list[Any]) -> list[ContextMessage]:
    """Parse a list of decoded JSON objects into ContextMessages."""
    dicts = [item for item in items if isinstance(item, dict)]
    parsed = [_parse_dict_to_message(d, idx) for idx, d in enumerate(dicts)]
    valid = [msg for msg in parsed if msg is not None]
    return _assign_position_ratios(valid)


def _try_parse_json_array(raw_stripped: str) -> list[ContextMessage] | None:
    """Attempt parsing raw text as a JSON array."""
    if not (raw_stripped.startswith("[") and raw_stripped.endswith("]")):
        return None
    try:
        items = json.loads(raw_stripped)
        return _parse_json_items(items)
    except json.JSONDecodeError:
        return None


def _try_parse_jsonl_line(line: str, idx: int) -> ContextMessage | None:
    """Attempt parsing a single JSONL line."""
    if not (line.startswith("{") and line.endswith("}")):
        return None
    try:
        data = json.loads(line)
        return _parse_dict_to_message(data, idx)
    except json.JSONDecodeError:
        return None


def _try_parse_jsonl(lines: Sequence[str]) -> list[ContextMessage]:
    """Parse lines as JSONL stream."""
    messages: list[ContextMessage] = []
    for idx, line in enumerate(lines):
        msg = _try_parse_jsonl_line(line, idx)
        if msg is not None:
            messages.append(msg)
    return messages


def parse_transcript_text(raw_text: str) -> list[ContextMessage]:
    """Parse raw text, JSONL, or JSON array into a sequence of ContextMessages."""
    raw_stripped = raw_text.strip()
    if not raw_stripped:
        return []

    json_array_result = _try_parse_json_array(raw_stripped)
    if json_array_result is not None:
        return json_array_result

    lines = [ln.strip() for ln in raw_stripped.splitlines() if ln.strip()]
    jsonl_messages = _try_parse_jsonl(lines)
    if jsonl_messages:
        return _assign_position_ratios(jsonl_messages)

    return _parse_plaintext_turns(raw_text)


def _resolve_plaintext_role(line: str) -> MessageRole:
    """Determine message role from plaintext line prefix."""
    lowered = line.lower()
    if lowered.startswith("system:"):
        return MessageRole.SYSTEM
    if lowered.startswith("assistant:"):
        return MessageRole.ASSISTANT
    if lowered.startswith("tool:") or lowered.startswith("observation:"):
        return MessageRole.TOOL
    return MessageRole.USER


def _parse_single_turn_block(block: str, idx: int) -> ContextMessage | None:
    """Parse a single plaintext turn block into ContextMessage."""
    b_strip = block.strip()
    if not b_strip:
        return None
    role = _resolve_plaintext_role(b_strip)
    tokens = estimate_tokens(b_strip)
    is_inv = _has_invariant_content(b_strip)
    is_obs = _is_tool_observation(role, b_strip)
    return ContextMessage(
        role=role,
        content=b_strip,
        token_count=tokens,
        turn_index=idx,
        is_invariant_bearing=is_inv,
        is_tool_observation=is_obs,
    )


def _parse_plaintext_turns(raw_text: str) -> list[ContextMessage]:
    """Parse plaintext turns separated by blocks."""
    blocks = re.split(r"\n(?=[A-Za-z]+:\s)", raw_text.strip())
    parsed = [_parse_single_turn_block(b, idx) for idx, b in enumerate(blocks)]
    valid = [msg for msg in parsed if msg is not None]
    return _assign_position_ratios(valid)


def _assign_position_ratios(messages: Sequence[ContextMessage]) -> list[ContextMessage]:
    """Calculate cumulative token position ratio (0.0 to 1.0) for each message."""
    total_tokens = sum(m.token_count for m in messages)
    if total_tokens <= 0:
        return list(messages)

    current_tokens = 0
    assigned: list[ContextMessage] = []
    for m in messages:
        pos_ratio = current_tokens / total_tokens
        assigned.append(
            ContextMessage(
                role=m.role,
                content=m.content,
                token_count=m.token_count,
                turn_index=m.turn_index,
                position_ratio=round(pos_ratio, 4),
                is_invariant_bearing=m.is_invariant_bearing,
                is_tool_observation=m.is_tool_observation,
            )
        )
        current_tokens += m.token_count
    return assigned


@dataclass(frozen=True)
class _AuditParams:
    """Internal parameters container for transcript audit."""

    total_tokens: int
    thresholds: RotThresholds
    filename: str


def _check_rot001_depth(
    total_tokens: int, max_tokens: int, filename: str
) -> RotFinding | None:
    """Check ROT001: Context token depth exceeding effective horizon."""
    if total_tokens <= max_tokens:
        return None
    h = _compute_hash(f"ROT001:{filename}:{total_tokens}")
    return RotFinding(
        rule_id="ROT001",
        severity=RotSeverity.HIGH,
        message=f"Context token depth {total_tokens} exceeds effective horizon {max_tokens}",
        location=f"{filename}:total",
        line_number=1,
        token_depth=total_tokens,
        recommendation="Compact context by observation masking or extractive summarization",
        finding_hash=h,
    )


def _check_rot002_valley(
    msg: ContextMessage, params: _AuditParams
) -> RotFinding | None:
    """Check ROT002: Invariant trapped in lost-in-the-middle valley."""
    if not msg.is_invariant_bearing:
        return None
    t = params.thresholds
    if not (t.valley_start_ratio <= msg.position_ratio <= t.valley_end_ratio):
        return None
    loc = f"{params.filename}:turn_{msg.turn_index}"
    h = _compute_hash(f"ROT002:{loc}:{msg.position_ratio}")
    return RotFinding(
        rule_id="ROT002",
        severity=RotSeverity.CRITICAL,
        message=f"Invariant constraint trapped in lost-in-the-middle valley at {int(msg.position_ratio * 100)}% depth",
        location=loc,
        line_number=msg.turn_index + 1,
        token_depth=msg.token_count,
        recommendation="Pin invariant envelope to prompt suffix or immediate active frame",
        finding_hash=h,
    )


def _check_rot003_bloat(
    msg: ContextMessage, params: _AuditParams
) -> RotFinding | None:
    """Check ROT003: Tool observation bloat."""
    if not msg.is_tool_observation:
        return None
    t = params.thresholds
    tot = params.total_tokens
    share = msg.token_count / tot if tot > 0 else 0.0
    if msg.token_count <= t.max_observation_tokens and share <= t.max_observation_share:
        return None
    loc = f"{params.filename}:turn_{msg.turn_index}"
    h = _compute_hash(f"ROT003:{loc}:{msg.token_count}")
    return RotFinding(
        rule_id="ROT003",
        severity=RotSeverity.MEDIUM,
        message=f"Observation bloat: tool output contains {msg.token_count} tokens ({int(share * 100)}% of context)",
        location=loc,
        line_number=msg.turn_index + 1,
        token_depth=msg.token_count,
        recommendation="Apply observation masking to collapse verbose stdout/stderr",
        finding_hash=h,
    )


def _check_rot004_repetition(
    msg: ContextMessage, seen_errors: set[str], filename: str
) -> RotFinding | None:
    """Check ROT004: Repetitive error traceback chatter."""
    lowered = msg.content.lower()
    if "traceback" not in lowered and "error:" not in lowered:
        return None
    err_h = _compute_hash(msg.content[:300])
    if err_h not in seen_errors:
        seen_errors.add(err_h)
        return None
    loc = f"{filename}:turn_{msg.turn_index}"
    h = _compute_hash(f"ROT004:{loc}:{err_h}")
    return RotFinding(
        rule_id="ROT004",
        severity=RotSeverity.LOW,
        message=f"Repetitive traceback chatter detected at turn {msg.turn_index}",
        location=loc,
        line_number=msg.turn_index + 1,
        token_depth=msg.token_count,
        recommendation="Deduplicate failure tracebacks into a single structured signature",
        finding_hash=h,
    )


def _check_rot005_deficit(
    signal_tokens: int, total_tokens: int, min_ratio: float, filename: str
) -> RotFinding | None:
    """Check ROT005: Signal-to-noise deficit."""
    effective_ratio = signal_tokens / total_tokens if total_tokens > 0 else 1.0
    if effective_ratio >= min_ratio or total_tokens <= 2000:
        return None
    h = _compute_hash(f"ROT005:{filename}:{effective_ratio:.4f}")
    return RotFinding(
        rule_id="ROT005",
        severity=RotSeverity.HIGH,
        message=f"Signal-to-noise deficit: invariant density {effective_ratio * 100:.1f}% below minimum {min_ratio * 100:.1f}%",
        location=f"{filename}:ratio",
        line_number=1,
        token_depth=total_tokens,
        recommendation="Filter conversational chatter and prune repetitive tool execution blocks",
        finding_hash=h,
    )


def _evaluate_message_rot(
    m: ContextMessage, params: _AuditParams, seen_errors: set[str]
) -> list[RotFinding]:
    """Evaluate rot rules ROT002, ROT003, and ROT004 for a single message."""
    findings: list[RotFinding] = []
    f2 = _check_rot002_valley(m, params)
    if f2 is not None:
        findings.append(f2)
    f3 = _check_rot003_bloat(m, params)
    if f3 is not None:
        findings.append(f3)
    f4 = _check_rot004_repetition(m, seen_errors, params.filename)
    if f4 is not None:
        findings.append(f4)
    return findings


class ContextRotAuditor:
    """Audits sequences of ContextMessages for context rot and attention dilution risks."""

    def __init__(self, thresholds: RotThresholds | None = None) -> None:
        """Initialize auditor with sensitivity thresholds."""
        self.thresholds = thresholds or PRESET_THRESHOLDS[AuditorPreset.STRICT]

    def _scan_message_findings(
        self,
        messages: Sequence[ContextMessage],
        params: _AuditParams,
    ) -> tuple[list[RotFinding], int, int]:
        """Scan messages for per-message findings and accumulate token counts."""
        findings: list[RotFinding] = []
        seen_error_hashes: set[str] = set()
        signal_tokens = 0
        noise_tokens = 0

        for m in messages:
            signal_tokens += m.token_count if m.is_invariant_bearing else 0
            noise_tokens += m.token_count if m.is_tool_observation else 0
            findings.extend(_evaluate_message_rot(m, params, seen_error_hashes))

        return findings, signal_tokens, noise_tokens

    def audit(self, messages: Sequence[ContextMessage], filename: str = "transcript.jsonl") -> AuditSummary:
        """Audit message sequence and return AuditSummary."""
        total_tokens = sum(m.token_count for m in messages)
        if not messages:
            return AuditSummary(0, 0, 0.0, 1.0, (), None)

        findings: list[RotFinding] = []
        f1 = _check_rot001_depth(total_tokens, self.thresholds.max_effective_tokens, filename)
        if f1 is not None:
            findings.append(f1)

        params = _AuditParams(total_tokens, self.thresholds, filename)
        msg_findings, signal, noise = self._scan_message_findings(messages, params)
        findings.extend(msg_findings)

        f5 = _check_rot005_deficit(signal, total_tokens, self.thresholds.min_signal_to_noise_ratio, filename)
        if f5 is not None:
            findings.append(f5)

        effective_ratio = signal / total_tokens if total_tokens > 0 else 1.0
        adi = (noise / max(1, signal)) * (total_tokens / 10000.0)
        return AuditSummary(
            total_messages=len(messages),
            total_tokens=total_tokens,
            attention_dilution_index=round(adi, 2),
            effective_context_ratio=round(effective_ratio, 4),
            findings=tuple(findings),
            compacted=None,
        )


class ActiveContextCompactor:
    """Performs multi-scale context compaction and invariant re-pinning."""

    def __init__(self, max_observation_lines: int = 10) -> None:
        """Initialize compactor with maximum line ceiling for tool observations."""
        self.max_observation_lines = max_observation_lines

    def _compact_observation(self, m: ContextMessage) -> ContextMessage:
        """Mask tool observation content."""
        masked_content = self._mask_observation(m.content)
        new_tokens = estimate_tokens(masked_content)
        return ContextMessage(
            role=m.role,
            content=masked_content,
            token_count=new_tokens,
            turn_index=m.turn_index,
            is_invariant_bearing=m.is_invariant_bearing,
            is_tool_observation=True,
        )

    def _suppress_traceback(self, m: ContextMessage, err_hash: str) -> ContextMessage:
        """Create suppressed placeholder for repeated traceback."""
        return ContextMessage(
            role=m.role,
            content=f"[Repeated traceback suppressed: {err_hash[:8]}]",
            token_count=10,
            turn_index=m.turn_index,
            is_invariant_bearing=False,
            is_tool_observation=m.is_tool_observation,
        )

    def _compact_message_item(
        self,
        m: ContextMessage,
        seen_errors: set[str],
    ) -> tuple[ContextMessage, bool, bool]:
        """Compact a single message, returning (message, was_masked, was_deduped)."""
        if m.is_tool_observation and m.token_count > 300:
            return self._compact_observation(m), True, False

        if "traceback" in m.content.lower():
            err_h = _compute_hash(m.content[:300])
            if err_h in seen_errors:
                return self._suppress_traceback(m, err_h), False, True
            seen_errors.add(err_h)

        return m, False, False

    def _build_pinned_envelope(self, pinned_invariants: Sequence[str], next_turn: int) -> ContextMessage:
        """Build suffix invariant envelope ContextMessage."""
        pinned_text = "### 🛡️ ANCHORED INVARIANT ENVELOPE (PINNED)\n" + "\n".join(
            f"- {inv.strip()}" for inv in pinned_invariants[-3:]
        )
        return ContextMessage(
            role=MessageRole.SYSTEM,
            content=pinned_text,
            token_count=estimate_tokens(pinned_text),
            turn_index=next_turn,
            is_invariant_bearing=True,
            is_tool_observation=False,
        )

    def _process_compaction_stream(
        self,
        messages: Sequence[ContextMessage],
    ) -> tuple[list[ContextMessage], list[str], int, int]:
        """Process messages stream to produce compacted messages, pinned invariants, and counts."""
        compacted: list[ContextMessage] = []
        pinned_invariants: list[str] = []
        seen_errors: set[str] = set()
        masked_count = 0
        dedup_count = 0

        for m in messages:
            if m.is_invariant_bearing and m.role != MessageRole.SYSTEM:
                pinned_invariants.append(m.content)

            item, was_masked, was_deduped = self._compact_message_item(m, seen_errors)
            compacted.append(item)
            masked_count += int(was_masked)
            dedup_count += int(was_deduped)

        return compacted, pinned_invariants, masked_count, dedup_count

    def compact(self, messages: Sequence[ContextMessage]) -> CompactedContext:
        """Compact message sequence: mask observations, deduplicate errors, and pin invariants."""
        original_tokens = sum(m.token_count for m in messages)
        compacted, pinned, masked_cnt, dedup_cnt = self._process_compaction_stream(messages)

        if pinned:
            compacted.append(self._build_pinned_envelope(pinned, len(compacted)))

        final_assigned = _assign_position_ratios(compacted)
        compacted_tokens = sum(m.token_count for m in final_assigned)
        ratio = (compacted_tokens / original_tokens) if original_tokens > 0 else 1.0

        return CompactedContext(
            original_token_count=original_tokens,
            compacted_token_count=compacted_tokens,
            compression_ratio=round(ratio, 4),
            masked_observations_count=masked_cnt,
            deduplicated_errors_count=dedup_cnt,
            pinned_invariants_count=len(pinned),
            compacted_messages=tuple(final_assigned),
        )

    def _mask_observation(self, content: str) -> str:
        """Mask a verbose observation into an abbreviated digest."""
        lines = content.splitlines()
        if len(lines) <= self.max_observation_lines:
            return content
        head = lines[:3]
        tail = lines[-3:]
        omitted = len(lines) - 6
        return (
            "\n".join(head)
            + f"\n... [{omitted} lines omitted by ActiveContextCompactor] ...\n"
            + "\n".join(tail)
        )


def export_sarif(summary: AuditSummary, target_uri: str) -> dict[str, Any]:
    """Export context rot findings as schema-valid OASIS SARIF 2.1.0 dictionary."""
    results: list[dict[str, Any]] = []
    for f in summary.findings:
        rule_def = SARIF_RULES.get(f.rule_id, {
            "id": f.rule_id,
            "name": "ContextRotFinding",
            "shortDescription": {"text": f.message},
            "defaultConfiguration": {"level": "warning"},
        })
        results.append({
            "ruleId": f.rule_id,
            "level": rule_def["defaultConfiguration"]["level"],
            "message": {"text": f.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": target_uri},
                        "region": {"startLine": max(1, f.line_number)},
                    }
                }
            ],
            "properties": {
                "severity": f.severity.value,
                "tokenDepth": f.token_depth,
                "recommendation": f.recommendation,
                "findingHash": f.finding_hash,
            },
        })

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "vibes-context-rot-auditor",
                        "informationUri": "https://github.com/dan-petty/vibes",
                        "version": AUDITOR_VERSION,
                        "rules": list(SARIF_RULES.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def export_json(summary: AuditSummary) -> str:
    """Export audit summary and telemetry as JSON string."""
    data = {
        "auditor_version": AUDITOR_VERSION,
        "total_messages": summary.total_messages,
        "total_tokens": summary.total_tokens,
        "attention_dilution_index": summary.attention_dilution_index,
        "effective_context_ratio": summary.effective_context_ratio,
        "findings_count": len(summary.findings),
        "findings": [
            {
                "rule_id": f.rule_id,
                "severity": f.severity.value,
                "message": f.message,
                "location": f.location,
                "line_number": f.line_number,
                "token_depth": f.token_depth,
                "recommendation": f.recommendation,
            }
            for f in summary.findings
        ],
    }
    if summary.compacted is not None:
        data["compacted"] = {
            "original_tokens": summary.compacted.original_token_count,
            "compacted_tokens": summary.compacted.compacted_token_count,
            "compression_ratio": summary.compacted.compression_ratio,
            "masked_observations": summary.compacted.masked_observations_count,
            "deduplicated_errors": summary.compacted.deduplicated_errors_count,
            "pinned_invariants": summary.compacted.pinned_invariants_count,
        }
    return json.dumps(data, indent=2)


def format_markdown_report(summary: AuditSummary) -> str:
    """Format human-readable Markdown summary report."""
    lines = [
        "# Attention Dilution & Context Rot Audit Report",
        "",
        f"- **Total Messages**: {summary.total_messages}",
        f"- **Total Token Depth**: {summary.total_tokens}",
        f"- **Attention Dilution Index (ADI)**: `{summary.attention_dilution_index}`",
        f"- **Effective Invariant Ratio**: `{summary.effective_context_ratio * 100:.1f}%`",
        f"- **Total Findings**: {len(summary.findings)}",
        "",
    ]
    if summary.compacted is not None:
        c = summary.compacted
        lines.extend([
            "## Active Context Compaction Summary",
            "",
            f"- **Compacted Tokens**: {c.compacted_token_count} (Saved {c.original_token_count - c.compacted_token_count} tokens)",
            f"- **Compression Ratio**: `{c.compression_ratio * 100:.1f}%`",
            f"- **Masked Observations**: {c.masked_observations_count}",
            f"- **Deduplicated Errors**: {c.deduplicated_errors_count}",
            f"- **Re-Pinned Invariant Anchors**: {c.pinned_invariants_count}",
            "",
        ])

    if not summary.findings:
        lines.append("✓ **Zero context rot hazards detected.** Signal-to-noise density optimal.\n")
        return "\n".join(lines)

    lines.extend([
        "## Rot & Attention Dilution Findings",
        "",
        "| Rule | Severity | Location | Finding | Recommendation |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])
    for f in summary.findings:
        lines.append(
            f"| `{f.rule_id}` | **{f.severity.value.upper()}** | `{f.location}` | {f.message} | {f.recommendation} |"
        )
    lines.append("")
    return "\n".join(lines)


def _build_argument_parser() -> argparse.ArgumentParser:
    """Construct command line interface argument parser."""
    parser = argparse.ArgumentParser(
        prog="context_rot_auditor.py",
        description="Attention Dilution & Context Rot Auditor with Active Compactor.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Path to transcript (JSONL, JSON, or text) to audit.",
    )
    parser.add_argument(
        "--preset",
        choices=[p.value for p in AuditorPreset],
        default=AuditorPreset.STRICT.value,
        help="Sensitivity preset (default: strict).",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Execute active compaction and print compacted telemetry.",
    )
    parser.add_argument(
        "--export-sarif",
        metavar="PATH",
        help="Export findings as SARIF 2.1.0 to PATH.",
    )
    parser.add_argument(
        "--export-json",
        metavar="PATH",
        help="Export audit summary as JSON to PATH.",
    )
    parser.add_argument(
        "--export-md",
        metavar="PATH",
        help="Export Markdown report to PATH.",
    )
    return parser


def _handle_exports(summary: AuditSummary, target_path: str, args: argparse.Namespace) -> None:
    """Export audit summary to requested formats."""
    if args.export_sarif:
        sarif_data = export_sarif(summary, target_path)
        Path(args.export_sarif).write_text(json.dumps(sarif_data, indent=2), encoding="utf-8")

    if args.export_json:
        json_data = export_json(summary)
        Path(args.export_json).write_text(json_data, encoding="utf-8")

    if args.export_md:
        md_data = format_markdown_report(summary)
        Path(args.export_md).write_text(md_data, encoding="utf-8")


def _run_cli_audit(messages: Sequence[ContextMessage], target_path: str, args: argparse.Namespace) -> AuditSummary:
    """Run auditor and optional compaction."""
    preset = AuditorPreset(args.preset)
    thresholds = PRESET_THRESHOLDS[preset]
    auditor = ContextRotAuditor(thresholds)
    summary = auditor.audit(messages, target_path)

    if not args.compact:
        return summary

    compactor = ActiveContextCompactor()
    compacted = compactor.compact(messages)
    return AuditSummary(
        total_messages=summary.total_messages,
        total_tokens=summary.total_tokens,
        attention_dilution_index=summary.attention_dilution_index,
        effective_context_ratio=summary.effective_context_ratio,
        findings=summary.findings,
        compacted=compacted,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Execute main CLI routine."""
    parser = _build_argument_parser()
    args = parser.parse_args(argv)

    if not args.target:
        print("No target transcript specified. Use --help for usage.", file=sys.stderr)
        return 1

    target_path = Path(args.target)
    if not target_path.is_file():
        print(f"Error: Target file '{target_path}' not found.", file=sys.stderr)
        return 1

    content = target_path.read_text(encoding="utf-8")
    messages = parse_transcript_text(content)
    summary = _run_cli_audit(messages, str(target_path), args)

    _handle_exports(summary, str(target_path), args)
    print(format_markdown_report(summary))
    has_critical_or_high = any(
        f.severity in (RotSeverity.CRITICAL, RotSeverity.HIGH) for f in summary.findings
    )
    return 1 if has_critical_or_high else 0


if __name__ == "__main__":
    sys.exit(main())

