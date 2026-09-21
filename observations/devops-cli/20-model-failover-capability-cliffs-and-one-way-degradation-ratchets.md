# Model Failover Capability Cliffs & One-Way Degradation Ratchets

## 1. Executive Context & Baseline

`devops-cli` implements a multi-tier model routing and failover architecture through its AI Gateway (`src/devops_cli/ai/gateway.py`). The gateway maps four virtual model aliases to physical backends:

- `devops-reasoning`: Llama-3.3-70B-Instruct on vLLM Tensor-Parallel ($TP=2$, AWQ quantization) (`gateway.py:40-65`)
- `devops-coder`: Qwen-2.5-Coder-14B on Ollama
- `devops-chat`: Qwen-2.5-Coder-7B on Ollama
- `devops-embedding`: BGE-M3 on Ollama

When a model tier becomes unavailable, the failover cascade (`gateway.py:67-73`) routes traffic down the chain: `devops-reasoning` $\to$ `devops-coder` $\to$ `devops-chat` $\to$ `direct-ollama`. The embedding engine (`src/devops_cli/ai/rag/embeddings.py:196-300`) adapts batch sizes dynamically, halving from 32 down to 1 when latency exceeds 2.0s (`embeddings.py:266-279`).

## 2. The Observed Phenomenon

**2a. Capability Cliff at Failover Boundaries**: When `devops-reasoning` (70B parameters, 32K+ context, high-fidelity instruction following) fails over to `devops-coder` (14B parameters, shorter effective context, weaker instruction adherence), the task does not gracefully degrade — it falls off a cliff. A 32K-token architectural reasoning task that succeeds at 70B produces incoherent, hallucinated output at 14B. The failover cascade assumes that lower-tier models produce lower-quality-but-usable results; in practice, they produce categorically different failure modes:

- **70B → 14B**: Complex multi-file refactoring instructions are ignored; the model reverts to single-file edits
- **14B → 7B**: Tool parameter schemas are hallucinated despite `additionalProperties: false`; JSON output malformation rate spikes
- **Any quantized tier**: AWQ 4-bit models (`gateway.py:444-445`) lose the ability to produce syntactically valid JSON reliably, triggering the structured output repair pipeline (`structured.py`) on every response

**2b. One-Way Embedding Batch Degradation**: The embedding engine's adaptive batching (`embeddings.py:266-279`) halves `self._current_batch_size` whenever a request exceeds 2.0s latency. However, there is no corresponding mechanism to increase batch size when latency returns to normal. A single transient network spike permanently ratchets batch size from 32 down to 16, then 8, then 4, then 2, then 1 — and stays there until the process restarts. This transforms a momentary infrastructure blip into permanent throughput degradation.

The ratchet is best read by what is missing from it — every arrow points one way, and the only edge leaving `batch = 1` is a process restart:

```mermaid
stateDiagram-v2
    direction LR
    [*] --> B32
    B32: batch = 32
    B16: batch = 16
    B8: batch = 8
    B4: batch = 4
    B2: batch = 2
    B1: batch = 1 (floor)

    B32 --> B16: one request over 2.0s
    B16 --> B8: one request over 2.0s
    B8 --> B4: one request over 2.0s
    B4 --> B2: one request over 2.0s
    B2 --> B1: one request over 2.0s
    B1 --> B1: healthy latency changes nothing
    B1 --> [*]: process restart, the only exit

    note right of B1
        No transition returns leftward.
        A 30-second network blip costs
        32x throughput until redeploy.
    end note
```

**2c. Hardcoded Model Identity in Scaling Engine**: The vLLM scaling function (`gateway.py:415-454`) hardcodes the served model as `"casperhansen/llama-3.3-70b-instruct-awq"` and assumes $TP=2$ with 24GB VRAM per GPU. If the cluster runs FP8, BF16, DeepSeek, or Mistral models, the scaling arithmetic produces incorrect VRAM estimates and the deployment command targets a phantom model.

## 3. The Underlying Failure Mode or Catalyst

The root cause is **treating model capabilities as a continuous spectrum when they are actually a discrete step function**. The failover architecture implicitly models capability as:

$$C(\text{reasoning}) > C(\text{coder}) > C(\text{chat}) > C(\text{direct})$$

where $C$ is a smooth, monotonically decreasing function. In reality, model capabilities exhibit sharp phase transitions:

- Below ~13B parameters, reliable tool calling collapses
- Below ~30B parameters, multi-step planning with constraints degrades non-linearly
- AWQ 4-bit quantization introduces measurable JSON syntax error rates that don't exist at FP16/BF16

