# Observation 10: Negative Tool Contract Assertions & Prescriptive Prompt Synthesis

> **Project**: `devops-cli` & `vibes`  
> **Topic**: JSON Schema Draft 2020-12 Verification Gates, Negative Schema Assertions, and Closed-Loop Agent Self-Correction  
> **Key Metric**: Every argument key absent from the contract is rejected (by construction, `examples/tool-contract-verifier/verifier.py`); bounded string length caps ($\le 256$ chars) mitigating CWE-400 log bloat; no model was run, so no self-correction rate exists  
> **TLDR**: Enforce JSON Schema negative assertions (additionalProperties: false) to detect hallucinated tool parameters and synthesize prescriptive error prompts that give the model what it needs to correct the call on the next turn.
> **ELI:7b**: Forbid extra made-up parameters on AI tools. When the AI hallucinates an argument, give it an exact error message showing the valid parameters.

---

## 1. Executive Context & The Tool Invocation Gap

Autonomous AI agents interact with development environments, infrastructure clouds, and SDKs primarily through **tool calling** (e.g. Model Context Protocol / FastMCP, JSON-RPC, OpenAI function calling). In this paradigm:
1. The tool manifest advertises an expected JSON schema of parameters and types.
2. The language model generates structured JSON arguments based on its interpretation of the user goal and context.
3. The host runtime deserializes the JSON payload and invokes backend execution handlers.

However, in production agentic workflows, **frontier language models exhibit systematic tool calling failure modes**:
- **Parameter Hallucination**: Passing plausible but non-existent keys (e.g. `dry_run: true`, `timeout_seconds: 30`, or `verbose: true`) not present in the tool's signature.
- **Type Permissiveness & Coercion Failures**: Passing stringified integers (`replicas: "3"` instead of `replicas: 3`) or boolean strings (`enabled: "true"`), causing backend runtime errors.
- **Unbounded String Inputs (CWE-400 / Log Bloat)**: Submitting massive strings, raw stack traces, or entire file contents into scalar fields (e.g. `reason` or `message`), inflating log storage and triggering denial-of-service conditions.
- **Uninformative Stack Traces**: When a backend tool fails with an unhandled Python exception (`KeyError: 'branch'` or `TypeError: '>' not supported between 'str' and 'int'`), the agent often fails to diagnose the root cause, leading to repetitive retry loops and prompt context exhaustion.

---

## 2. The Observed Phenomenon: Negative Schema Assertions

To systematically resolve tool hallucination, we implemented the **Formal Tool Contract Verification Gate (`examples/tool-contract-verifier/`)** in `vibes`. It is a hand-written Python validator, not a JSON Schema engine; it can export each contract as a Draft 2020-12-style `parameters` object with `additionalProperties: false`. In September 2026, `devops-cli` source had no `additionalProperties` declaration (`git grep additionalProperties acd091826 -- src` finds nothing).

```mermaid
flowchart TD
    LLM["Agent Tool Invocation Payload<br/>(e.g. deploy_stack)"] --> Gate["ToolContractVerifier"]
    Gate --> Check1{"Check Required Fields"}
    Check1 -->|Missing Field| Err1["Prescriptive Feedback:<br/>'Missing required parameter X'"]
    Check1 -->|Pass| Check2{"Negative Schema Assertion<br/>(strict_forbid_extra)"}
    Check2 -->|Extra / Hallucinated Keys| Err2["Prescriptive Feedback:<br/>'Remove unknown argument Y. Valid keys: [...]'"]
    Check2 -->|Pass| Check3{"Table-Driven Type & Bounds Check<br/>(str <= 256, int in [min, max])"}
    Check3 -->|Type / Bound Violation| Err3["Prescriptive Feedback:<br/>'Coerce replicas to integer; cap string <= 256'"]
    Check3 -->|Pass| Exec["Invoke Backend Execution Handler"]

    Err1 --> Synth["Prescriptive Error Prompt Synthesizer"]
    Err2 --> Synth
    Err3 --> Synth
    Synth --> ZeroShot["Agent Corrects the Call (Turn 2)"]
```

