# High-Performance AST Context Packer with Binary Search Truncation

Reference implementation of an algorithmic prompt context packing engine that packs the top-level imports, functions and classes of a Python source file into a token budget without producing truncated function/class blocks. The budget is in estimated tokens (UTF-8 bytes divided by 4); no tokenizer is called. Other top-level statements are dropped.

---

## 🏛️ The Problem with Naive Context Truncation

When autonomous agents assemble prompt context across multi-file repositories (repomaps, architectural outlines, review context), they face strict context window budgets ($B$ tokens).

Two failure modes consistently degrade LLM reasoning:
1. **The Mid-Block Syntax Fracture**: Arbitrary line truncation cuts a function or class in half. Downstream language models receive malformed Python code, triggering hallucinated syntax error reports or misattributing missing closing braces to developer bugs.
2. **The Greedy Packing Gap**: Greedily packing whole files in sequence either leaves massive token gaps (when the next file is slightly too large to fit the remaining budget) or overshoots the budget, triggering API context length rejection.

---

## 🧭 Binary Search Convergence Architecture

Rather than treating source code as flat text, the engine parses code into top-level structural units (`AstSymbol`) using Python's `ast` module. It then evaluates three hierarchical fidelity tiers (`FULL`, `DOCSTRINGS`, `SIGNATURES`) and executes a monotonic binary search to maximize token utilization in $O(\log N)$ iterations.

The output is meant to parse as Python, and the test suite checks that for its sample sources. It is not guaranteed: a one-line `def` with an annotated parameter (`def f(x: int): return x`) is cut at its first colon, and its DOCSTRINGS and SIGNATURES renderings do not parse.

```mermaid
flowchart TD
    Source["Python Source Code"] --> Parse["Parse into Top-Level Symbols: ast.parse()"]
    Parse --> CheckTier{"Evaluate Fidelity Tier"}
    CheckTier -->|"1. FULL"| BinarySearch1["Binary Search Max Symbols in FULL tier"]
    CheckTier -->|"2. DOCSTRINGS"| BinarySearch2["Binary Search Max Symbols in DOCSTRINGS tier"]
    CheckTier -->|"3. SIGNATURES"| BinarySearch3["Binary Search Max Symbols in SIGNATURES tier"]

    BinarySearch1 --> CountCheck1{"All symbols fit within Budget B?"}
    CountCheck1 -->|"Yes (100% Symbols Fit)"| EmitFull["Return Full Source Context"]
    CountCheck1 -->|No| CheckTier

    BinarySearch2 --> CountCheck2{"All symbols fit within Budget B?"}
    CountCheck2 -->|"Yes (100% Symbols Fit)"| EmitDocstrings["Return Signatures + Docstrings Context"]
    CountCheck2 -->|No| CheckTier

    BinarySearch3 --> EmitSignatures["Return Maximal Signatures Context"]
```

---

## 🚀 Usage

### Packing to an Estimated Token Budget

```python
from context_packer import pack_source_to_budget

source_code = Path("my_module.py").read_text()
budget_tokens = 500

# Packs source code to fit budget, degrading gracefully from FULL -> DOCSTRINGS -> SIGNATURES
rendered_context, fidelity_level, symbol_count = pack_source_to_budget(
    source_code,
    budget_tokens=budget_tokens,
)

print(f"Packed {symbol_count} symbols at {fidelity_level} fidelity.")
```

---

## 🧪 Testing

Run the isolated test suite:
```bash
pytest examples/binary-search-context-packer/test_context_packer.py
```
