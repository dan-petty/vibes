# Strategic Roadmap — vibes ✨

High-density product roadmap, engineering milestones, and open-source curation strategy for the `vibes` living showcase and artifact repository.

---

## Core Vision & Design Principles

1. **Living Laboratory Over Static Museum**: `vibes` is an active workbench. Every observation must be grounded in empirical engineering; every pattern must have a reproducible code or harness artifact.
2. **Beyond "Vibe Coding"**: Systematically counter the narrative that AI coding is undisciplined prompting. Highlight mechanical invariant gates, test-first boundary conditions, and formal state machines.
3. **Zero-Trust Egress & Universal Sanitization**: All artifacts, test fixtures, and logs are 100% sanitized of internal network topologies, private IPs, credentials, and confidential paths.
4. **Executable Demonstrations**: Pair every conceptual pattern with an executable reference implementation under `examples/`, kept close to the standard library so a reader can copy a single file and run it. Repository tooling under `tools/` takes the opposite default and adopts established open source rather than reimplementing it.
5. **Autonomous Swarm Maintainability**: Maintain structured `AGENTS.md` operating instructions and GitHub Project tracking so AI agents can continuously curate, verify, and cross-reference new artifacts autonomously.
6. **Polyglot & Multi-Runtime Breadth**: Extend verification paradigms beyond Python to Rust, Go, TypeScript, and containerized runtime sandboxes.

---

## Release Milestones (Chronological Order)

```mermaid
timeline
    title Milestone arc, foundations to living standard
    section Shipped
        v0.1.0 : Foundations and the devops-cli retrospective
        v0.2.0 : Interactive testbeds and executable sample apps
    section In flight
        v0.3.0 : Multi-agent benchmark suites and observability mesh
        v0.4.0 : Polyglot invariant harnesses and ephemeral sandboxes
    section Ahead
        v0.5.0 : Autonomous swarm orchestration and self-healing
        v1.0.0 : Enterprise governance, AIBOM and living standard
```

### Delivered: v0.1.0 through v0.3.0

Three milestones are complete and archived verbatim in [`docs/archive/delivered-milestones-v0.1-to-v0.3.md`](./archive/delivered-milestones-v0.1-to-v0.3.md).

| Milestone | Scope | Headline deliverables |
|---|---|---|
| **v0.1.0** | Foundations & the `devops-cli` retrospective | Manifesto, taxonomy, the first eight observations, core patterns |
| **v0.2.0** | Interactive testbeds & executable sample apps | Sentinel, CEGIS workbench, token-bucket gateway, trace generator, autonomous tooling |
| **v0.3.0** | Multi-agent benchmarks & observability mesh | Benchmark runner, OTLP mesh, Valkey L2 cache, contract verification, fast-feedback runner, AST refactorer |

---

### Milestone 4: Polyglot Invariant Harnesses & Ephemeral Sandboxes (v0.4.0 - In Flight)
- [x] **Go Concurrency & Goroutine Leak Sentinel (`examples/go-leak-sentinel/`)**:
  - Executable reference tool utilizing Go runtime stack inspection (`runtime.NumGoroutine()`) and `pprof` trace analysis to detect orphaned goroutines, deadlock-prone unbuffered channels, and leaking context lifecycles.
  - Integration with `golangci-lint` AST checkers for static channel closure verification.
- [x] **Rust Memory & Type-State Benchmark Track (`benchmarks/rust/`)**:
  - Specialized benchmark suite evaluating agent capability to synthesize memory-safe, zero-cost abstractions using Rust's affine ownership types and type-state builders (`benchmarks/rust/src/lib.rs`).
  - Automated `cargo test` harness verifying `#![forbid(unsafe_code)]`, zero-sized type-state memory overhead (`size_of::<State>() == 0`), and compile-time rejection of invalid lifecycle transitions.
- [x] **Ephemeral Rootless Docker Sandbox Harness (`examples/ephemeral-container-sandbox/`)**:
  - Lightweight, isolated execution harness for executing untrusted agent-generated code inside unprivileged, rootless containers (`docker` / `podman`) with POSIX simulator fallback.
  - Hardened execution policies: CIS Rootless Container Security Benchmark auditing (8 controls), cgroups v2 resource caps (512MB RAM, 1 CPU, 100 PIDs), read-only root filesystems, ephemeral tmpfs scratch space, dropped capabilities (`CAP_DROP ALL`), zero-trust sanitized environment variables, and strict network egress deny-all policies (`--network none`).
  - Interactive containment matrix demo verifying safe execution, timeout termination, and output buffer bounding (CWE-400 mitigation).