### The Invariant Distinction: Permissive vs. Negative Schemas
In standard web API development, frameworks frequently ignore unexpected keys (`allow_extra = True`) to maintain backwards compatibility. In agentic engineering, **this permissiveness is disastrous**:
- If an agent passes `dry_run: true` to a tool that does not support dry-runs, a permissive parser silently discards the flag and executes destructive live mutations!
- If an agent passes `timeout: 10` to a long-running backup task, discarding the unrecognized parameter allows the command to hang indefinitely.

By enforcing **strict negative assertions** (`"additionalProperties": False` in JSON Schema Draft 2020-12 and OpenAPI specs), the verifier treats any unrecognized key as a critical contract violation.

---

## 3. Prescriptive Error Prompt Synthesis vs. Stack Traces

The breakthrough of the contract verifier lies in the **synthesis of prescriptive corrective feedback**:

### Unhandled Exception (Antipattern)
```text
Traceback (most recent call last):
  File "server.py", line 142, in handle_call
    replicas = int(payload["replicas"])
TypeError: int() argument must be a string, a bytes-like object or a real number, not 'dict'
```
*Agent Reaction*: Often hallucinates code modifications, apologizes unnecessarily, or repeats the same tool call with slightly different phrasing.

### Prescriptive Prompt Synthesis (The Disciplined Pattern)
Abridged illustration; the verifier's actual message numbers each violation and adds a `Prescriptive Action` line:
```text
TOOL CONTRACT VIOLATION for 'k8s_deploy_stack':
- Parameter 'replicas': Value must be an integer, got string ('2'). Coerce 'replicas' to integer.
- Unknown parameter 'dry_run': Tool does not accept 'dry_run'. Remove 'dry_run' from arguments.
  Valid parameters: ['cluster', 'manifest_path', 'namespace', 'replicas']
- Parameter 'release_name': String length (312) exceeds maximum bounded limit of 256 characters (CWE-400).
Action: Update argument dictionary to satisfy contract before re-invoking.
```

`test_verifier.py` checks that the verifier detects each violation and builds this feedback (`test_verifier_corrective_feedback_generation`). It calls no language model, so it does not measure how often a model corrects the call on the next turn.

---

## 4. Architectural Implementation & Headroom Compliance

To satisfy strict project complexity ($M \le 10$) and proactive headroom ($M \le 6$, $Depth \le 3$) invariants, the contract verifier decomposes validation logic into table-driven dispatch:

```python
# Table-driven type predicates replacing procedural if/elif ladders
TYPE_PREDICATES: dict[ParameterType, Callable[[Any], bool]] = {
    ParameterType.STRING: lambda v: isinstance(v, str),
    ParameterType.INTEGER: lambda v: isinstance(v, int) and not isinstance(v, bool),
    ParameterType.FLOAT: lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    ParameterType.BOOLEAN: lambda v: isinstance(v, bool),
    ParameterType.LIST: lambda v: isinstance(v, list),
    ParameterType.DICT: lambda v: isinstance(v, dict),
}
```

Replacing procedural `if/elif` ladders with registry lookups keeps the type check (`_check_type_match`) to a single lookup. The earlier ladder version was not kept, so no before/after complexity figure exists.

---

## 5. Key Engineering Recommendations

1. **Always Enforce Negative Schema Assertions (`additionalProperties: false`) on Agent Tools**: Never permit unrecognized parameters in agent tool schemas. Negative assertions intercept hallucinated options before side-effects occur.
2. **Cap Scalar String Lengths ($\le 256$ chars)**: Enforce bounded caps on caller-provided scalar inputs to prevent context bloating and log injection (CWE-400).
3. **Emit Prescriptive Action Hints Over Raw Exception Traces**: Format contract violations with exact actionable recommendations (`"Remove key X"`, `"Coerce Y to int"`).
4. **Use Table-Driven Type Predicates**: Implement contract validators with dispatch dictionaries rather than branching cascades to preserve cyclomatic headroom ($M \le 6$).
