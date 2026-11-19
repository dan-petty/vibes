# Pattern: Root-Cause Remediation & Instruction Hardening

> **Pattern Class**: Meta-Cognitive Self-Improvement & Harness Resilience
> **Problem**: An agent that fixes a defect learns nothing: the next session starts blank and reintroduces it
> **Solution**: Codify the preventative rule into the instruction file on every defect fix, so the lesson survives the session that learned it
> **TLDR**: Codify preventative rules into instruction files (AGENTS.md) upon fixing any bug, ensuring lessons survive across stateless agent sessions.
> **ELI:7b**: Every time an AI makes a mistake and fixes it, write a new rule in the instructions so future AIs never repeat that mistake.

---

## 1. Problem Statement

In human development, an engineer who fixes a bug learns from it. In standard LLM setups, each agent session starts with a blank memory:
- If Agent A encounters a subtle bug (e.g. an unhandled deprecation in Pydantic v2 or a rate-limit error) and fixes it in code, Agent B in a future session may re-introduce the exact same mistake because the system instructions remain unchanged.
- This creates **cyclic regressions and repeated failure modes**.

---

## 2. Core Mechanics

The Root-Cause Hardening pattern couples every code fix with an **instruction hardening step**:

```mermaid
flowchart TD
    Error[Runtime Exception / Deprecation Warning / Failure] --> Diagnose[1. Identify Underlying Root Cause]
    Diagnose --> CodeFix[2. Fix Root Cause at Source in src/ & tests/]
    CodeFix --> Verify[3. Verify CI Gates Pass with Zero Warnings]
    Verify --> Decision{Could Another Agent Repeat This?}
    Decision -- Yes --> Harden[4. Harden AGENTS.md with Rule & Guardrail]
    Decision -- No --> Done[Conclude Work]
    Harden --> Future[Future Agent Sessions Inherit Guardrail]
```

### The Dual-Remediation Rule

Whenever an agent encounters any unexpected failure, unhandled exception, CLI crash, or deprecation warning:
1. **Fix in Source Code**: Remediate the underlying root cause directly. Never apply superficial workarounds, mask exceptions (`except Exception: pass`), or suppress warnings.
2. **Harden the Instruction File (`AGENTS.md`)**:
   - If the failure was caused by an ambiguous convention, missing pre-flight check, or common antipattern, add an explicit rule to `AGENTS.md`.
   - The rule must clearly specify:
     - The prohibited behavior.
     - The required engineering replacement.
     - The rationale.

---

## 3. Implementation Example

### Illustrative Example
- **Issue**: An agent uses `datetime.utcnow()`, which Python 3.12 deprecates with a `DeprecationWarning`.
- **Step 1 (Source Fix)**: Replace it with `datetime.now(timezone.utc)`.
- **Step 2 (Instruction Hardening)**: Add a guardrail to `AGENTS.md`. devops-cli has a real rule of this kind, added in v0.2.18 (commit 46472f3a9, 2026-09-16):
  > *"Proactive Deprecation Remediation & Zero-Warning Mandate ... Suppressing, ignoring, bypassing, or tolerating actionable deprecation warnings in project code, configurations, or test suites is strictly prohibited."*
- **Outcome**: The rule tells the next agent not to suppress the warning. A mechanical check, such as Ruff rule `DTZ003` or `-W error::DeprecationWarning` in the test run, is what stops the call from coming back.

---

## 4. Guardrails & Anti-Patterns

| Anti-Pattern | Why It Fails | Root-Cause Guardrail |
|---|---|---|
| **Symptom Masking** | Suppressing warnings with `@pytest.mark.filterwarnings("ignore")`. | Banned. Fix the deprecated call at the source. |
| **Silent Workaround** | Working around a broken CLI subcommand without logging an issue. | Banned. File a defect immediately and fix the underlying CLI tool. |
| **Instruction Bloat** | Adding long paragraphs of prose to instructions for trivial typos. | Keep additions concise, actionable, and rule-oriented. |

---

## 5. Cross-References
- [Observation 01: TDD as Living Contract](../observations/devops-cli/01-tdd-as-living-contract.md)
- [Pattern: CEGIS & Hypothesis Debugging](./cegis-and-hypothesis-debugging.md)
- [Canonical Agent Instructions](../AGENTS.md)
