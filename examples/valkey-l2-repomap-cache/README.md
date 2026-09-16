# Valkey L2 Repomap Cache & Embedding Drift Auditor

High-throughput, two-tier AST symbol table caching and semantic drift auditing across autonomous AI subagent invocations.

---

## Architecture Overview

Multi-turn agent coding sessions frequently re-parse, inspect, and evaluate identical or slightly modified source files. Re-parsing the AST on every turn consumes valuable CPU cycles and introduces token-budget context latency.

This reference implementation combines:
1. **L1 Fast Memory Cache**: Microsecond in-process LRU cache keyed by SHA-256 content hash.
2. **L2 Valkey / Redis Distributed Cache**: Content-addressed cache over RESP wire protocol with TTL expiration.
3. **AST Feature Embedding Generator**: Generates an 8-dimensional normalized structural feature vector from module AST metrics.
4. **Embedding Drift Auditor**: Calculates Cosine Similarity and Cosine Distance ($D_C$) between AST versions to ensure that automated refactorings preserve structural semantics without unintended degradation.

```mermaid
flowchart TD
    Req["Source File / Diff Input"] --> HASH["Compute Content SHA-256 Digest"]
    HASH --> L1{"Check L1 Memory Cache"}
    L1 -->|L1 Hit (< 0.1ms)| Ret["Return Cached Repomap"]
    L1 -->|L1 Miss| L2{"Check L2 Valkey Cache"}
    L2 -->|L2 Hit (< 2ms)| Hydrate["Hydrate Record -> Populate L1"] --> Ret
    L2 -->|L2 Miss| Parse["Parse AST & Extract Symbols"]
    Parse --> Vector["Compute Normalized Structural Embedding"]
    Vector --> Store["Write to L1 & L2 (TTL=86400s)"] --> Ret
```

---

## Semantic Drift Auditing

When refactoring code (e.g. converting branching ladders into table-driven dispatch), agents must verify that the structural semantics of the module are preserved.

The auditor extracts an 8-dimensional feature vector $\vec{v}$:
$$\vec{v} = \begin{bmatrix} \frac{\text{functions}}{\text{lines}}, \frac{\text{classes}}{\text{lines}}, \frac{\text{branches}}{\text{lines}}, \frac{\text{calls}}{\text{lines}}, \frac{\text{returns}}{\text{lines}}, \frac{\text{docstrings}}{\text{symbols}}, \ln(1 + \text{branches}), \ln(1 + \text{lines}) \end{bmatrix}$$

Normalized to unit length $\hat{v} = \frac{\vec{v}}{\|\vec{v}\|_2}$, the auditor computes Cosine Distance:
$$D_C(\hat{u}, \hat{v}) = 1.0 - (\hat{u} \cdot \hat{v})$$

### Drift Classification
- **Preserved ($D_C \le 0.05$)**: Expected behavior during clean refactoring. Symbol topology remains stable.
- **Slight Drift ($0.05 < D_C \le 0.15$)**: Moderate structural additions (e.g. added helper functions).
- **Significant Drift ($D_C > 0.15$)**: Major topological divergence (e.g. paradigm shift from functional to OOP).

---

## Wire Protocol (RESP)

The client implements pure standard-library Redis Serialization Protocol (RESP) without third-party dependencies:
- PING / GET / SET / DEL commands encoded via `*<n>\r\n$<len>\r\n...`
- Multi-tier in-memory fallback when no local Valkey daemon is running, guaranteeing 100% CI pass rate in offline environments.

---

## Running the Interactive Demo & Tests

```bash
# Run interactive caching and drift audit demo
python3 repomap_cache.py --demo

# Audit drift between two files
python3 repomap_cache.py --file before.py --refactored after.py

# Run unit tests
pytest test_repomap_cache.py -v
```
