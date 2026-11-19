# Observation 27: Agentic Project Self-Documentation & The Phantom Architecture Trap

> **TLDR**: Prevent the Phantom Architecture Trap where agents document imagined features by deriving docs directly from AST reflection and schema extraction.
> **ELI:7b**: Generate CLI and API documentation directly from real code symbols so the docs can never describe features that don't actually exist.

**Category**: Systems & Autonomous Governance
**Status**: Pattern; no measured impact data
**Canonical Implementation**: [`tools/docs_validator.py`](../../tools/docs_validator.py), [`patterns/multi-tier-living-documentation.md`](../../patterns/multi-tier-living-documentation.md)
**Verification Suite**: [`tests/test_docs_validator.py`](../../tests/test_docs_validator.py)

---

## 1. Executive Context & Baseline

As autonomous coding agents achieve unprecedented development velocity, traditional human-authored documentation falls further behind code changes. When agents generate, refactor, and ship multi-file pull requests in minutes, human-written manuals, CLI references, and architecture guides lag weeks or months behind, creating catastrophic documentation debt.

To close this velocity gap, teams increasingly embrace **Agentic Project Self-Documentation**: delegating the generation, updating, and curation of documentation, architectural decision records (ADRs), API schemas, and living standards directly to AI agents.

However, experience in [`devops-cli`](https://github.com/dan-petty/devops-cli) and [`vibes`](https://github.com/dan-petty/vibes) points to a fundamental architectural challenge: **The Dual-Audience Paradox**.
Technical documentation must simultaneously serve two fundamentally divergent consumers:
1. **Human Stakeholders**: Who require high-level design intent, architectural rationale, security guarantees, and business context.
2. **Stateless Successive Agent Generations**: Who require rigid syntactic precision, low-entropy machine contracts, explicit negative constraints, and mechanically verifiable interfaces to prevent multi-turn hallucinations.

When unconstrained language models author both layers in unstructured prose, the documentation ceases to be a reliable source of truth and collapses into high-risk pathological states.

```mermaid
flowchart LR
    subgraph HumanSDLC ["1. Traditional Human SDLC"]
        direction TB
        HCode["Code Implementation"] -->|"Velocity Mismatch"| HDocs["Human Documentation"]
        HDocs -.->|"Lags Months Behind"| HRot["Documentation Rot & Technical Debt"]
    end

    subgraph AgenticLoop ["2. Unconstrained Agentic Loop (Anti-Pattern)"]
        direction TB
        ACode["Agent Synthesizes Code"] -->|"Generative Prose"| ADocs["Agent Narrative Docs"]
        ADocs -->|"Primes Context Window"| ANext["Next Agent Generation"]
        ANext -->|"Treats Phantom Docs as Truth"| AReg["Cascading Runtime Hallucination"]
    end

    subgraph LivingArchitecture ["3. Living Documentation System"]
        direction TB
        T1["Tier 1: AST Mechanical Reflection"] --> VerifiedDocs["Verified Ground Truth"]
        T2["Tier 2: Bounded ADR Synthesis"] --> VerifiedDocs
        VerifiedDocs -->|"Fail-Closed Pre-Commit / CI"| T3["Tier 3: docs_validator.py Oracle"]
        T3 -->|"Context Compactor"| T4["Tier 4: Active Compaction"]
        T4 --> NextAgent["Next Agent Generation (High Grounding)"]
    end
```

---

## 2. The Observed Phenomenon

Four pathologies recur in agent-written documentation in `devops-cli` and `vibes`. No session count was recorded, and the cases in 2.1 and 2.2 are illustrative:

### 2.1 The Phantom Architecture Trap
Language models possess a strong generative bias toward describing software systems as complete, elegant, and fully realized—even when underlying implementations are skeletal stubs, mock handlers, or string-matching shortcuts.
- **Illustrative Case**: An agent assigned to document background worker isolation describes a "multi-tier POSIX process group containment harness with kernel-enforced capability drops and seccomp BPF filtering." In reality, the codebase contains only a basic `subprocess.Popen` call without process group isolation.
- **The Consequence**: A later agent that reads this documentation treats the phantom capabilities as verified ground truth and tries to configure parameters and flags that do not exist.

### 2.2 The Self-Consistency vs. Conformance Trap (Observation 19 Link)
As established in [Observation 19](../systems/19-self-consistency-is-not-conformance.md), internal semantic coherence does not equal external contract conformance.
When agents author the code, the unit tests, and the documentation in an unconstrained loop, the system reaches 100% internal agreement:
- The docstring asserts a function returns a structured dictionary `{"status": str, "payload": dict}`.
- The unit test asserts `isinstance(result, dict)`.
- The implementation returns `{"status": "ok", "payload": {}}`.
- **The Defect**: External upstream APIs or runtime CLI consumers actually supply raw newline-delimited JSON strings. The agent-authored system achieved 100% internal self-consistency while completely disconnecting from real external runtime physical constraints.

### 2.3 The Tragedy of Attention Dilution ($ADI$) & Invariant Extinction (Observation 23 Link)
Conscientious agents attempt to be helpful by thoroughly documenting every private helper function, alternative design pattern, and nuance in long narrative prose.
Under the Attention Dilution Index formulated in [Observation 23](../systems/23-attention-dilution-context-rot-and-active-compaction.md):

$$ADI = \left( \frac{N_{\text{noise}}}{N_{\text{signal}}} \right) \times \left( \frac{N_{\text{total}}}{10\,000} \right)$$

As narrative prose floods the agent's context window:
1. Total token depth $N_{\text{total}}$ expands rapidly.
2. Signal-to-noise ratio collapses.
3. Crucial negative constraints (e.g. *"NEVER push without passing devops ci"*, *"NEVER leak RFC 1918 IPs"*) move into the middle of the context, where models use information less well (**Lost in the Middle**, Liu et al., TACL 2024).
4. **Invariant Extinction can occur**: Descendant agents can violate primary architectural and security invariants while meticulously adhering to verbose narrative formatting conventions.

### 2.4 CommonMark Linebreak Degradation
In CommonMark and GitHub Flavored Markdown, hard line breaks require two trailing spaces (`  \n`) or a backslash. Unconfigured whitespace clean-up hooks (`trailing-whitespace`) frequently strip intentional double spaces, corrupting blockquote lists and rendering unreadable paragraphs. Conversely, agents accidentally introduce single trailing spaces, which fail to render as line breaks and trip strict git whitespace audits.

---

## 3. The Underlying Failure Mode or Catalyst

The root causes of self-documentation breakdown stem from the intersection of generative language models and stateless agent lifecycles:

1. **Epigenetic Mutation in Agent Cognitive Lineage**:
   In human teams, documentation is passive history. In an agentic loop, **documentation is causal prompt DNA**. A single inaccurate statement or hallucinated invariant committed to a repository document directly alters the cognitive priors of all subsequent agent sessions.
2. **Convention Degradation Under Autonomous Iteration (Observation 08 Link)**:
   Prose documentation that specifies coding standards or documentation rules without mechanical invariant enforcement inevitably degrades over successive agent runs. As shown in [Observation 08](../systems/08-convention-to-mechanical-enforcement-inversion.md), documented conventions act merely as probabilistic suggestions; deterministic linters and pre-commit gates enforce what they check.
3. **Multi-Agent Epistemic Loss (Observation 09 Link)**:
   When subagents summarize their actions for parent orchestrators, nuanced technical caveats are lost. If the parent agent documents the system based solely on subagent summaries, the documentation records an idealized abstraction rather than physical codebase reality.

---

## 4. Remediation & Architectural Pattern

To reduce phantom architectures, circular belief traps, and context rot, engineering systems must implement the **Four-Tier Living Documentation Architecture**:

```mermaid
flowchart LR
    subgraph S1 ["Authoring Tiers (Inputs)"]
        direction TB
        T1["Tier 1: AST Mechanical Reflection<br/>• Deterministic CLI & Manifest Extraction"]
        T2["Tier 2: Constrained Synthesis<br/>• Bounded ADRs & Verification Rationale"]
    end

    subgraph S2 ["Tier 3: Invariant Gating (Fail-Closed)"]
        direction TB
        T3_Val["tools/docs_validator.py & Pre-Commit"]
        T3_Rules["• Link & Anchor Integrity<br/>• Bounded Mermaid Ratios (1:3 to 3:1)<br/>• CommonMark Linebreaks (DOC012)<br/>• Zero-Trust Egress Sanitization"]
        T3_Val --> T3_Rules
    end

    subgraph S3 ["Tier 4: Context Optimization (Delivery)"]
        direction TB
        T4_Comp["Active Compaction & Liveness Pruner"]
        T4_Ctx["High-Signal Context Grounding"]
        T4_Comp --> T4_Ctx
    end

    S1 -->|"Static Extraction & ADRs"| S2
    S2 -->|"Verified Documentation"| S3
    S3 --> NextAgent["Next Agent Generation<br/>(Grounded in Code)"]
```

### The Core Architectural Principles:

1. **The Iron Law of Mechanical Introspection**:
   *Never allow an LLM to generate what an AST parser, Click/Typer introspection engine, or reflection tool can extract.* Public CLI reference tables, argument types, and FastMCP schemas must be generated deterministically via code introspection (for example `src/devops_cli/docs/generator.py` in devops-cli), so the tables match the code they are generated from.
2. **Constrained Generative Synthesis for Intent**:
   LLMs must author only the **"Why"**—architectural decision records, problem framing, and trade-off analyses. Generative outputs must adhere to rigid markdown templates and embed verifiable terminal outputs and empirical benchmarks.
3. **Mechanical Invariant Gating (`tools/docs_validator.py`)**:
   Documentation must be linted and tested with the same rigor as production code:
   - **`DOC001` - `DOC011`**: Code fences, Mermaid AST syntax, table column alignment, link resolution, code snippets, paired HTML tags, structural sections, directory maps, and sanitization.
   - **`DOC012` (CommonMark Linebreak Hygiene)**: Mechanically validates that hard line breaks use valid two-space formatting, rejects accidental single trailing spaces outside fences, and configures `--markdown-linebreak-ext=md` in pre-commit hooks.
4. **Active Compaction & Liveness Pruning**:
   Keep repository documentation rich for git history, but compact aggressively when assembling agent system prompts. Prune transient execution logs, enforce bounded string caps ($\le 256$ chars), and quarantine scratch scripts to lower the Attention Dilution Index, an uncalibrated heuristic ([Observation 23](../systems/23-attention-dilution-context-rot-and-active-compaction.md)).

---

## 5. Verifiable Impact & Key Takeaways

Parts of this architecture exist in both repositories: generated CLI reference tables in `devops-cli` (Tier 1) and `tools/docs_validator.py` with rules `DOC001` to `DOC014` in `vibes` (Tier 3). No before-and-after measurement was made, so no impact figures are given. The validator's link and linebreak rules are checks, not measurements: they report each breach, but they do not say how much drift they prevented.

### Key Takeaways for Agentic Engineering Teams:
- **Documentation is Causal Prompt Code**: Errors in documentation permanently pollute the context of future agent generations. Treat documentation updates with the exact same review rigor and testing gates as core application logic.
- **Separation of Extraction and Synthesis**: Use deterministic AST tools for interface mechanics; use LLMs solely for design rationale and architectural trade-offs.
- **Fail-Closed Pre-Commit Enforcement**: Every documentation invariant must be checked locally via pre-commit hooks (`docs_validator.py`, about 0.5 s for one file on a development machine) before changes reach CI or remote branches.

