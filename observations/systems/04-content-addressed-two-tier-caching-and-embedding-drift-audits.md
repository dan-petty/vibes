# Observation 04 (Systems): Content-Addressed Two-Tier Caching & Embedding Drift Audits

> **Project**: `devops-cli` & `vibes`  
> **Topic**: Multi-Tier AST Repomap Caching (L1 Fast Memory + L2 Valkey Distributed Cache) & Structural Embedding Drift Auditing  
> **Key Metric**: L1 hit in about 0.1 ms with the source supplied (1,863-line file, 2026-11-19); content-addressed SHA-256 cache keys; cosine distance ($D_C$) over 99 shape, operator and identifier-name features, which does not detect behavior change  
> **TLDR**: Implement two-tier content-addressed caching (L1 memory + L2 Valkey) with a cosine-distance check of coarse structural features.
> **ELI:7b**: Cache repeated search results by content hash, and check embedding scores so the AI's search index doesn't drift out of date.

---

## 1. Executive Context & The AST Parsing Tax

In autonomous multi-agent software engineering, repository context is king. Before planning a change, refactoring code, or verifying invariants, agents must build a mental repomap of symbol definitions, function signatures, class hierarchies, and docstrings.

However, across long-running, multi-turn subagent sessions:
1. **The Parsing Latency Tax**: Parsing hundreds of Python source files using `ast.parse` on every subagent turn consumes significant CPU time and creates cognitive friction in conversational workflows.
2. **The Fragility of Filepath & Mtime Caching**: Standard caching mechanisms keyed by file path or modification timestamp (`st_mtime`) break down in agent environments. When agents checkout branches, apply git stashes, or perform test runs that touch timestamps without altering content, timestamp-based caches suffer from cache thrashing or serve stale data.
3. **Semantic Drift During Automated Refactoring**: When an automated refactoring engine transforms procedural code (e.g. decomposing branching ladders into table dispatch), a reviewer needs evidence that the module's behavior was preserved. A structural feature vector cannot supply that evidence; tests and equivalence checks can.

---

## 2. The Observed Phenomenon: Content-Addressed Multi-Tier Architecture

To eliminate redundant AST parsing and audit structural integrity, we implemented **Valkey L2 Repomap Cache & Embedding Drift Auditor (`examples/valkey-l2-repomap-cache/`)**:

```mermaid
flowchart TD
    Req["Target Source File / Subagent AST Query"] --> HASH["Compute Content SHA-256 Digest"]
    HASH --> KEY["Generate Content-Addressed Key: repomap:v2:<sha256>"]

    KEY --> L1{"L1 Fast Memory Cache<br/>(In-Process, LRU eviction)"}
    L1 -->|"L1 Hit"| Serve["Serve RepomapRecord & Symbols"]
    L1 -->|L1 Miss| L2{"L2 Distributed Cache<br/>(Valkey / Redis RESP)"}

    L2 -->|"L2 Hit (Valkey or reported fallback)"| Hydrate["Hydrate Record -> Populate L1"] --> Serve
    L2 -->|L2 Miss| Compute["Parse AST & Extract Symbols"]

    Compute --> Embed["Compute Shape, Operator and Name Vector"]
    Embed --> Store["Write to L1 & L2 (TTL = 86400s)"] --> Serve

    Serve --> Drift{"Refactoring Drift Auditor"}
    Drift --> Cosine["Compute Cosine Distance: Dc = 1.0 - (u · v)"]
    Cosine -->|"Dc <= 0.05"| OK["Verdict: PRESERVED (structure only)"]
    Cosine -->|"Dc > 0.15"| Warn["Verdict: SIGNIFICANT_DRIFT (Review Invariants)"]
```

### 1. Content-Addressed Keying (`repomap:v2:<sha256>`)
Instead of keying cache entries by volatile paths (`repomap:/path/to/file.py`), the cache keys strictly by the SHA-256 digest of the source code content. The `v2` segment is the record format version. A change to the vector changes the version, so a cached record of one length is never compared with a vector of another length:
- If a file is touched without content changes (only its mtime moves), the digest is identical, resulting in a cache hit. A reformat changes the bytes and therefore the digest.
- If a single character changes, the digest changes, guaranteeing that stale AST symbols are never served.
- Previous file revisions remain cached under their respective digests until they are evicted (L1 holds 128 entries) or expire (L2 TTL of 24 hours), so a rollback or a diff between commits can skip re-parsing.

### 2. Two-Tier Hierarchy
- **L1 In-Process Memory Cache**: `collections.OrderedDict` lookup with least-recently-used eviction at 128 entries. A hit moves the entry to the most-recently-used end (`move_to_end`), and an insert over capacity evicts the oldest entry (`popitem(last=False)`). Each lookup still hashes the full source with SHA-256.
- **L2 Valkey Distributed Cache**: Redis Serialization Protocol (RESP) wire cache accessible across parallel subagent processes and persistent across CLI sessions.

---

## 3. Mathematical Formulation: Shape, Operator and Name Feature Vector

To compare the structure of two versions of a module, the engine computes a normalized structural feature vector $\hat{v}$ of three blocks, 99 features in total. It is a hand-built feature vector, not a learned embedding:

$$\vec{s} = \begin{bmatrix}
\frac{\text{functions}}{\text{lines}}, &
\frac{\text{classes}}{\text{lines}}, &
\frac{\text{branches}}{\text{lines}}, &
\frac{\text{calls}}{\text{lines}}, &
\frac{\text{returns}}{\text{lines}}, &
\frac{\text{docstrings}}{\text{symbols}}
\end{bmatrix}$$

