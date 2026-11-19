# Pattern: Invariant-Grounded Recursive Self-Improvement

> **Pattern Class**: Architecture & Autonomous Governance  
> **Problem**: Static codebases decay in the post-harness era, but unconstrained recursive self-improvement collapses into phantom architectures, circular self-consistency traps, and attention dilution  
> **Solution**: A tripartite architecture binding deterministic AST invariants, counterexample-guided regression accumulation, and outward landscape foraging into a convergent, closed-loop self-evolution engine  
> **TLDR**: Anchor recursive agent self-improvement in AST invariant gates, counterexample test accumulation, and outward landscape foraging.
> **ELI:7b**: Let AI improve its own code and tools, but only allow changes that pass strict math checks, pass all tests, and beat external alternatives.
> **Reference Implementation**: [`tools/resource_iteration_workbench.py`](../tools/resource_iteration_workbench.py), [`tools/landscape_survey.py`](../tools/landscape_survey.py)

---

## 1. Problem Statement

In the post-agentic-harness era, the velocity of software generation outstrips human auditing capacity. If a codebase relies solely on human engineers to identify architectural decay, refactor procedural bloat, and track upstream evolutions, it accumulates technical debt faster than it can be addressed.

Conversely, attempting autonomous recursive self-improvement (RSI) without mechanical checks fails catastrophically:
1. **The Phantom Architecture Trap**: Language models hallucinate idealized capabilities in documentation and tests, creating self-reinforcing loops of synthetic non-functionality ([Observation 27](../observations/systems/27-agentic-project-self-documentation-and-the-phantom-architecture-trap.md)).
2. **Circular Self-Consistency**: Internal unit test suites pass completely against internal mock models while diverging from real-world wire protocols, kernel APIs, and network behaviors ([Observation 19](../observations/systems/19-self-consistency-is-not-conformance.md)).
3. **Defect-Shaped Convergence**: Systems that only run linters converge to a myopic dead-end: zero open issues on a static codebase that nobody uses ([Observation 14](../observations/systems/14-defect-shaped-loops-and-the-feature-blind-spot.md)).

To scale safely, self-improvement must be anchored to **unforgiving, asymmetric mechanical invariants**.

---

## 2. Core Mechanics

The Invariant-Grounded RSI pattern coordinates three interconnected feedback loops:

```mermaid
flowchart LR
    subgraph Discovery ["1. Horizon Discovery"]
        direction TB
        LS["tools/landscape_survey.py<br/>Upstream & Registry Ingestion"]
        RM["docs/ROADMAP.md<br/>Value-Driven Deliverables"]
        LS --> RM
    end

    subgraph Synthesis ["2. Autonomous Synthesis"]
        direction TB
        Agent["Multi-Agent Swarm Orchestrator"]
        Candidate["Code & Test Candidate Diffs"]
        Agent --> Candidate
    end

    subgraph Verification ["3. Invariant Gating"]
        direction TB
        AST["AST Sentinel (M &le; 10, Depth &le; 5)"]
        CEGIS["Monotonic Counterexample Regression Corpus"]
        AST --> CEGIS
    end

    Discovery -->|"Roadmap Ingestion"| Synthesis
    Synthesis -->|"Candidate Diff"| Verification
    Verification -->|"Reject with Counterexample"| Synthesis
    Verification -->|"Pass: Reconcile Headroom"| Discovery
```

### The Three Structural Pillars:

1. **Asymmetric Invariant Gatekeeping (checking is cheaper than generating)**:
   Code generation is treated as probabilistic search. Every proposed modification must pass an array of deterministic, cheap oracles before landing. This is an analogy to the asymmetry behind $P$ vs $NP$, not an application of it: nothing here is a complexity-class result.
   - **AST Structural Invariants**: McCabe cyclomatic complexity $M \le 10$, indentation depth $\le 5$.
   - **Zero-Trust Egress Guards**: Rejection of private RFC 1918 IPs, internal DNS suffixes, and plain-text secrets.
   - **Coverage & Test Floors**: Strict $\ge 90.0\%$ line coverage (`--cov-fail-under=90` in CI). Deprecation warnings are not a gate here: `pytest.ini` ignores them.
2. **Monotonic Constraint Accumulation (counterexample accumulation)**:
   Every bug, race condition, or fuzzing crash is permanently converted into a regression fixture. The regression corpus must only accumulate; a self-improving agent must not drop or weaken existing test cases to make a build pass. No check in this repository blocks the removal of a corpus case, so review enforces this rule.
3. **Outward Telemetry & Horizon Expansion**:
   The self-improving loop reads outward sources to ingest forward-looking roadmap items, preventing defect-shaped stagnation. In this repository, `tools/landscape_survey.py` fetches GitHub facts for a curated list of comparable projects, and `tools/semconv_snapshot.py` snapshots the OpenTelemetry GenAI semantic conventions at a pinned commit.

---

## 3. Implementation Blueprint

### Step 1: Deterministic AST Invariant Verification (sketch of [`examples/ast-invariant-sentinel/sentinel.py`](../examples/ast-invariant-sentinel/sentinel.py))
```python
import ast
from pathlib import Path

def audit_function_complexity(node: ast.FunctionDef, max_m: int = 10, max_depth: int = 5) -> None:
    """Enforce mathematical complexity caps across all synthesized routines."""
    complexity = 1 + sum(
        1 for n in ast.walk(node)
        if isinstance(n, (ast.If, ast.While, ast.For, ast.ExceptHandler, ast.Assert))
    )
    if complexity > max_m:
        raise AssertionError(f"Function {node.name} breached complexity cap: {complexity} > {max_m}")
```

### Step 2: Monotonic Regression Corpus Replay (sketch of `replay` in [`tools/fuzz_harness.py`](../tools/fuzz_harness.py))
```python
def replay_regression_corpus(corpus_dir: Path) -> None:
    """Fail if any recorded crash input crashes the target again."""
    for case_file in corpus_dir.glob("*.case"):
        payload = case_file.read_bytes()
        # Assert deterministic handling without crashes, timeouts, or leaks
        execute_isolated_target(payload)
```

---

## 4. Prescriptive Operational Guardrails

- **The Grounding Rule**: Never permit an agent to evaluate its own output without an external oracle (compiler, AST visitor, OS sandbox, or reference benchmark).
- **The Outward Balance Rule**: Maintain a balanced portfolio between quality instruments and product capabilities (`python3 tools/portfolio_balance.py`). A loop that only fixes defects is a failing loop.
- **The Monotonicity Rule**: Regression tests and invariant thresholds may only be tightened, never relaxed or waived without audited justifications (`# sentinel: allow[...]`).
- **The Visual Proportion Rule**: Diagrams generated during self-documentation must stay within the **$1:3$ to $3:1$ aspect ratio ceiling** to prevent cognitive overload.

---

## 5. Key Takeaways & Architectural Relevance

1. **Recursive Self-Improvement is a Mandate**: In a world of instantaneous code synthesis, static codebases suffer fatal relative decay.
2. **The Invariant Architect**: The human role evolves from writing procedural syntax to curating the objective functions, invariant gates, and counterexample regression corpora that constrain autonomous evolution.
3. **Internal Consistency $\neq$ Conformance**: Continuous external probing is the only antidote to autonomous model echo chambers.
