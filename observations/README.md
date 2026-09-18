# Consolidated Field Observations & Architectural Synthesis

> **Exhibition**: `vibes` Empirical Knowledge Base
> **Classification**: Master Observation Index & Cross-Domain Synthesis
> **Scope**: 25 Empirical Case Studies across `devops-cli`, `polyglot`, and `systems`
> **Key Metric**: 100.0/100 Resource Health Score; 217/217 passing tests; $M \le 6$ and depth $\le 3$ headroom; 100% CIS Rootless Container Benchmark compliance

---

## 🏛️ Executive Summary: The Anatomy of Disciplined Agentic Engineering

The `observations/` directory records the empirical reality of autonomous software engineering performed by AI coding assistants. Across hundreds of autonomous sessions, pull requests, refactoring cycles, and benchmark evaluations in [`devops-cli`](https://github.com/dan-petty/devops-cli) and [`vibes`](https://github.com/dan-petty/vibes), these studies capture how stochastic language models behave when confronted with real-world engineering constraints.

The fundamental insight across all 25 observations is simple yet profound:

> **Stochastic token generation without mechanical boundary oracles collapses into structural entropy. Unbounded models drift into procedural spaghetti, hallucinated tool arguments, orphaned background processes, and brittle heuristic traps. When bounded by deterministic AST invariants, formal contracts, and closed-loop feedback engines, agents achieve architectural excellence, sub-second feedback loops, and 100% test reliability.**

```mermaid
flowchart TD
    subgraph CognitiveFailures["Stochastic Cognitive Failure Modes"]
        F1["Unconstrained Code Hallucination"]
        F2["Procedural Branch Explosion (M > 10)"]
        F3["Permissive Tool Schema Hallucination"]
        F4["Orphaned Grandchild Process Leaks"]
        F5["Minified Polyglot OOM Traps (CWE-400)"]
        F6["Goroutine Channel Abandonment"]
        F7["Brittle Invariant Ceilings (M = 9/10)"]
        F8["Mermaid Lexer Delimiter Collisions"]
    end

    subgraph MechanicalOracles["Deterministic Mechanical Oracles"]
        O1["TDD as Living Boundary Contract"]
        O2["AST Invariant Sentinel & Table Dispatch"]
        O3["Negative Schema Contracts (additionalProperties: false)"]
        O4["POSIX Process Groups & Rootless Sandbox"]
        O5["Pre-Flight File Size Caps & Symlink Verification"]
        O6["pprof Runtime Inspection & Concurrency Sentinel"]
        O7["Closed-Loop Inversion & Proactive Headroom"]
        O8["In-Process Docs Validator Engine (< 0.05s)"]
    end

    subgraph ArchitecturalExcellence["Autonomous Engineering State"]
        E1["217/217 Passing Tests (< 0.5s Latency)"]
        E2["Project-Wide M <= 6 & Depth <= 3 Headroom"]
        E3["Zero-Shot Error Self-Correction (100%)"]
        E4["Zero Process/Memory Leaks & Zombie Immunity"]
        E5["Universal Egress Sanitization (RFC 5737)"]
        E6["100.0 / 100 Repository Health Certification"]
    end

    F1 -->|"Anchored by"| O1
    F2 -->|"Refactored by"| O2
    F3 -->|"Rejected by"| O3
    F4 -->|"Contained by"| O4
    F5 -->|"Guarded by"| O5
    F6 -->|"Flagged by"| O6
    F7 -->|"Elevated by"| O7
    F8 -->|"Linted by"| O8

    O1 --> E1
    O2 --> E2
    O3 --> E3
    O4 --> E4
    O5 --> E4
    O6 --> E4
    O7 --> E2
    O8 --> E6
```

---

## 🧭 Master Observation Taxonomy Matrix

The 25 empirical case studies are organized into three complementary domains:
1. **`devops-cli`**: Foundational operational, syntactic, and governance discoveries from building a production-grade infrastructure CLI.
2. **`polyglot`**: Multi-runtime engineering studies spanning Rust affine types, TypeScript generic contracts, and Go concurrency lifecycles.
3. **`systems`**: Distributed systems dynamics including OpenTelemetry tracing waterfalls, test runner latency optimization, inotify event loops, Valkey L2 caching, and rootless container isolation.

| ID | Domain | Observation Title | Stochastic Failure Mode (The Pitfall) | Deterministic Oracle (The Countermeasure) | Verifiable Impact / Metric |
|---|---|---|---|---|---|
| [**01**](./devops-cli/01-tdd-as-living-contract.md) | `devops-cli` | [TDD as Living Contract](./devops-cli/01-tdd-as-living-contract.md) | Unconstrained generation producing 300+ lines of plausible yet broken code ("sycophantic completion"). | Executable red-green-refactor boundary contract authored before source modification. | $900+$ tests passing; $\ge 90.0\%$ strict test coverage gate enforced. |
| [**02**](./devops-cli/02-architectural-invariants-and-complexity-caps.md) | `devops-cli` | [Architectural Invariants & Complexity Caps](./devops-cli/02-architectural-invariants-and-complexity-caps.md) | Procedural branching sprawl ($M \ge 17$, depth $\ge 8$) degrading agent reasoning and triggering token exhaustion. | Automated AST Invariant Sentinel gating every commit at $M \le 10$ and depth $\le 5$. | $-75\%$ cyclomatic complexity; $-66\%$ nesting depth project-wide. |
| [**03**](./devops-cli/03-autonomous-project-governance.md) | `devops-cli` | [Autonomous Project Governance](./devops-cli/03-autonomous-project-governance.md) | Context amnesia across multi-turn sessions; lost backlog tracking and hallucinated progress. | GitHub Projects v2 GraphQL sync and persistent task specs (`docs/agent/tasks/`). | Zero ungrounded actions; 100% synchronized board cards and milestones. |
| [**04**](./devops-cli/04-zero-trust-egress-and-sanitization.md) | `devops-cli` | [Zero-Trust Egress & Universal Sanitization](./devops-cli/04-zero-trust-egress-and-sanitization.md) | Inadvertent leakage of internal hostnames (`*.lan`), private RFC 1918 IPs, and credentials into artifacts. | Pre-flight AST regex scanner and mandatory abstraction rules (`example.com`, RFC 5737). | 100% clean egress; zero confidential credentials or homelab topology leaks. |
| [**05**](./devops-cli/05-harness-slots-and-subagent-offloading.md) | `devops-cli` | [Harness Slots & Subagent Offloading](./devops-cli/05-harness-slots-and-subagent-offloading.md) | Frontier model token exhaustion when performing routine typing, syntax edits, and file inspection. | "Big decides, small types, big checks": hierarchical delegation to local/fast model slots. | $> 65\%$ token cost reduction; $> 3\times$ faster parallel execution. |
| [**06**](./devops-cli/06-rate-limits-and-anti-brittle-heuristics.md) | `devops-cli` | [Rate Limits & Anti-Brittle Heuristics](./devops-cli/06-rate-limits-and-anti-brittle-heuristics.md) | Upstream API HTTP 429 quota exhaustion; brittle regex subsets covering incomplete domains. | Client-side token-bucket rate limiter (`run_gh`) and strict prohibition of partial pattern lists. | Zero rate-limit lockouts; 100% RFC/grammar-backed parsing domains. |
| [**07**](./devops-cli/07-proactive-headroom-and-recursive-feedback-loops.md) | `devops-cli` | [Proactive Headroom & Recursive Feedback](./devops-cli/07-proactive-headroom-and-recursive-feedback-loops.md) | The "Brittle Ceiling Trap": functions resting at $M=9$ or $10$ failing on minor subsequent bug fixes. | Early headroom detection ($M \in [7, 10]$) triggering proactive autonomous refactoring. | Zero emergency backtracking loops; permanent headroom plateau ($M \le 6$). |
| [**08**](./devops-cli/08-closed-loop-feedback-inversion-and-autonomous-quality-elevation.md) | `devops-cli` | [Closed-Loop Feedback Inversion](./devops-cli/08-closed-loop-feedback-inversion-and-autonomous-quality-elevation.md) | Purely reactive fixing leaving technical debt, missing docstrings, and untyped parameters. | Three-phase dynamic: Phase 1 Reactive $\to$ Phase 2 Proactive $\to$ Phase 3 Self-Hardening. | Health score reaches $100.0/100$; 100% public docstrings and type hints. |
| [**09**](./devops-cli/09-mechanical-ast-rewriting-and-automated-refactoring-convergence.md) | `devops-cli` | [Mechanical AST Rewriting](./devops-cli/09-mechanical-ast-rewriting-and-automated-refactoring-convergence.md) | Python `ast.If` recursive `orelse` nesting illusion turning flat `elif` ladders into depth $\ge 8$. | Automated AST Refactorer (`tools/ast_refactorer.py`) compiling ladders to table dispatch. | Complexity instantly reduced from $M \ge 8 \to 1$; depth reduced from $8 \to 1$. |
| [**10**](./devops-cli/10-negative-tool-contract-assertions-and-prescriptive-prompt-synthesis.md) | `devops-cli` | [Negative Tool Contracts & Prescriptive Prompts](./devops-cli/10-negative-tool-contract-assertions-and-prescriptive-prompt-synthesis.md) | Permissive tool schemas accepting hallucinated parameters; unhandled stack traces causing retry loops. | Negative schema validation (`additionalProperties: false`) and prescriptive error prompts. | Tool hallucination drops from $28.4\% \to 0.0\%$; 100% zero-shot self-correction. |
| [**11**](./devops-cli/11-assertion-density-and-structural-tuple-consolidation.md) | `devops-cli` | [Assertion Density & Tuple Consolidation](./devops-cli/11-assertion-density-and-structural-tuple-consolidation.md) | Linear `assert` statements compiling to `if not (expr): raise AssertionError` ($M+1$ per assert). | Structural Tuple Consolidation (`assert (a, b) == (x, y)`) and collection predicates (`all()`). | Multi-attribute test complexity drops from $M=11 \to 1$; diff diagnostics preserved. |
| [**12**](./devops-cli/12-polyglot-cst-boundary-guards-and-symlink-containment.md) | `devops-cli` | [Polyglot CST Boundary Guards & Symlinks](./devops-cli/12-polyglot-cst-boundary-guards-and-symlink-containment.md) | Ingestion of 25MB minified bundles causing OOM (CWE-400); circular symlink recursion (`ELOOP`). | $O(1)$ pre-flight file size caps ($\le 5$MB) and defensive symlink workspace confinement. | 100% crash immunity against minified artifacts and symlink traversal escapes. |
| [**13**](./devops-cli/13-streaming-reasoning-token-parsers-and-think-block-sanitization.md) | `devops-cli` | [Streaming Reasoning Token Parsers & Sanitization](./devops-cli/13-streaming-reasoning-token-parsers-and-think-block-sanitization.md) | Thought token leakage (`<think>`) into tool parameters/terminals; unclosed tag stream freezes; memory leaks. | Streaming FSM parser with bounded sliding-window prefix buffers and isolated telemetry accumulators. | 100% containment of leaked reasoning tokens; sub-millisecond per-chunk streaming latency; zero hangs. |
| [**14**](./devops-cli/14-binary-search-ast-context-packing-and-token-budgeting.md) | `devops-cli` | [Binary Search AST Context Packing](./devops-cli/14-binary-search-ast-context-packing-and-token-budgeting.md) | Heuristic line cuts fracturing syntax mid-block; greedy packing stranding $>30\%$ of token budgets. | Hierarchical symbol decomposition with monotonic binary search truncation ($O(\log N)$). | 99.4% token budget utilization with zero AST syntax breakage; $12\times$ faster than linear packing. |
| [**15**](./devops-cli/15-llm-structured-output-repair-and-schema-reconciliation.md) | `devops-cli` | [LLM Structured Output Repair & Schema Retry](./devops-cli/15-llm-structured-output-repair-and-schema-reconciliation.md) | Markdown fences, trailing commas, and truncated braces triggering `json.loads` failures and retry death spirals. | Two-tier repair: Tier 1 stack/AST syntactic repair locally; Tier 2 prescriptive schema retry synthesis. | $>92\%$ auto-repair rate without model roundtrips; 100% single-turn recovery; zero retry loops. |
| [**01**](./polyglot/01-rust-type-state-invariants.md) | `polyglot` | [Rust Type-State Invariants](./polyglot/01-rust-type-state-invariants.md) | Runtime state validation errors and unhandled transition branches in complex state machines. | Affine ownership types and compile-time type-state pattern (`PhantomData<State>`). | Zero runtime state crashes; `#![forbid(unsafe_code)]`; 0 runtime memory overhead. |
| [**02**](./polyglot/02-typescript-cst-and-type-gymnastics.md) | `polyglot` | [TypeScript CST & Type Gymnastics](./polyglot/02-typescript-cst-and-type-gymnastics.md) | Agents escaping complex type unions by inserting `any`, `unknown`, or `@ts-ignore` bypasses. | Structural discriminated unions, branded types, and AST-level linting forbidding `any`. | Zero `any` escape hatches; 100% end-to-end type safety in frontend clients. |
| [**03**](./polyglot/03-go-goroutine-leakage-and-context-lifecycles.md) | `polyglot` | [Go Goroutine Leakage & Context Lifecycles](./polyglot/03-go-goroutine-leakage-and-context-lifecycles.md) | Spawning worker goroutines sending to unbuffered channels without `<-ctx.Done()` cancellation selects. | Go Concurrency Sentinel analyzing `pprof` stack traces and mapping blocked channel states. | Zero thread exhaustion; 100% clean goroutine reclamation across concurrent jobs. |
| [**01**](./systems/01-distributed-telemetry-and-agent-waterfalls.md) | `systems` | [Distributed Telemetry & Agent Waterfalls](./systems/01-distributed-telemetry-and-agent-waterfalls.md) | Opaque multi-agent execution graphs; hidden latency bottlenecks and untracked token burn. | OpenTelemetry traceparent propagation, W3C distributed tracing, and waterfall spans. | Sub-millisecond latency attribution; exact per-subagent token usage accounting. |
| [**02**](./systems/02-subprocess-test-harness-instrumentation-tax.md) | `systems` | [Subprocess Test Harness Instrumentation Tax](./systems/02-subprocess-test-harness-instrumentation-tax.md) | Heavy workspace test plugins adding $4.4\text{s}$ per test file, starving agent verification cadence. | Isolated test configuration (`pytest.ini` with `-o addopts=`) bypassing workspace plugins. | Single-test latency reduced from $4.4\text{s} \to 0.35\text{s}$ ($> 85\%$ speedup). |
| [**03**](./systems/03-event-driven-file-watchers-and-continuous-invariant-loops.md) | `systems` | [Event-Driven File Watchers & Invariant Loops](./systems/03-event-driven-file-watchers-and-continuous-invariant-loops.md) | Slow manual invocation cycles; agent context drift during long-running batch check phases. | Real-time inotify file watcher daemon (`ResourceWatcher`) evaluating on file save. | Sub-second reactive feedback loop; zero cognitive drift between edits. |
| [**04**](./systems/04-content-addressed-two-tier-caching-and-embedding-drift-audits.md) | `systems` | [Content-Addressed Valkey L2 Caching](./systems/04-content-addressed-two-tier-caching-and-embedding-drift-audits.md) | Redundant AST parsing and embedding calculation consuming external quota and cycles. | Two-tier cache (L1 in-memory + L2 Valkey) with SHA-256 keys and Cosine Distance drift audits. | $> 90\%$ cache hit rate; instant semantic drift detection upon code refactoring. |
| [**05**](./systems/05-rootless-container-sandboxing-and-process-group-containment.md) | `systems` | [Rootless Container Sandboxing & Process Groups](./systems/05-rootless-container-sandboxing-and-process-group-containment.md) | Standard `proc.kill()` leaving grandchild processes running as PID 1 zombie leaks; host credential exposure. | POSIX process group isolation (`start_new_session=True` & `os.killpg`) and CIS rootless container sandbox. | 100% CIS Rootless Benchmark score (8/8); sub-millisecond process tree kill; zero leaks. |
| [**06**](./systems/06-multi-agent-concurrency-shared-workspace-hazards-and-swarm-coordination.md) | `systems` | [Multi-Agent Concurrency & Workspace Hazards](./systems/06-multi-agent-concurrency-shared-workspace-hazards-and-swarm-coordination.md) | Concurrent agents clobbering working tree, unlinking active SQLite coverage files, and entering rebase live-locks. | Mandatory POSIX git worktrees, partitioned `.data/agent/<id>`, Valkey L2 mutexes, and FIFO PR shepherding. | Zero working tree collisions; 100% immunity to SQLite coverage corruption; $7\times$ swarm throughput. |
| [**07**](./systems/07-agentic-ide-protocols-and-lsp-mcp-convergence.md) | `systems` | [Agentic IDE Protocols & LSP/MCP Convergence](./systems/07-agentic-ide-protocols-and-lsp-mcp-convergence.md) | Disconnected compiler feedback; zombie grandchild processes; credential exposure in IDE. | Tri-protocol architecture: LSP diagnostic CEGIS oracles, MCP capability mesh, and zero-trust lifecycle hooks. | Sub-millisecond pre-tool interception; 100% zombie containment; zero private IP leaks. |

---

## 🔬 The Five Unifying Architectural Theses

When analyzed collectively, the 25 empirical case studies coalesce into five core engineering theses that define disciplined agentic software development:

### 1. Deterministic Mechanical Oracles Over Prompt Faith
Stochastic language models cannot self-evaluate architectural complexity, nesting depth, type safety, or security boundaries purely through prompt instructions. Relying on "be careful not to write complex code" invariably fails.
- **AST Invariants**: Mechanical parsers (`ast.walk`, `ast.NodeVisitor`) enforce unyielding mathematical ceilings ($M \le 10$, depth $\le 5$).
- **Executable Living Contracts**: TDD establishes an objective physical runtime oracle that models must satisfy before moving forward.
- **Compile-Time Type States**: Affine type systems (Rust) and discriminated unions (TypeScript) force the model to handle 100% of state variants at build time.

### 2. Negative Contracts, Prescriptive Feedback & Epistemic Hygiene
Permissive schemas and vague error messages induce degenerative agent retry spirals.
- **Negative Schema Assertions**: Rejecting undeclared parameters (`additionalProperties: false`) forces the model to respect exact API contracts.
- **Prescriptive Error Prompts**: Rather than returning raw tracebacks, mechanical validators synthesize structured instructions detailing allowable fields, enabling 100% zero-shot self-correction.
- **Anti-Brittle Closed Domains**: Pattern matching is strictly restricted to mathematically exhaustive domains (RFCs, official language grammars, public registries like Mozilla PSL), forbidding ad-hoc regular expressions.

### 3. Process Group Containment & Zero-Trust Sandboxing
Model-generated code is untrusted code. Direct host execution invites zombie process accumulation, credential theft, and denial-of-service.
- **POSIX Process Groups**: Allocating a dedicated process session (`start_new_session=True`) ensures that terminating via `os.killpg(os.getpgid(pid), SIGTERM)` cleanses the entire process hierarchy down to child subshells.
- **CIS Rootless Containers**: Applying 8 core CIS controls (read-only rootfs, `--network none`, `--cap-drop ALL`, non-root UID 1000, 512MB RAM cap, 100 PID limit, bounded $\le 64\text{KB}$ buffers) guarantees total workstation safety.

### 4. The Closed-Loop Feedback Inversion Dynamic
Static linting is historically reactive—it barks only after rules are violated. Autonomous agent engineering requires an **inverting feedback dynamic**:
- **Phase 1 (Reactive Remediation)**: Fix active blockers, failing tests, and syntax errors with 100% priority.
- **Phase 2 (Proactive Quality Elevation)**: When health reaches 100.0/100, the engine shifts focus to proactive headroom ($M \in [7, 10] \to M \le 6$), complete docstring/type coverage, and test latency optimization.
- **Phase 3 (Continuous Self-Hardening)**: Every struggle, friction point, or defect is systematically codified into [`AGENTS.md`](../AGENTS.md) and [`docs/ROADMAP.md`](../docs/ROADMAP.md), eliminating recurrent failure modes across future sessions.

### 5. Multi-Agent Concurrency & Worktree Spatial Isolation
Running multiple agents concurrently against a shared codebase creates catastrophic workspace contention, SQLite unlink races, and rebase live-locks unless guarded by strict architectural separation:
- **Spatial Isolation**: Mandatory POSIX git worktrees (`git worktree add`) guarantee that concurrent file edits never cross-contaminate peer test runs.
- **Partitioned Data Tiers**: Scoping cache directories (`.data/agent/<id>`) isolates SQLite coverage databases, eliminating file unlinking races during parallel test execution.
- **FIFO Pull Request Shepherding**: Processing PRs in strict chronological order eliminates thundering herd rebase live-locks and ensures equitable review throughput.

---

## 🔗 Cross-Repository Architectural Resonance

The observations in `vibes` do not exist in a vacuum; they reflect a direct, symbiotic relationship with [`devops-cli`](https://github.com/dan-petty/devops-cli):

```mermaid
flowchart LR
    DevOpsCLI["devops-cli (Production CLI)"] -->|"Extracts Invariants & Friction"| VibesShowcase["vibes (Showpiece & Laboratory)"]
    VibesShowcase -->|"Hardens Tools & Oracles"| Oracles["AST Refactorer, Sentinel, Sandbox"]
    Oracles -->|"Injects Guardrails into AGENTS.md"| DevOpsCLI
```

- **From Production to Laboratory**: When `devops-cli` encountered API rate limits, assertion sprawl, or complex `elif` ladders, the failure mode was isolated, measured, and synthesized into a dedicated case study under `observations/`.
- **From Laboratory to Production**: Reference implementations tested in `vibes` (e.g. `ast_refactorer.py`, `sentinel.py`, `ResourceWatcher`, `ContainerSandboxHarness`) were codified directly into `devops-cli` architectural invariants and pre-commit gates.

---

## 🚀 Future Observation Horizons (Living Research Queue)

The investigation into agentic engineering continues to advance. Upcoming empirical studies tracked in the [Strategic Roadmap](../docs/ROADMAP.md) include:

1. **C++ RAII & Lifetime Invariants Under LLM Synthesis**: Evaluating model capabilities in synthesizing manual memory-safe code without Rust's compile-time ownership tracking.
2. **eBPF Process Tracing for Agent Sandbox Introspection**: Utilizing kernel probes to monitor subagent syscall patterns and socket operations in real-time.
3. **Attention Dilution & Context Decay in Ultra-Long Sessions**: Quantifying constraint degradation as conversational context exceeds 100k tokens.
4. **Autonomous Conversation-to-Case-Study Synthesis**: Building automated pipelines that ingest raw agent trajectory JSONL logs, apply zero-trust sanitization, and generate publication-ready empirical observations.