- [x] **Polyglot CST Ingestion Engine (`examples/polyglot-cst-parser/`)**:
  - Unified Concrete Syntax Tree (CST) and AST parser supporting Python, Rust, Go, TypeScript/JavaScript, and Bash.
  - Language-agnostic cyclomatic complexity ($M$) and block nesting depth calculation.
  - Hardened pre-flight file size boundary guards (`MAX_FILE_SIZE_BYTES = 5MB`) mitigating OOM/DoS (CWE-400) and defensive symlink resolution preventing circular loops (`ELOOP`) and workspace traversal escapes.
- [x] **Automated Assertion Consolidation Engine (`tools/ast_refactorer.py`)**:
  - Mechanical AST rewriting transform identifying linear sequences of `ast.Assert` statements in test suites and compiling them into structural tuple equality checks (`assert actual == expected`) and collection predicates (`all(...)`).
  - Automatically mitigates test suite cyclomatic complexity traps ($M > 10$) without loss of pytest element-level diff diagnostics.
- [x] **Ephemeral Container Backend for CEGIS Patch Evaluation (`examples/cegis-debugging-workbench/`)**:
  - Direct integration of `ContainerSandboxHarness` into the CEGIS debugging loop.
  - Safely evaluates candidate patches inside isolated rootless containers with strict cgroup memory (512MB) and PID limits, preventing runaway candidate code from impacting host processes.
- [x] **Zero-Dependency Markdown Syntax & Link Integrity Validator (`tools/docs_validator.py`)**:
  - Standalone, zero-external-dependency validation engine verifying CommonMark nested code fence parity, Mermaid diagram AST headers, markdown table column and delimiter alignment, relative link targets, and paired HTML tags.
  - Native integration with `ResourceIterationWorkbench` (`ResourceType.DOCUMENTATION`) enforcing automated documentation quality gates across 56+ repository documents.
- [x] **Automated Documentation Self-Healing & Relative Path Normalizer (`tools/docs_validator.py --fix`)**:
  - Mechanical remediation engine auto-correcting CommonMark code fence unclosed blocks, unquoted Mermaid node/edge labels, and normalizing IDE absolute paths (`file:///...`) into verified relative links with accurate directory traversal depths.
- [x] **Streaming Reasoning Token Parser & Bounded Stream Sanitizer (`examples/streaming-reasoning-sanitizer/`)**:
  - Streaming FSM extracting reasoning tokens (`<think>`, `[reasoning]`), bounded buffer isolation (CWE-400 mitigation), zero stream hangs, and isolated thought telemetry.
- [x] **High-Performance AST Context Packer with Binary Search Truncation (`examples/binary-search-context-packer/`)**:
  - Algorithmic prompt context packing decomposing Python source into structured AST symbol units, with monotonic binary search convergence ($O(\log N)$) maximizing token budget utilization with zero broken syntax trees.
- [x] **Agentic IDE Lifecycle Hook Sentinel & Zero-Trust Execution Guard (`examples/agentic-ide-hook-sentinel/`)**:
  - Intercepts agent tool execution calls and file writes in IDE control planes (VS Code, Antigravity, Cursor).
  - Enforces zero-trust egress (blocks private RFC 1918 IPs, blocks credential file paths), POSIX process group containment, pre-flight file size bounds (5MB), and LSP diagnostic ingestion.
- [ ] **Automated Seccomp BPF Profile Synthesizer**:
  - Generates minimal, tool-specific Linux seccomp-bpf JSON filter profiles based on static symbol analysis and syscall trace profiling, restricting agent tool execution strictly to required system calls.

---

### Milestone 5: Autonomous Swarm Orchestration & Self-Healing (v0.5.0 - Planned)
- [x] **Reliability SLO Engine & Error-Budget-Driven Phase Policy (`tools/reliability_slo.py`)**:
  - Five service level indicators measured as good events over valid events — `gate_pass_rate`, `invariant_compliance`, `feedback_latency`, `headroom_saturation`, and `toil_containment` — each with a target, a rolling window, and a stated rationale.
  - Error budget arithmetic with burn rate and a minimum-sample floor, replacing the binary `health == 100.0` inversion in `AGENTS.md` §11. Spending budget below target is normal operation; exhausting or burning it freezes proactive work.
  - `OBJECTIVE_REVIEW`: a budget that closes a full window entirely unspent is reported as a finding, since a loop that never fails cannot distinguish reliable from unambitious.
  - Registry/Strategy indicators, frozen value objects, and a single policy table, codified in [`patterns/error-budget-driven-feedback-inversion.md`](../patterns/error-budget-driven-feedback-inversion.md).
