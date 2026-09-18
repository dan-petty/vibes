#!/usr/bin/env python3
"""Streaming Reasoning Token Parser & Bounded Stream Sanitizer.

Provides deterministic streaming isolation of reasoning tokens (<think>...</think>,
[reasoning]...[/reasoning]) emitted by frontier reasoning models. Prevents thinking
trace leakage into operational execution payloads, tool parameters, and user terminals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Sequence

DEFAULT_OPEN_TAG: Final[str] = "<think>"
DEFAULT_CLOSE_TAG: Final[str] = "</think>"
DEFAULT_MAX_THOUGHT_CHARS: Final[int] = 65_536


class ParserState(str, Enum):
    """Streaming finite state machine state for reasoning token extraction."""

    EMITTING = "EMITTING"
    THINKING = "THINKING"


@dataclass(frozen=True)
class StreamChunkResult:
    """Result of processing an incremental streaming text chunk."""

    visible_chunk: str
    thought_chunk: str
    is_thinking: bool


@dataclass
class SanitizerMetrics:
    """Telemetry tracking streaming sanitization, character counts, and bounding."""

    total_input_chars: int = 0
    visible_chars: int = 0
    thought_chars: int = 0
    discarded_thought_chars: int = 0
    open_tags_detected: int = 0
    close_tags_detected: int = 0
    unclosed_stream: bool = False


class StreamingReasoningSanitizer:
    """Streaming parser that strips internal reasoning blocks from LLM token streams.

    Features:
    - Zero-dependency streaming finite state machine.
    - Chunk boundary splitting tolerance (e.g. '<th' followed by 'ink>').
    - Bounded thinking buffer preventing memory exhaustion (CWE-400).
    - Isolated extraction of thoughts for audit trails and telemetry.
    """

    def __init__(
        self,
        open_tag: str = DEFAULT_OPEN_TAG,
        close_tag: str = DEFAULT_CLOSE_TAG,
        max_thought_chars: int = DEFAULT_MAX_THOUGHT_CHARS,
    ) -> None:
        self.open_tag = open_tag
        self.close_tag = close_tag
        self.max_thought_chars = max_thought_chars

        self._state = ParserState.EMITTING
        self._buffer: str = ""
        self._collected_thoughts: list[str] = []
        self._metrics = SanitizerMetrics()

    @property
    def metrics(self) -> SanitizerMetrics:
        """Return cumulative telemetry metrics."""
        return self._metrics

    @property
    def is_thinking(self) -> bool:
        """Return True if currently inside a reasoning block."""
        return self._state == ParserState.THINKING

    def get_accumulated_thoughts(self) -> str:
        """Return all extracted thinking tokens as a single string."""
        return "".join(self._collected_thoughts)

    def feed(self, chunk: str) -> StreamChunkResult:
        """Feed a streaming chunk into the state machine and return processed fragments."""
        if not chunk:
            return StreamChunkResult("", "", self.is_thinking)

        self._metrics.total_input_chars += len(chunk)
        text = self._buffer + chunk
        self._buffer = ""

        visible_out: list[str] = []
        thought_out: list[str] = []

        while text:
            if self._state == ParserState.EMITTING:
                text = self._consume_emitting(text, visible_out)
            else:
                text = self._consume_thinking(text, thought_out)

        vis_str = "".join(visible_out)
        th_str = "".join(thought_out)
        self._metrics.visible_chars += len(vis_str)
        return StreamChunkResult(vis_str, th_str, self.is_thinking)

    def flush(self) -> StreamChunkResult:
        """Flush remaining buffered characters at the end of the stream."""
        remaining = self._buffer
        self._buffer = ""

        if self._state == ParserState.THINKING:
            self._metrics.unclosed_stream = True
            thought_fragment = self._record_thought(remaining) if remaining else ""
            return StreamChunkResult("", thought_fragment, True)

        if remaining:
            self._metrics.visible_chars += len(remaining)
        return StreamChunkResult(remaining, "", False)

    def _consume_emitting(self, text: str, visible_out: list[str]) -> str:
        """Process text while in EMITTING state."""
        idx = text.find(self.open_tag)
        if idx != -1:
            visible_out.append(text[:idx])
            self._state = ParserState.THINKING
            self._metrics.open_tags_detected += 1
            return text[idx + len(self.open_tag) :]

        cutoff = self._find_potential_prefix(text, self.open_tag)
        if cutoff < len(text):
            visible_out.append(text[:cutoff])
            self._buffer = text[cutoff:]
            return ""

        visible_out.append(text)
        return ""

    def _consume_thinking(self, text: str, thought_out: list[str]) -> str:
        """Process text while in THINKING state."""
        idx = text.find(self.close_tag)
        if idx != -1:
            raw_thought = text[:idx]
            thought_out.append(self._record_thought(raw_thought))
            self._state = ParserState.EMITTING
            self._metrics.close_tags_detected += 1
            return text[idx + len(self.close_tag) :]

        cutoff = self._find_potential_prefix(text, self.close_tag)
        if cutoff < len(text):
            thought_out.append(self._record_thought(text[:cutoff]))
            self._buffer = text[cutoff:]
            return ""

        thought_out.append(self._record_thought(text))
        return ""

    def _record_thought(self, fragment: str) -> str:
        """Record thought text respecting buffer bounds."""
        current_len = self._metrics.thought_chars
        capacity = max(0, self.max_thought_chars - current_len)
        allowed = fragment[:capacity]
        discarded = len(fragment) - len(allowed)

        if allowed:
            self._collected_thoughts.append(allowed)
            self._metrics.thought_chars += len(allowed)
        if discarded > 0:
            self._metrics.discarded_thought_chars += discarded

        return allowed

    @staticmethod
    def _find_potential_prefix(text: str, tag: str) -> int:
        """Find index where a prefix of tag begins at the end of text."""
        max_prefix_len = min(len(text), len(tag) - 1)
        for length in range(max_prefix_len, 0, -1):
            if text.endswith(tag[:length]):
                return len(text) - length
        return len(text)


def sanitize_reasoning_stream(
    chunks: Sequence[str],
    open_tag: str = DEFAULT_OPEN_TAG,
    close_tag: str = DEFAULT_CLOSE_TAG,
    max_thought_chars: int = DEFAULT_MAX_THOUGHT_CHARS,
) -> tuple[str, str, SanitizerMetrics]:
    """Sanitize an entire sequence of streaming chunks in a single call."""
    sanitizer = StreamingReasoningSanitizer(
        open_tag=open_tag,
        close_tag=close_tag,
        max_thought_chars=max_thought_chars,
    )
    visible_fragments: list[str] = []

    for chunk in chunks:
        res = sanitizer.feed(chunk)
        if res.visible_chunk:
            visible_fragments.append(res.visible_chunk)

    flush_res = sanitizer.flush()
    if flush_res.visible_chunk:
        visible_fragments.append(flush_res.visible_chunk)

    return (
        "".join(visible_fragments),
        sanitizer.get_accumulated_thoughts(),
        sanitizer.metrics,
    )