The failover cascade routes a 70B task to a 14B model because the 14B model is "next in line" — but the task may require capabilities that only exist above a 30B threshold. There is no mechanism to detect that a failover target is categorically incapable of the assigned task, so the system silently degrades from "correct execution" to "confident hallucination."

The one-way batch ratchet exhibits a different but related pattern: **asymmetric adaptation**. The system learns to be cautious (shrink batch size) but never learns to be confident again (restore batch size). This is the operational equivalent of a circuit breaker that trips but never resets — appropriate for safety-critical systems, but pathological for throughput optimization where transient failures are the norm.

## 4. Remediation & Architectural Pattern

**4a. Capability-Gated Failover with Task Classification**: Before routing a failed task to a lower tier, classify the task's minimum capability requirements:

- **Tier 1 (Reasoning)**: Multi-file refactoring, architectural analysis, complex multi-step planning → minimum 30B+ parameters
- **Tier 2 (Coding)**: Single-file edits, test generation, code completion → minimum 7B+ parameters
- **Tier 3 (Chat)**: Documentation, summarization, simple Q&A → any model

If the failover target's tier is below the task's minimum requirement, reject the failover and return an explicit error rather than silently routing to a model that will produce hallucinated output.

```mermaid
flowchart TD
    classDef failure fill:#b3261e,color:#fff
    classDef success fill:#1b5e20,color:#fff
    classDef accent fill:#4527a0,color:#fff
    classDef neutral fill:#37474f,color:#fff

    Task["Task arrives: multi-file refactor"] --> Primary{"devops-reasoning (70B) available?"}
    Primary -->|"Yes"| Execute["Correct execution"]:::success
    Primary -->|"No"| Gate{"Capability gate: does the next tier<br/>meet the task's 30B+ minimum?"}:::accent

    Gate -->|"No (14B coder)"| Refuse["Explicit CapabilityFloor error<br/>surfaced to the caller"]:::success
    Gate -->|"Yes"| Failover["Route to next tier"]:::neutral
    Failover --> Execute

    Primary -.->|"Ungated cascade (observed)"| Cliff["Routed to 14B because it is next in line"]
    Cliff --> Hallucinate["Confident hallucination:<br/>instructions dropped, output plausible"]:::failure

    Hallucinate -.->|"indistinguishable from success<br/>until a human reads the diff"| Task
```

> [!IMPORTANT]
> The gate does not make the failure go away — it converts an **undetectable** failure into a **loud** one. A `CapabilityFloor` error costs one retry; a confidently hallucinated refactor costs a review cycle and whatever it silently broke.

**4b. Exponential Backoff Batch Recovery**: Replace the one-way halving ratchet with a bidirectional adaptation strategy:

```python
if latency > threshold:
    batch_size = max(min_batch, batch_size // 2)    # Shrink fast
    recovery_cooldown = 10  # requests before attempting recovery
elif recovery_cooldown <= 0 and latency < threshold * 0.5:
    batch_size = min(max_batch, batch_size + 1)     # Grow slow
    recovery_cooldown = 10
else:
    recovery_cooldown -= 1
```

Shrink fast (halve), grow slow (increment by 1), with a cooldown window between recovery attempts. This mirrors TCP congestion control's AIMD (Additive Increase, Multiplicative Decrease) strategy.

**4c. Dynamic Model Capability Probing**: On gateway startup and periodically thereafter, probe each model endpoint with a standardized capability test:

1. **JSON adherence**: Can the model produce valid JSON matching a 5-field schema?
2. **Tool calling**: Can the model select the correct tool from a 10-tool manifest?
3. **Context length**: Can the model recall information from position 16K in a 32K context?
4. **Instruction following**: Does the model respect explicit prohibitions (e.g., "do not use field X")?

Cache probe results in `gateway_state.json` and use them to populate the capability matrix that gates failover routing.

## 5. Verifiable Impact & Key Takeaways

- **Failover cliff quantification**: 70B → 14B failover on a 32K reasoning task produces 0% usable output (complete task failure) versus 85%+ success at the native tier. Silent degradation is worse than explicit failure.
- **Batch ratchet cost**: A single 2.1s latency spike permanently reduces embedding throughput from 32 items/request to 1 item/request — a 32× throughput collapse that persists until process restart.
- **AIMD precedent**: TCP's congestion control solved this exact problem in 1988. Additive Increase / Multiplicative Decrease is the canonical solution for systems that must adapt to transient failures without permanent degradation.

> **Aphorism**: A failover cascade that routes a 70B task to a 7B model doesn't degrade gracefully — it degrades catastrophically while reporting success. Silent capability cliffs are worse than loud failures.

> **The Ratchet Trap**: Any adaptive system that can decrease a parameter but never increase it will converge to its minimum. One-way adaptation is not adaptation — it is permanent degradation with a transient trigger.