- [ ] **Closed-Loop PR Review Thread Synchronizer & Atomic Resolver (`tools/pr_thread_sync.py`)**:
  - Automated PR shepherd querying unresolved GitHub GraphQL review discussion threads, correlating review comments to source AST nodes, orchestrating mechanical fixes with invariant oracles, and atomically posting structured review replies with thread resolution.
- [x] **Continuous SDLC Backlog & Automated Lifecycle Transition Daemon (`tools/sdlc_project_manager.py sync --watch`)**:
  - Reconciles `.data/sdlc_backlog.json` against a fresh `ResourceIterationWorkbench` export: defect cards whose finding is no longer reported are closed, new findings are opened, and exactly one unblocked card is promoted to Ready per pass. Roadmap cards are exempt from closing, since no scan can observe an unbuilt feature. Idempotent across repeated passes; `--watch` reconciles continuously.
- [ ] **Closed-Loop PR Triage & Invariant Review Bot**:
  - GitHub App / Action orchestrating the Multi-Persona Code Reviewer (`security`, `architecture`, `devops`, `qa`) against incoming pull requests.
  - Automated inline review comments, structured GitHub check runs, and AST Invariant Sentinel gating before pull request merges.
- [ ] **Autonomous Conversation-to-Case-Study Synthesizer (`tools/synthesizer/`)**:
  - Automated pipeline ingesting raw agent trajectory logs (JSONL transcripts), applying zero-trust redaction (RFC 5737 IPs, `example.com` domains, secret masking), calculating quantitative token/complexity metrics, and drafting structured observation reports.
- [ ] **Live Multi-Model Leaderboard & Cost-Per-Invariant Index**:
  - Automated benchmarking matrix running weekly against leading frontier and local open-weights models (Claude, GPT-4o, DeepSeek-V3, Qwen-2.5-Coder).
  - Public dashboard tracking: Pass@1 on AST complexity invariants, patch minimality score, and cloud token expenditure per verified pull request.
- [ ] **Hierarchical Subagent Slot Offloading Orchestrator**:
  - Multi-agent coordinator implementing the "Big decides, small types, big checks" architectural pattern.
  - Dynamic routing engine allocating planning to frontier reasoning models, atomic typing and refactoring to lightweight local models, and verification to deterministic AST oracles.

---

### Milestone 6: Enterprise Governance, AIBOM & Living Standard (v1.0.0 - North Star)
- [x] **Post-1.0 Deprecation Lifecycle Enforcement (`examples/deprecation-lifecycle-sentinel/`)**:
  - Executable release gate enforcing the deprecation contract (`since`, `remove_in`, `replacement`), runtime `DeprecationWarning` emission, major-boundary removal scheduling, and internal call-site migration.
  - Pre-1.0 inert by construction: the SemVer major-boundary rule does not bind while the major version is `0`, so the repository keeps delete-on-sight freedom and inherits the discipline automatically at the 1.0 boundary.
  - Codified in [`patterns/post-v1-deprecation-lifecycle.md`](../patterns/post-v1-deprecation-lifecycle.md) and `AGENTS.md` §7.
- [x] **Post-1.0 Deprecation Lifecycle Enforcement (`examples/deprecation-lifecycle-sentinel/`)**:
  - Executable release gate enforcing the deprecation contract (`since`, `remove_in`, `replacement`), runtime `DeprecationWarning` emission, major-boundary removal scheduling, and internal call-site migration.
  - Pre-1.0 inert by construction: the SemVer major-boundary rule does not bind while the major version is `0`, so the repository keeps delete-on-sight freedom and inherits the discipline automatically at the 1.0 boundary.
  - Codified in [`patterns/post-v1-deprecation-lifecycle.md`](../patterns/post-v1-deprecation-lifecycle.md) and `AGENTS.md` §7.
- [ ] **AI Bill of Materials (AIBOM) & Supply-Chain Security Scanner**:
  - Automated scanner generating AIBOM inventories mapping AI dependencies, pretrained weights provenance, Hugging Face model card hashes, and dataset licenses.
  - AST security sentinel scanning codebases for dangerous `trust_remote_code=True`, unpickling vulnerabilities, and unsafe model checkpoint loaders.
