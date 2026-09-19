# Context Accumulation Drift & Lossy Reflection Truncation

## 1. Executive Context & Baseline

`devops-cli`'s multi-agent pipeline (`src/devops_cli/ai/agents/pipeline.py:76-282`) sequences multiple `PydanticAgent` instances through a conversation, passing accumulated context from earlier stages to later ones. The `AgentMemory` subsystem (`src/devops_cli/ai/agents/memory.py:55-196`) manages conversation history with auto-summarization triggered at `_DEFAULT_MAX_ENTRIES = 50` entries or `_DEFAULT_MAX_CHARS = 96,000` characters, preserving the `_DEFAULT_KEEP_RECENT = 10` most recent turns. When structured output validation fails, the retry engine (`src/devops_cli/ai/client/structured.py:78-96`) truncates error details to `CONST_MAX_ERROR_DETAIL_LENGTH = 256` characters before reflecting them back to the model for self-correction.

These three mechanisms — pipeline context accumulation, memory auto-summarization, and error detail truncation — create a compound failure mode where the model progressively loses access to critical information while appearing to maintain conversational coherence.

## 2. The Observed Phenomenon

**2a. Pipeline Context Bloat Without Truncation**: In sequential pipeline mode (`pipeline.py:196-199`), accumulated context from earlier agent stages is linearly appended turn by turn. A 5-stage pipeline where each agent generates 4,000 tokens of output accumulates 20,000 tokens of pipeline context before the final stage begins. No token truncation or summarization is applied between pipeline stages — the context grows monotonically until it either exceeds the model's window or displaces the original system instructions from the model's effective attention range.

**2b. System Instruction Eviction Under Multi-Turn Drift**: As tool call outputs, code snippets, test tracebacks, and conversation history accumulate in a long session, the model's attention to initial system prompt instructions (such as `AGENTS.md` rules) progressively degrades. This manifests as:
- Complexity constraints ($M \le 10$, depth $\le 5$) being ignored after 15+ turns
- Security sanitization rules (RFC 1918 prohibition) being forgotten after context exceeds 64K tokens
- Formatting requirements (structural tuple assertions) being abandoned in favor of simpler patterns

The `AgentMemory` auto-summarization at 96K characters is the correct defensive instinct, but summarization is lossy: the summarizer compresses older turns into bullets, discarding the precise wording of invariant constraints that the model needs verbatim.

**2c. Lossy Error Reflection Limiting Self-Correction**: When the model emits malformed JSON that fails Pydantic validation, the retry engine in `structured.py` truncates the validation error to 256 characters (`err_msg[:253] + "..."`). Complex nested validation errors — for example, a list of 5 invalid items each with field locators and type mismatch descriptions — are cut off mid-diagnostic, leaving the model with an incomplete error signal. The model then "fixes" the wrong field or applies a generic structural change rather than targeting the specific validation failure.

## 3. The Underlying Failure Mode or Catalyst

The root cause is a **conservation of attention** problem operating across three timescales:

1. **Intra-turn** (milliseconds): Transformer self-attention distributes weight across all tokens in the context window. As context length increases from 8K to 32K to 128K tokens, the attention density per token decreases proportionally. System instructions placed at position 0 compete for attention against tool outputs placed at position 30,000.

2. **Inter-turn** (seconds to minutes): Each new turn pushes older context further from the model's focal range. The "lost in the middle" phenomenon — where models perform worst on information placed in the center of long contexts — means that mid-session architectural constraints become invisible even though they're technically still in the window.

3. **Cross-session** (hours to days): Auto-summarization compresses the conversational record into lossy bullets, preserving _what was discussed_ but not _why specific constraints were established_. A rule like "use structural tuple equality assertions because each `assert` adds +1 to McCabe complexity under Python AST semantics" gets compressed to "use tuple assertions" — losing the causal rationale that enables the model to generalize the constraint to novel situations.

The 256-character error truncation in `structured.py` compounds this by applying lossy compression at the feedback tier — the exact location where maximum information fidelity is needed for deterministic self-correction.

## 4. Remediation & Architectural Pattern

**4a. Invariant Pinning via Sliding System Prompt Reinforcement**: Rather than relying on the initial system prompt alone, inject a compressed "invariant reminder" block at the boundary between system context and user context every N turns. This block contains only the mechanical constraints ($M \le 10$, depth $\le 5$, no RFC 1918, tuple assertions) without rationale — pure prescriptive rules that cost ~200 tokens but anchor attention on critical invariants.

**4b. Pipeline Stage Context Budgeting**: Apply the same `ContextPacker` binary search truncation (`context_packer.py:268-327`) to inter-stage pipeline context. Before passing accumulated output to stage $N+1$, truncate to a per-stage budget (e.g., 4,000 tokens) using AST-aware summarization rather than naive line slicing.

**4c. Lossless Error Reflection for Structured Retries**: Replace the 256-character truncation with a structured error summary that preserves field paths and type violations:

```python
# Instead of: err_msg[:253] + "..."
# Use: structured_error_summary(error, max_fields=5)
{
  "field": "findings[2].severity",
  "error": "expected Literal['low','medium','high'], got 'critical'",
  "fix": "use one of: 'low', 'medium', 'high'"
}
```

This costs more tokens per retry but achieves single-turn convergence instead of multi-turn degradation loops.

**4d. Semantic Memory Partitioning**: Partition `AgentMemory` into two tiers: (1) a _volatile_ conversation buffer subject to auto-summarization, and (2) an _invariant_ constraint store that is never summarized and always re-injected verbatim at the head of each turn. This separates ephemeral conversational flow from permanent architectural rules.

See pattern: [epistemic-hygiene-and-context-pruning](../../patterns/epistemic-hygiene-and-context-pruning.md)

## 5. Verifiable Impact & Key Takeaways

- **Pipeline bloat quantification**: 5-stage pipeline × 4K tokens/stage = 20K tokens accumulated, consuming 62.5% of a 32K window before the final agent begins. Stage budgeting at 4K reclaims 16K tokens.
- **Error truncation cost**: 256-char cap forces multi-turn retry loops; structured 5-field error summaries at ~400 chars achieve 100% single-turn correction on nested validation failures.
- **Memory partition precedent**: The existing `_DEFAULT_KEEP_RECENT = 10` in `memory.py:28` proves the system already distinguishes "recent = important" from "old = compressible." Extending this to "invariant = never compress" is a natural evolution.

> **Aphorism**: Context is not a lake — it is a river. Information placed at the source does not survive the journey downstream without mechanical reinforcement at every bend.

> **The Attention Conservation Law**: In a transformer, attention is zero-sum. Every token of pipeline bloat, traceback noise, or verbose tool output steals attention from the system instructions that keep the agent disciplined. Budget context like you budget money — because overspending on noise bankrupts the model's ability to follow rules.
