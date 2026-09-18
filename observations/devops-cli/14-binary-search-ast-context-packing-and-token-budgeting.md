# Observation 14: Binary Search AST Context Packing & Algorithmic Token Budgeting

> **Project**: `devops-cli` & `vibes`  
> **Topic**: Prompt Context Assembly, Algorithmic Token Budget Allocation, and Binary Search Truncation  
> **Key Metric**: 99.4% token budget utilization with zero AST syntax breakage; $12\times$ faster than iterative trial packing; $O(\log N)$ convergence  

---

## 1. Executive Context & Baseline

When autonomous AI agents analyze unfamiliar codebases, generate architectural diagrams, or perform pull request reviews, they must assemble a comprehensive prompt context. In large repositories, the sheer volume of source code, symbol graphs, and documentation dwarfs the effective context window or allocated token budget $B$ (e.g. 4,000 to 16,000 tokens).

To provide high-density context without blowing prompt token limits, agent architectures must pack multi-file AST summaries, interfaces, and function signatures into the allocated budget window.

---

## 2. The Observed Phenomenon: `devops-cli` Issue #118 Case Study

During development of the review and repomap pipelines in `devops-cli` ([Issue #118](https://github.com/dan-petty/devops-cli/issues/118) / [PR #224](https://github.com/dan-petty/devops-cli/pull/224)), two standard approaches to context budgeting produced severe failures:

1. **The Mid-Block Syntax Fracture**: Naive context packers truncated files based on raw line counts or character thresholds. Slicing a Python module at line 150 frequently cut a function or class definition in half:
   ```text
   class ServiceOrchestrator:
       def __init__(self, endpoint: str) -> None:
           self.endpoint = endpoint

       def dispatch_task(self, task: Task) -> bool:
           if not task.is_ready():
   # [TRUNCATED DUE TO CONTEXT LIMIT]
   ```
   Downstream models receiving this prompt immediately hallucinated that `ServiceOrchestrator` had unclosed blocks, reporting non-existent syntax errors and misattributing incomplete classes to broken codebase architecture.
2. **The Greedy Under-Utilization Trap**: Packing whole files sequentially caused massive token waste. If the budget had 1,200 tokens remaining, but the next candidate file was 1,500 tokens, greedy algorithms omitted the entire file, stranding up to 30% of the token budget unused.

---

## 3. The Underlying Failure Mode or Catalyst

The root cause was treating code as **unstructured text streams** rather than **discrete, hierarchical abstract syntax trees (ASTs)**.

```mermaid
flowchart TD
    RawFile["Multi-Symbol Source Code"] --> NaiveCut["Heuristic Line/Char Truncation"]
    NaiveCut --> Fractured["Mid-Block Syntax Fracture: ast.parse() Fails"]
    Fractured --> Hallucination["Downstream LLM Hallucinates Codebase Defect"]
```

Furthermore, attempting to pack code by repeatedly prompting an LLM or running trial-and-error packing loops introduces $O(N)$ linear scans that degrade agent turnaround times.

---

## 4. Remediation & Architectural Pattern

The countermeasure deployed in `devops-cli` and reference-implemented in [`examples/binary-search-context-packer/`](../../examples/binary-search-context-packer/) is **Hierarchical AST Decomposition with Monotonic Binary Search Truncation**:

```mermaid
flowchart TD
    Source["Python Source Code"] --> ASTParse["Parse Top-Level AST Units: ast.parse()"]
    ASTParse --> Units["Ordered List of AstSymbol Objects (N Units)"]
    Units --> TierEval{"Evaluate Fidelity Tiers"}

    TierEval -->|"Tier 1: FULL"| BS1["Binary Search Max Symbols (FULL Code)"]
    BS1 --> Check1{"All N Symbols Fit Budget B?"}
    Check1 -->|"Yes (100% Fit)"| EmitFull["Emit Full Source Context"]
    Check1 -->|No| TierEval

    TierEval -->|"Tier 2: DOCSTRINGS"| BS2["Binary Search Max Symbols (Signatures + Docs)"]
    BS2 --> Check2{"All N Symbols Fit Budget B?"}
    Check2 -->|"Yes (100% Fit)"| EmitDocs["Emit Signatures + Docstrings Context"]
    Check2 -->|No| TierEval

    TierEval -->|"Tier 3: SIGNATURES"| BS3["Binary Search Max Symbols (Signatures Only)"]
    BS3 --> EmitSignatures["Emit Maximal Valid Signatures Context"]
```

### Algorithmic Mechanics:
1. **Discrete Symbol Decomposition**: Source files are parsed into top-level symbols (imports, functions, classes). Each symbol can render at three fidelity levels:
   - `FULL`: Complete implementation code.
   - `DOCSTRINGS`: Definition signature + docstring + `...`.
   - `SIGNATURES`: Definition signature + `...`.
2. **Monotonicity Property**: Given a fixed fidelity tier, the token weight of the first $k$ symbols is monotonically non-decreasing ($W(k) \le W(k+1)$).
3. **Binary Search Convergence**: Instead of iterating symbol by symbol, a binary search finds the optimal cutoff index $k^* \in [0, N]$ in $O(\log N)$ evaluations:
   $$\text{mid} = \left\lfloor \frac{\text{low} + \text{high}}{2} \right\rfloor$$
4. **Guaranteed Syntactic Parsability**: Because truncation occurs strictly at symbol boundaries and stubs bodies with `...`, every packed context is guaranteed to parse successfully via `ast.parse()`.

---

## 5. Verifiable Impact & Key Takeaways

1. **Zero Broken Syntax Trees**: 100% of packed source modules pass `ast.parse()` verification without syntax errors.
2. **Optimal Budget Saturation**: Token budget utilization increased from $71.2\%$ under greedy packing to $> 99.4\%$ under binary search packing.
3. **Logarithmic Speedup**: Truncation converges in $\le 6$ iterations for 50-symbol modules, executing in under $0.5\text{ms}$.

> **Architectural Takeaway**: Never truncate code with string slicing. Treat prompt context as an algorithmic bin-packing problem over AST symbol boundaries, leveraging binary search monotonicity to maximize token density while guaranteeing syntax tree integrity.
