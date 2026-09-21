# The Disciplined Agentic Manifesto
### Beyond "Vibe Coding": The Principles of Rigorous Autonomous Engineering

> *"Vibe coding"* is writing prompts, hoping for the best, and manually wrestling with the hallucinated debris.  
> **Agentic Engineering** is building deterministic scaffolding around non-deterministic intelligence.

---

## The Core Thesis

Large Language Models are probabilistic token generators. When given a blank canvas and unrestrained agency, their natural tendency is toward:
- **Sprawling procedural logic** with deeply nested conditionals (`if/elif/else` ladders).
- **Silent degradation of edge cases** and hallucination of non-existent APIs.
- **Context saturation and amnesia**, where earlier architectural decisions are forgotten mid-session.
- **Zombie code accumulation**, leaving obsolete fallbacks and dead shims scattered across the codebase.

Treating agent coding as a casual "vibe" guarantees brittle, unmaintainable software. However, when an AI agent is surrounded by **uncompromising architectural invariants, deterministic quality gates, test-first contracts, and bounded operational state machines**, the equation flips:

The agent transitions from an erratic code generator into a relentless, tire-less software engineer capable of producing enterprise-grade systems with zero defects, continuous $\ge 90.0\%$ test coverage, and mathematically bounded complexity.

---

## The 5 Pillars of Disciplined Agentic Development

```mermaid
flowchart TD
    P1["1. Tests as Living Contracts (TDD)"]
    P2["2. AST-Enforced Invariants (Complexity <= 10)"]
    P3["3. Zero Zombie Code & Clean Breaking Changes"]
    P4["4. Grounded Project Governance (Zero Drift)"]
    P5["5. Self-Hardening Instruction Loops"]

    P1 --> Scaffold[High-Reliability Autonomous System]
    P2 --> Scaffold
    P3 --> Scaffold
    P4 --> Scaffold
    P5 --> Scaffold
```

### Pillar 1: Tests Are Executable Contracts, Not Afterthoughts
In traditional development, tests are often written after code as an afterthought. In agentic engineering, **tests must be written first**.
- Tests provide the agent with a deterministic sandbox.
- When an agent writes tests first, it must declare the interface, argument types, error conditions, and return structures before generating business logic.
- The test suite serves as an unyielding boundary condition: the agent cannot declare victory until every assertion passes.

### Pillar 2: Architectural Invariants Must Be Mechanically Enforced
LLMs cannot intuitively "feel" codebase rot. They cannot sense when a function has grown too complex or when indentation has nested too deeply.
- Invariants cannot rely on polite prompt instructions; they must be verified with automated AST scanners.
- In `devops-cli`, cyclomatic complexity is strictly capped at $\le 10$ and maximum nesting depth at $\le 5$ levels.
- If an agent generates messy procedural loops, the invariant test gate rejects the commit, forcing the agent to refactor into elegant functional pipelines, dictionary lookups, and single-responsibility helpers.

### Pillar 3: Zero Zombie Code & Clean Breaking Changes
The fear of breaking things creates legacy sediment. Weak agents frequently wrap old buggy functions in deprecation aliases or retain obsolete fallback branches "just in case."
- In modern alpha development, maintain **zero backwards compatibility guarantees** until release `1.0.0`.
- Eliminate legacy shims, dead code, and vestigial fallbacks ruthlessly.
- A codebase that carries no dead weight allows agents to navigate with high token efficiency and minimal confusion.

### Pillar 4: Grounded Project Governance (Zero Invisible Actions)
An agent operating without an external memory substrate will drift. It will duplicate work, jump between unrelated features, and lose focus on user requirements.
- Every engineering action, bug investigation, and pull request must be grounded in an external issue tracker (e.g. GitHub Projects v2) and a local task specification file.
- Explicit lifecycle transitions (`Backlog` -> `In Progress` -> `Review` -> `Done`) anchor the agent's attention and provide human operators with real-time visibility.

### Pillar 5: Self-Hardening Instruction Loops
Software bugs are not isolated incidents; they are symptoms of missing constraints.
- When an agent encounters an unhandled exception, syntax error, or rate-limit stall, fixing the bug in source code is only half the job.
- The agent must immediately update the agent operating instructions (`AGENTS.md`) with defensive rules, pre-flight checks, and avoidance patterns.
- Every failure permanently hardens the harness against recurrence.

---

## The Shift in Developer Experience

| Traditional "Vibe Coding" | Disciplined Agentic Engineering |
|---|---|
| Prompting in chat and copy-pasting diffs | Direct tool invocation via structured MCP protocols |
| Testing manually in terminal | Automated TDD with $\ge 90.0\%$ coverage gates |
| Allowing functions to grow unchecked | AST-enforced cyclomatic complexity $\le 10$, nesting $\le 5$ |
| Secrets leaked into logs or prompts | Zero-plaintext OS Keyring and RFC 5737 dummy sanitization |
| Hallucinating external APIs | Polyglot Tree-Sitter & CST contract parsing |
| Blaming the LLM for errors | Hardening the `AGENTS.md` instructions on failure |

---

## Conclusion

The power of AI coding is not determined by the model's raw parameters alone; it is determined by the **architecture of the harness** that guides it. `vibes` exists to document the science of that harness.