- [ ] **Adversarial Prompt Injection & CWE-200 Egress Security Scanner**:
  - Automated red-teaming scanner validating agent code and prompt templates against indirect prompt injection (via file content, tool outputs, or git commits).
  - Zero-Trust Egress Validator verifying zero leakage of environmental variables, secret tokens, or private RFC 1918 hostnames.
- [ ] **Enterprise Change Management & Version Deprecation Protocol** (partially delivered):
  - Delivered: the deprecation contract and removal gate — `since`/`remove_in`/`replacement`, runtime `DeprecationWarning`, major-boundary scheduling, and internal call-site migration ([`examples/deprecation-lifecycle-sentinel/`](../examples/deprecation-lifecycle-sentinel/)).
  - Remaining: runtime feature flags carrying the same removal obligation, and automated migration tooling for agent tool schemas.
- [ ] **Certified Living Standard for Agentic Engineering**:
  - Formal specification and certification test suite for AI coding assistants.
  - Standardized open compliance badge (`Passed Vibes Invariant Gate v1.0`) for repositories engineered with disciplined agentic workflows.

---

## Research & Observation Backlog (Living Research Queue)

Upcoming field observations, empirical studies, and architectural investigations to be authored and added to `observations/`:

| Topic / Working Title | Target Domain | Key Research Question | Status / Outcome |
|---|---|---|---|
| **Negative Tool Schema Invariants vs. Hallucinated Parameters** | `observations/devops-cli/` | Zero-shot agent recovery using JSON Schema negative assertions and prescriptive error prompt synthesis. | ✅ Completed ([Obs 10](../observations/devops-cli/10-negative-tool-contract-assertions-and-prescriptive-prompt-synthesis.md)) |
| **Tree-Sitter CST vs. AST Hierarchy & Boundary Traps** | `observations/devops-cli/` | Reconciling visual indentation depth against hierarchical AST structures and enforcing file size / symlink boundaries. | ✅ Completed ([Obs 09](../observations/devops-cli/09-mechanical-ast-rewriting-and-automated-refactoring-convergence.md), [Obs 12](../observations/devops-cli/12-polyglot-cst-boundary-guards-and-symlink-containment.md)) |
| **Autonomous Session Retrospective & Recursive Inversion** | `docs/` | Systemic retrospective analyzing agent failure modes, mechanical oracles, and the reactive-to-proactive feedback inversion. | ✅ Completed ([RETROSPECTIVE.md](./RETROSPECTIVE.md)) |
| **Go Goroutine Leakage & Context Lifecycles** | `observations/polyglot/` | How do frontier models handle graceful cancellation and channel closure under high concurrency? | ✅ Completed ([Obs 03](../observations/polyglot/03-go-goroutine-leakage-and-context-lifecycles.md)) |
| **Streaming Reasoning Token Parsers & Think Block Sanitization** | `observations/devops-cli/` | Preventing `<think>` trace leakage into execution payloads and tool parameters via streaming FSM sanitizers. | ✅ Completed ([Obs 13](../observations/devops-cli/13-streaming-reasoning-token-parsers-and-think-block-sanitization.md)) |
| **Binary Search AST Context Packing & Token Budgeting** | `observations/devops-cli/` | Packing multi-symbol AST context into exact token budgets without mid-block syntax breakage. | ✅ Completed ([Obs 14](../observations/devops-cli/14-binary-search-ast-context-packing-and-token-budgeting.md)) |
| **LLM Structured Output Repair & Schema Reconciliation** | `observations/devops-cli/` | In-process syntactic repair of markdown fences, trailing commas, and truncated braces. | ✅ Completed ([Obs 15](../observations/devops-cli/15-llm-structured-output-repair-and-schema-reconciliation.md)) |
| **Agentic IDE Protocols & LSP/MCP Convergence** | `observations/systems/` | How do Language Server diagnostics and Model Context Protocol tools converge with zero-trust IDE hooks? | ✅ Completed ([Obs 07](../observations/systems/07-agentic-ide-protocols-and-lsp-mcp-convergence.md)) |
| **Suppression Ergonomics: Waivers That Cannot Become Mute Buttons** | `patterns/` | Which invariants may a codebase ever waive, and what makes a waiver auditable rather than an escape hatch that erodes the gate? | ✅ Completed ([Gate Integrity pattern](../patterns/gate-integrity-and-total-input-coverage.md)) |
| **Silent Certification Failures in Mechanical Gates** | `observations/systems/` | When an enforcement tool reads only the first of N inputs and reports success, how long does a green checkmark conceal unaudited code? | ✅ Completed ([Obs 11](../observations/systems/11-silent-certification-failure-and-gate-integrity.md)) |
| **Oracle Measurement Validity & Harness-Smeared Metrics** | `observations/systems/` | When a deterministic oracle measures its own scaffolding, how long does an agent chase an unfixable defect before suspecting the ruler rather than the object? | ✅ Completed ([Obs 11](../observations/systems/11-silent-certification-failure-and-gate-integrity.md)) |
| **C++ RAII & Lifetime Invariants Under LLM Synthesis** | `observations/polyglot/` | Can LLMs reliably avoid use-after-free and double-free bugs without Rust-like compile-time guarantees? | 🔬 In Queue |
| **eBPF Process Tracing for Agent Sandbox Introspection** | `observations/systems/` | Using eBPF probes to capture syscall patterns, file access, and network socket operations of subagents in real-time. | 🔬 In Queue |
| **Attention Dilution & Context Decay in Ultra-Long Sessions** | `observations/cognitive/` | Measuring degradation in constraint adherence as context lengths exceed 100k tokens and evaluating multi-scale pruning. | 🔬 In Queue |
| **Self-Correction Loops vs. Constraint Accumulation** | `observations/methodology/` | Comparing conversational "fix this error" prompting vs. formal CEGIS negative-constraint accumulation. | 🔬 In Queue |