- $\vec{o}$ holds one count for each of the 29 AST operator types. `==` (`Eq`) and `!=` (`NotEq`) are different features.
- $\vec{n}$ holds 64 buckets. Each variable, attribute, argument, function and class name adds 1 to the bucket of its CRC-32 hash.

Each block is scaled to unit length, and the joined vector is scaled to unit length again:
$$\hat{v} = \frac{[\hat{s}, \hat{o}, \hat{n}]}{\|[\hat{s}, \hat{o}, \hat{n}]\|_2}$$

When all three blocks are non-zero, the cosine of two such vectors is the mean of the three block cosines. Thus no block can hide the others.

### Cosine Distance & Drift Thresholds
Given baseline vector $\hat{u}$ (original file) and candidate vector $\hat{v}$ (refactored file), the auditor calculates:
$$D_C(\hat{u}, \hat{v}) = 1.0 - (\hat{u} \cdot \hat{v})$$

| Cosine Distance ($D_C$) | Classification | Architectural Meaning |
|---|---|---|
| **$D_C \le 0.05$** | `PRESERVED` | Clean refactoring. Structural density and symbol topology remain consistent. |
| **$0.05 < D_C \le 0.15$** | `SLIGHT_DRIFT` | Minor architectural addition (e.g. extracted helper functions or dispatch dictionary). |
| **$D_C > 0.15$** | `SIGNIFICANT_DRIFT` | Major topological divergence (e.g. paradigm shift from procedural to OOP, or dropped functionality). |

The unit test `test_embedding_drift_auditor_preserved` extracts a helper (`_compute_mean`) from a short function and asserts $D_C \le 0.05$. The computed value is $D_C = 0.0184$.

The earlier 8-feature vector had the six shape features and two more terms, $\ln(1 + \text{branches})$ and $\ln(1 + \text{lines})$. For any file longer than a few lines, the two logarithmic terms dominated the vector, so most pairs of files scored close to zero. On 2026-11-19, with that vector:
- Replacing every `==` with `!=` in `tools/sarif_report.py` gave $D_C = 0.0$.
- Three unrelated modules (`tools/sarif_report.py`, `tools/docs_validator.py` and `examples/adaptive-web-crawler/crawler.py`) scored `PRESERVED` against each other, with $D_C$ from 0.0025 to 0.0099.
- All 780 pairs of the first 40 modules in `tools/` scored `PRESERVED`.

Adding the operator counts and one name density (names per line) to the same single vector was not enough. It left 765 of the 780 pairs `PRESERVED`, because the name density then dominated the vector. Scaling each block on its own removed the domination. With the three-block vector, on the same day:
- The same `==` to `!=` change gives $D_C = 0.071$.
- The three unrelated modules give $D_C$ from 0.137 to 0.222 (`SLIGHT_DRIFT` or `SIGNIFICANT_DRIFT`).
- No pair of the 40 modules scores `PRESERVED` (smallest $D_C = 0.069$, median 0.221).
- 27 of 27 modules compared with their version 80 commits earlier still score `PRESERVED` (largest $D_C = 0.024$).

The metric still does not detect behavior change. Changing `return a - b` to `return b - a` gives $D_C = 0.0$, because the counts do not change.

---

## 4. Pure Standard-Library RESP Wire Protocol & Mock Fallback

To ensure that `vibes` and `devops-cli` run hermetically in offline, rootless, and minimal CI environments:
- The Valkey client implements native RESP serialization using the pure Python standard library (`socket`).
- If no local Valkey daemon answers `PING` on `localhost:6379`, the client moves to an in-process dictionary fallback store (`_fallback_store: dict[str, str]`). A later failed command, or a connection that closes before a complete reply, also moves it. It stays on the fallback store for the rest of its life, so reads and writes do not split between two stores.
- The move is never silent. The client logs one warning and keeps the reason in `fallback_reason`. The cache reports each hit from the fallback store as `L2_FALLBACK`, not `L2_VALKEY`, and counts it in `stats["l2_fallback_hits"]`. Until 2026-11-19 the fallback was silent, and a hit from the dict was reported as `L2_VALKEY`.
- The tests therefore need no running Valkey. A RESP server double in the tests covers the connected path and the failed `GET` and `SET`.

---

## 5. Key Engineering Recommendations

1. **Content-Address All AST & Repomap Caches by SHA-256**: Never cache symbol tables by file path or modification timestamp. Content hashing eliminates stale cache reads across git branches and agent edits.
2. **Combine In-Process L1 and Distributed L2 Caching**: Use fast in-memory LRU caches for hot subagent turns, backed by Valkey/Redis L2 caches for cross-agent and cross-session persistence.
3. **Audit Structural Drift with Cosine Distance**: Compute normalized topological feature vectors before and after automated code refactoring. Flag any transformation where $D_C > 0.15$ for manual review. Do not read a low $D_C$ as evidence that behavior is preserved.
4. **Build In-Memory Fallbacks for Network Protocols, and Report Them**: Never let an external service dependency (Valkey, Redis, Jaeger) break test suites. Provide zero-config in-memory fallbacks, and make each one visible: log once, record the reason, and give a fallback hit its own tier. A silent fallback reads as a healthy cache.
