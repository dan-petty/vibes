"""Comprehensive test suite for Streaming Reasoning Token Parser & Sanitizer."""

from __future__ import annotations

from sanitizer import (
    StreamingReasoningSanitizer,
    sanitize_reasoning_stream,
)


def test_streaming_sanitizer_clean_text() -> None:
    """Ensure text without reasoning tags flows through unmodified."""
    chunks = ["Hello ", "world! ", "How are you?"]
    visible, thoughts, metrics = sanitize_reasoning_stream(chunks)
    assert (
        visible,
        thoughts,
        metrics.open_tags_detected,
        metrics.close_tags_detected,
        metrics.unclosed_stream,
    ) == ("Hello world! How are you?", "", 0, 0, False)


def test_streaming_sanitizer_single_chunk_thought() -> None:
    """Ensure complete thinking block inside a single chunk is extracted cleanly."""
    chunk = "Starting.<think>Planning the solution step by step.</think>Finished."
    visible, thoughts, metrics = sanitize_reasoning_stream([chunk])
    assert (
        visible,
        thoughts,
        metrics.open_tags_detected,
        metrics.close_tags_detected,
    ) == ("Starting.Finished.", "Planning the solution step by step.", 1, 1)


def test_streaming_sanitizer_split_tags_across_chunks() -> None:
    """Ensure tags split across streaming chunk boundaries are properly buffered."""
    chunks = [
        "Prefix text ",
        "<th",
        "ink>Internal ",
        "reasoning here",
        "</th",
        "ink> Suffix text.",
    ]
    visible, thoughts, metrics = sanitize_reasoning_stream(chunks)
    assert (
        visible,
        thoughts,
        metrics.open_tags_detected,
        metrics.close_tags_detected,
    ) == (
        "Prefix text  Suffix text.",
        "Internal reasoning here",
        1,
        1,
    )


def test_streaming_sanitizer_interleaved_thoughts() -> None:
    """Ensure multiple thinking blocks interleaved with visible text are isolated."""
    chunks = [
        "First step.\n",
        "<think>Compute 1 + 1 = 2</think>\n",
        "Result is 2.\n",
        "<think>Verify 2 * 2 = 4</think>\n",
        "All verified.",
    ]
    visible, thoughts, metrics = sanitize_reasoning_stream(chunks)
    assert (
        visible,
        thoughts,
        metrics.open_tags_detected,
        metrics.close_tags_detected,
    ) == (
        "First step.\n\nResult is 2.\n\nAll verified.",
        "Compute 1 + 1 = 2Verify 2 * 2 = 4",
        2,
        2,
    )


def test_streaming_sanitizer_unclosed_thought_at_eof() -> None:
    """Ensure unclosed reasoning block at EOF triggers unclosed_stream metric and buffers thought."""
    sanitizer = StreamingReasoningSanitizer()
    r1 = sanitizer.feed("Before tag.")
    r2 = sanitizer.feed("<think>Unclosed reasoning that ends abruptly")
    flush_res = sanitizer.flush()

    assert (
        r1.visible_chunk,
        r2.visible_chunk,
        flush_res.visible_chunk,
        sanitizer.is_thinking,
        sanitizer.metrics.unclosed_stream,
        "Unclosed reasoning" in sanitizer.get_accumulated_thoughts(),
    ) == ("Before tag.", "", "", True, True, True)


def test_streaming_sanitizer_custom_tags() -> None:
    """Ensure custom tag pairs such as [reasoning]...[/reasoning] are supported."""
    chunks = ["Header\n", "[reasoning]Secret logic[/reasoning]\n", "Footer"]
    visible, thoughts, metrics = sanitize_reasoning_stream(
        chunks, open_tag="[reasoning]", close_tag="[/reasoning]"
    )
    assert (
        visible,
        thoughts,
        metrics.open_tags_detected,
        metrics.close_tags_detected,
    ) == ("Header\n\nFooter", "Secret logic", 1, 1)


def test_streaming_sanitizer_bounded_buffer_discard() -> None:
    """Ensure thinking buffer caps discard excess characters when exceeding limit."""
    sanitizer = StreamingReasoningSanitizer(max_thought_chars=10)
    sanitizer.feed("<think>0123456789EXCESS_CHARACTERS</think>Done.")
    sanitizer.flush()

    assert (
        sanitizer.get_accumulated_thoughts(),
        sanitizer.metrics.thought_chars,
        sanitizer.metrics.discarded_thought_chars > 0,
    ) == ("0123456789", 10, True)


def test_streaming_sanitizer_empty_and_partial_flush() -> None:
    """Ensure empty chunk handling and flushing trailing non-tag buffers."""
    sanitizer = StreamingReasoningSanitizer()
    empty_res = sanitizer.feed("")
    partial_res = sanitizer.feed("Ending on partial <th")
    flush_res = sanitizer.flush()

    assert (
        empty_res.visible_chunk,
        partial_res.visible_chunk,
        flush_res.visible_chunk,
        sanitizer.metrics.visible_chars > 0,
    ) == ("", "Ending on partial ", "<th", True)


def test_a_stream_that_opens_its_thought_in_the_prompt_does_not_leak_it() -> None:
    """The parser could only start in EMITTING, so a closing-tag-only stream leaked entirely.

    Some model templates put the opening tag in the prompt rather than the completion, so
    the stream begins *inside* the thought and carries only `</think>`. Started in EMITTING,
    every reasoning token was treated as visible output and handed to whatever consumes the
    "clean" stream — the exact leak this component exists to prevent.
    """
    chunks = ["Deciding what to commit.", "</think>", 'git commit -m "x"']
    visible, thoughts, _ = sanitize_reasoning_stream(chunks, starts_thinking=True)
    assert (visible, thoughts) == ('git commit -m "x"', "Deciding what to commit.")


def test_a_closing_tag_while_emitting_is_recorded_rather_than_passed_through() -> None:
    """A caller who got the flag wrong finds out, instead of shipping the leak silently."""
    visible, _, metrics = sanitize_reasoning_stream(["thought", "</think>", "answer"])
    assert (metrics.unopened_close_tags, "</think>" in visible) == (1, False)