---

## Value vs. Effort Prioritization Matrix

Every item still open, placed by the same value and effort judgements the table records. The rejected anti-patterns are plotted alongside deliberately — a roadmap is defined as much by what it refuses as by what it schedules:

```mermaid
quadrantChart
    title Open deliverables and rejected anti-patterns
    x-axis "Low effort" --> "High effort"
    y-axis "Low value" --> "High value"
    quadrant-1 "Major projects"
    quadrant-2 "Quick wins"
    quadrant-3 "Fill-ins"
    quadrant-4 "Anti-patterns"
    "PR review thread synchronizer": [0.45, 0.82]
    "SDLC backlog daemon": [0.54, 0.74]
    "PR triage and invariant bot": [0.70, 0.88]
    "Conversation synthesizer": [0.80, 0.76]
    "Multi-model leaderboard": [0.88, 0.68]
    "AIBOM supply-chain scanner": [0.74, 0.94]
    "Certified living standard": [0.92, 0.84]
    "Prompt injection scanner": [0.50, 0.50]
    "Toy chat-with-your-code demos": [0.86, 0.12]
    "Unsanitized transcript dumps": [0.16, 0.10]
    "Ad-hoc pattern matching subsets": [0.46, 0.20]
```

| Priority Category | Feature / Deliverable | Primary Tech / Tools | Value | Effort | Target Milestone | Status |
|---|---|---|---|---|---|---|
| **Quick Wins** | Executable Sample Apps (`sentinel`, `gateway`, `workbench`, `trace-gen`) | Python Standard Library | High | Low | v0.2.0 | ✅ Completed |
|  | Strategic Product Roadmap (`docs/ROADMAP.md`) | Markdown / Planning | High | Low | v0.2.0 | ✅ Completed |
|  | GitHub Actions CI & Recursive Workflows | GitHub Workflows / `pytest` | High | Low | v0.2.0 | ✅ Completed |
|  | Autonomous Project Tooling (`tools/project_tooling.py`) | Python Standard Library | High | Low | v0.2.0 | ✅ Completed |
|  | Polyglot Case Studies (Rust & TypeScript) | Systems Engineering | High | Low | v0.2.0 | ✅ Completed |
|  | Continuous File-Watcher Mode (`--watch`) | Python / inotify | High | Low | v0.3.0 | ✅ Completed |
|  | Zero-Dependency Markdown Syntax & Link Integrity Validator | Python Standard Library | High | Low | v0.4.0 | ✅ Completed |
|  | Automated Documentation Self-Healing & Relative Path Normalizer | Python Standard Library | High | Low | v0.4.0 | ✅ Completed |
|  | Streaming Reasoning Token Parser & Sanitizer | Python / Streaming FSM | High | Medium | v0.4.0 | ✅ Completed |
|  | High-Performance AST Context Packer | Python AST / Binary Search | High | Medium | v0.4.0 | ✅ Completed |
|  | Agentic IDE Lifecycle Hook Sentinel | Python / Process Groups / LSP | High | Medium | v0.4.0 | ✅ Completed |
| **Major Projects** | Multi-Agent Benchmark Suite (`benchmarks/`) | `pytest` / AST Analyzer | High | Medium | v0.3.0 | ✅ Completed |
|  | OpenTelemetry Agent Waterfall Generator | OpenTelemetry / Python | High | Medium | v0.3.0 | ✅ Completed |
|  | Interactive Prompt Mutation Suite & Invariant Fuzzer | Python / AST / Fuzzing | High | Medium | v0.3.0 | ✅ Completed |
|  | Fast-Feedback Test Optimization & Isolated Runner | Pytest / Isolated ini | High | Medium | v0.3.0 | ✅ Completed |
|  | Automated AST Conditional Refactorer | Python AST Transformer | High | Medium | v0.3.0 | ✅ Completed |
|  | OTLP Live Collector & Jaeger/Grafana Mesh | OTLP / gRPC / Docker | High | Medium | v0.3.0 | ✅ Completed |
|  | Valkey L2 AST Caching & Embedding Drift Auditor | Valkey / Redis / Vector | High | Medium | v0.3.0 | ✅ Completed |
|  | Rust Memory & Type-State Benchmark Track | Cargo / Clippy / Rust | High | Medium | v0.4.0 | ✅ Completed |
|  | Rootless Docker Sandbox Harness | Docker / cgroups v2 | High | High | v0.4.0 | ✅ Completed |
|  | Polyglot CST Ingestion Engine | Tree-Sitter / Multi-Lang | High | High | v0.4.0 | ✅ Completed |
|  | Go Concurrency & Goroutine Leak Sentinel | Go 1.23 / `pprof` | High | Medium | v0.4.0 | ✅ Completed |
|  | Automated Assertion Consolidation Engine | Python AST Transformer | High | Medium | v0.4.0 | ✅ Completed |
|  | Ephemeral Container Backend for CEGIS | Docker / cgroups v2 | High | High | v0.4.0 | ✅ Completed |
|  | Closed-Loop PR Review Thread Synchronizer & Atomic Resolver | Python / GraphQL / AST | High | Medium | v0.5.0 | 📋 Scheduled |
|  | Continuous SDLC Backlog & Automated Lifecycle Transition Daemon | Python / Watcher / JSON | High | Medium | v0.5.0 | ✅ Completed |
|  | Closed-Loop PR Triage & Invariant Review Bot | FastMCP / GitHub Actions | High | High | v0.5.0 | 📋 Scheduled |
|  | Autonomous Conversation Synthesizer | Python / NLP / Metrics | High | High | v0.5.0 | 📋 Scheduled |
|  | Live Multi-Model Leaderboard & Cost Index | Python / GitHub Pages | High | High | v0.5.0 | 📋 Scheduled |
|  | AIBOM & Supply-Chain Security Scanner | Python / AST / CycloneDX | High | High | v1.0.0 | 💡 Future Vision |
|  | Certified Living Standard & Compliance Badge | Specification / Test Suite | High | High | v1.0.0 | 💡 Future Vision |
| **Fill-Ins** | Community Issue Templates & PR Rubrics | GitHub Templates | Medium | Low | v0.1.0 | ✅ Completed |
|  | Adversarial Prompt Injection Security Scanner | Red-Teaming / Python | Medium | Medium | v1.0.0 | 💡 Future Vision |
| **Foundation** | Invariant Curation Guidelines & Sanitization | `AGENTS.md` / RFC 5737 | High | Medium | v0.1.0 | ✅ Completed |
|  | Disciplined Agentic Manifesto | `docs/MANIFESTO.md` | High | Medium | v0.1.0 | ✅ Completed |
|  | Taxonomy of Agentic Engineering | `docs/TAXONOMY.md` | High | Medium | v0.1.0 | ✅ Completed |
| **Anti-Patterns** | Toy "Chat with your Code" Demos | Generic LangChain wrappers | Low | High | — | ❌ Rejected (No toy demos) |
|  | Unsanitized Transcript Dumps | Raw log files | Low | Low | — | ❌ Rejected (Zero leakage) |
|  | Ad-Hoc Pattern Matching Subsets | Arbitrary regex lists | Low | Medium | — | ❌ Rejected (Use AST / RFCs) |
