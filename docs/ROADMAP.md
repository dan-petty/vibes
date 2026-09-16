# Strategic Roadmap — vibes ✨

High-density product roadmap, engineering milestones, and open-source curation strategy for the `vibes` living showcase and artifact repository.

---

## Core Vision & Design Principles

1. **Living Laboratory Over Static Museum**: `vibes` is an active workbench. Every observation must be grounded in empirical engineering; every pattern must have a reproducible code or harness artifact.
2. **Beyond "Vibe Coding"**: Systematically counter the narrative that AI coding is undisciplined prompting. Highlight mechanical invariant gates, test-first boundary conditions, and formal state machines.
3. **Zero-Trust Egress & Universal Sanitization**: All artifacts, test fixtures, and logs are 100% sanitized of internal network topologies, private IPs, credentials, and confidential paths.
4. **Executable Demonstrations**: Pair every conceptual pattern with an executable, zero-dependency reference implementation under `examples/`.
5. **Autonomous Swarm Maintainability**: Maintain structured `AGENTS.md` operating instructions and GitHub Project tracking so AI agents can continuously curate, verify, and cross-reference new artifacts autonomously.
6. **Polyglot & Multi-Runtime Breadth**: Extend verification paradigms beyond Python to Rust, Go, TypeScript, and containerized runtime sandboxes.

---

## Release Milestones (Chronological Order)

### Milestone 1: Foundations & The `devops-cli` Retrospective (v0.1.0 - Completed)
- [x] **Repository Architecture & Manifest**: Established four-tier directory structure (`docs/`, `observations/`, `patterns/`, `artifacts/`), Apache 2.0 open-source licensing, and flagship `README.md`.
- [x] **Agent Operating Instructions (`AGENTS.md`)**: Codified strict curation protocols, zero-trust sanitization checklists, and five-part observation authoring standards.
- [x] **The Disciplined Agentic Manifesto (`docs/MANIFESTO.md`)**: The 5 Pillars of Disciplined Agentic Development.
- [x] **Taxonomy of Agentic Engineering (`docs/TAXONOMY.md`)**: Formalized vocabulary for execution models, context topologies, verification gates, and failure modes.
- [x] **Curation Guidelines (`docs/CURATION_GUIDELINES.md`)**: Sanitization rules (RFC 5737 IPs, `example.com` domains) and contribution templates.
- [x] **Foundational Case Studies (`observations/devops-cli/`)**:
  - `01-tdd-as-living-contract.md`: Anchoring LLM non-determinism with executable boundary oracles.
  - `02-architectural-invariants-and-complexity-caps.md`: Enforcing AST complexity $M \le 10$ and nesting $\le 5$.
  - `03-autonomous-project-governance.md`: State grounding via GitHub Projects v2 and atomic task specs.
  - `04-zero-trust-egress-and-sanitization.md`: Eliminating homelab leaks and standardizing dummy mock domains.
  - `05-harness-slots-and-subagent-offloading.md`: "Big decides, small types, big checks" multi-tier routing.
  - `06-rate-limits-and-anti-brittle-heuristics.md`: Surviving API quotas and eliminating arbitrary pattern lists.
- [x] **Operational Patterns (`patterns/`)**:
  - `cegis-and-hypothesis-debugging.md`: Formal constraint accumulation.
  - `fifo-pull-request-shepherding.md`: Chronological queue and GraphQL review thread resolution.
  - `root-cause-hardening.md`: Dual remediation in source code and `AGENTS.md`.
  - `epistemic-hygiene-and-context-pruning.md`: Multi-scale outlines and bounded string caps ($\le 256$ chars).
- [x] **Foundational Artifacts (`artifacts/`)**:
  - `prompts/multi-persona-code-reviewer.md`: Specialized review personas with XML boundary isolation.
  - `prompts/architectural-invariant-sentinel.md`: Pre-push architectural sentinel prompt.
  - `task-harnesses/structured-task-spec-template.md`: Persistent task tracking template.
  - `task-harnesses/sample-completed-task-spec.md`: Certified task completion example.
  - `schemas/fastmcp-agent-tool-manifest-spec.json`: FastMCP tool manifestation contract.

---

### Milestone 2: Interactive Testbeds & Executable Sample Apps (v0.2.0 - Completed)
- [x] **AST Invariant Sentinel Reference App (`examples/ast-invariant-sentinel/`)**: Executable Python static analyzer checking cyclomatic complexity, nesting depth, and IP sanitization with automated `pytest` test suite.
- [x] **FastMCP Token-Bucket Gateway Reference App (`examples/fastmcp-token-bucket-gateway/`)**: Asynchronous, client-side rate limiter and tool dispatcher with burst capacity and jittered backoff.
- [x] **CEGIS Debugging Workbench Reference App (`examples/cegis-debugging-workbench/`)**: Executable simulation of hypothesis-driven counterexample accumulation and candidate patch convergence.
- [x] **Automated CI Validation Workflow (`.github/workflows/ci.yml`)**: Continuous verification of all sample app test suites (`pytest`), AST Invariant Sentinel checks, and FastMCP schema integrity.
- [x] **Autonomous GitHub Project Tooling (`tools/project_tooling.py`)**: Rate-managed CLI and library for issue classification, acceptance criteria tracking, and recursive self-hardening auditing.
- [x] **Autonomous Workflow Suite (`.github/workflows/`)**:
  - `autonomous-triage.yml`: Automatic taxonomy classification, labeling, and onboarding comments.
  - `pr-sentinel.yml`: Invariant sentinel gating PRs with automatic feedback and certification labels.
  - `recursive-hardening.yml`: Closed-loop self-hardening verification on merged defect fixes.
- [x] **Polyglot Case Studies (`observations/polyglot/`)**:
  - `01-rust-type-state-invariants.md`: Type-state patterns and affine types eliminating state bugs at build time.
  - `02-typescript-cst-and-type-gymnastics.md`: Preserving generic contracts and avoiding `any` or `@ts-ignore`.

---

### Milestone 3: Multi-Agent Benchmark Suites & Observability Mesh (v0.3.0 - Active / In Flight)
- [x] **Standardized Agentic Benchmark Runner (`benchmarks/`)**:
  - `ComplexityRefactoring`: Complexity reduction rate and AST $M \le 10$ compliance on messy procedural code.
  - `CEGISConvergence`: Defect convergence velocity and candidate patch minimization.
  - `TokenEconomy`: Quantified token savings comparing monolithic frontier prompting vs. local subagent slot offloading.
- [x] **OpenTelemetry Agent Waterfall Trace Generator (`examples/agent-telemetry-trace-generator/`)**:
  - Reference trace generator emitting W3C traceparent spans, token usage attributes, and ASCII waterfall timelines.
- [x] **Interactive Prompt Mutation Suite & Invariant Fuzzer (`examples/prompt-mutation-fuzzer/`)**:
  - Grammar-guided adversarial prompt perturbation generator testing agent resilience against instruction dilution, distraction noise, prompt injection prefixes, and context truncation.
  - Quantified drift scoring: automatically measures frequency of AST invariant violations and contract regression under perturbed system instructions.
- [🔄] **Fast-Feedback Test Optimization & In-Process Runner**:
  - Phase 1 (Complete): Added `-o addopts=` worker bypass in `ResourceRunner`, dropping single-file test overhead by > 85% (from 4.4s down to 0.3s) and halving repository iteration time.
  - Phase 2 (Planned): In-process `pytest.main()` runner with warm module cache and stdout/stderr capture to slash multi-target execution down to < 2s.
- [ ] **Continuous File-Watcher Mode (`devops-cli / vibes --watch`)**:
  - Real-time inotify/event-driven daemon executing the 5-phase `Scan -> Run -> Review -> Feedback -> Iterate` loop on file save.
  - Instant ASCII diff display showing complexity delta ($\Delta M$) and test latency delta ($\Delta t$) immediately upon code modification.
- [ ] **Automated AST Conditional Refactorer (`tools/ast_refactorer.py`)**:
  - Mechanical AST rewriting engine that consumes `PROACTIVE_REFACTOR` opportunities from the Feedback Engine and auto-decomposes branching ladders ($M \ge 7$) into single-responsibility predicate helpers.
- [x] **Live OTLP Observability Mesh & Jaeger/Grafana Collector Pipeline**:
  - Reference export integration streaming agent spans over OpenTelemetry Protocol (OTLP/HTTP) into live Jaeger, Tempo, and Grafana collector endpoints (`examples/agent-telemetry-trace-generator/`).
  - Standardized semantic attributes for agent execution: `agent.persona`, `agent.tool.call_name`, `agent.tokens.prompt`, `agent.tokens.completion`, `agent.cache_hit`, and `agent.verification_result`.
  - CLI support for standard OTLP Protobuf-JSON (`--otlp`) and direct network streaming (`--export-otlp <endpoint>`).
- [ ] **Valkey L2 Caching for AST Repomaps & Embedding Drift Auditor**:
  - High-throughput Valkey L2 caching tier storing AST symbol tables, file digest hashes, and embeddings across subagent invocations.
  - Embedding drift auditor monitoring semantic degradation and cosine distance shifts when refactoring source modules.
- [ ] **Formal Tool Contract Verification Gates**:
  - Pydantic v2 + JSON Schema (Draft 2020-12) validation engine for all agent tool inputs and outputs.
  - Negative schema assertion harness detecting hallucinated parameters, undocumented flags, and unvalidated string inputs.

---

### Milestone 4: Polyglot Invariant Harnesses & Ephemeral Sandboxes (v0.4.0 - Planned)
- [ ] **Go Concurrency & Goroutine Leak Sentinel (`examples/go-leak-sentinel/`)**:
  - Executable reference tool utilizing Go runtime stack inspection (`runtime.NumGoroutine()`) and `pprof` trace analysis to detect orphaned goroutines, deadlock-prone unbuffered channels, and leaking context lifecycles.
  - Integration with `golangci-lint` AST checkers for static channel closure verification.
- [ ] **Rust Memory & Type-State Benchmark Track (`benchmarks/rust/`)**:
  - Specialized benchmark suite evaluating agent capability to synthesize memory-safe, zero-cost abstractions using Rust's affine ownership types and type-state builders.
  - Automated `cargo check` and `clippy` harness measuring compilation error resolution velocity and lifetime annotation precision.
- [ ] **Ephemeral Rootless Docker Sandbox Harness (`tools/sandbox/`)**:
  - Lightweight, isolated execution harness for executing untrusted agent-generated code inside unprivileged, rootless containers.
  - Hardened execution policies: seccomp system call filtering, cgroups v2 resource caps (CPU, memory, process limits), read-only root filesystems, and strict network egress deny-all policies (loopback only).
- [ ] **Polyglot CST Ingestion Engine (Tree-Sitter Multi-Language Parser)**:
  - Unified Concrete Syntax Tree (CST) parser powered by Tree-Sitter supporting Python, Rust, Go, TypeScript, C++, and Bash.
  - Language-agnostic cyclomatic complexity, nesting depth, and symbol dependency graph extractor.

---

### Milestone 5: Autonomous Swarm Orchestration & Self-Healing (v0.5.0 - Planned)
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
- [ ] **AI Bill of Materials (AIBOM) & Supply-Chain Security Scanner**:
  - Automated scanner generating AIBOM inventories mapping AI dependencies, pretrained weights provenance, Hugging Face model card hashes, and dataset licenses.
  - AST security sentinel scanning codebases for dangerous `trust_remote_code=True`, unpickling vulnerabilities, and unsafe model checkpoint loaders.
- [ ] **Adversarial Prompt Injection & CWE-200 Egress Security Scanner**:
  - Automated red-teaming scanner validating agent code and prompt templates against indirect prompt injection (via file content, tool outputs, or git commits).
  - Zero-Trust Egress Validator verifying zero leakage of environmental variables, secret tokens, or private RFC 1918 hostnames.
- [ ] **Enterprise Change Management & Version Deprecation Protocol**:
  - Full enterprise adoption playbook: Semantic Versioning 2.0.0 enforcement, runtime feature flags, structured multi-release deprecation warning cycles, and automated migration tooling for agent tool schemas.
- [ ] **Certified Living Standard for Agentic Engineering**:
  - Formal specification and certification test suite for AI coding assistants.
  - Standardized open compliance badge (`Passed Vibes Invariant Gate v1.0`) for repositories engineered with disciplined agentic workflows.

---

## Research & Observation Backlog (Living Research Queue)

Upcoming field observations, empirical studies, and architectural investigations to be authored and added to `observations/`:

| Topic / Working Title | Target Domain | Key Research Question |
|---|---|---|
| **Go Goroutine Leakage & Context Lifecycles** | `observations/polyglot/` | How do frontier models handle graceful cancellation and channel closure under high concurrency? |
| **C++ RAII & Lifetime Invariants Under LLM Synthesis** | `observations/polyglot/` | Can LLMs reliably avoid use-after-free and double-free bugs without Rust-like compile-time guarantees? |
| **eBPF Process Tracing for Agent Sandbox Introspection** | `observations/systems/` | Using eBPF probes to capture syscall patterns, file access, and network socket operations of subagents in real-time. |
| **Attention Dilution & Context Decay in Ultra-Long Sessions** | `observations/cognitive/` | Measuring degradation in constraint adherence as context lengths exceed 100k tokens and evaluating multi-scale pruning. |
| **Self-Correction Loops vs. Constraint Accumulation** | `observations/methodology/` | Comparing conversational "fix this error" prompting vs. formal CEGIS negative-constraint accumulation. |

---

## Value vs. Effort Prioritization Matrix

| Priority Category | Feature / Deliverable | Primary Tech / Tools | Value | Effort | Target Milestone | Status |
|---|---|---|---|---|---|---|
| **Quick Wins** | Executable Sample Apps (`sentinel`, `gateway`, `workbench`, `trace-gen`) | Python Standard Library | High | Low | v0.2.0 | ✅ Completed |
|  | Strategic Product Roadmap (`docs/ROADMAP.md`) | Markdown / Planning | High | Low | v0.2.0 | ✅ Completed |
|  | GitHub Actions CI & Recursive Workflows | GitHub Workflows / `pytest` | High | Low | v0.2.0 | ✅ Completed |
|  | Autonomous Project Tooling (`tools/project_tooling.py`) | Python Standard Library | High | Low | v0.2.0 | ✅ Completed |
|  | Polyglot Case Studies (Rust & TypeScript) | Systems Engineering | High | Low | v0.2.0 | ✅ Completed |
|  | Continuous File-Watcher Mode (`--watch`) | Python / inotify | High | Low | v0.3.0 | 📋 Scheduled |
|  | Formal Tool Contract Verification Gates | Pydantic v2 / JSON Schema | High | Low | v0.3.0 | 📋 Scheduled |
| **Major Projects** | Multi-Agent Benchmark Suite (`benchmarks/`) | `pytest` / AST Analyzer | High | Medium | v0.3.0 | ✅ Completed |
|  | OpenTelemetry Agent Waterfall Generator | OpenTelemetry / Python | High | Medium | v0.3.0 | ✅ Completed |
|  | Interactive Prompt Mutation Suite & Invariant Fuzzer | Python / AST / Fuzzing | High | Medium | v0.3.0 | ✅ Completed |
|  | Fast-Feedback Worker Pool & Test Runner | Pytest Daemon / Python | High | Medium | v0.3.0 | 📋 Scheduled |
|  | Automated AST Conditional Refactorer | Python AST Transformer | High | Medium | v0.3.0 | 📋 Scheduled |
|  | OTLP Live Collector & Jaeger/Grafana Mesh | OTLP / gRPC / Docker | High | Medium | v0.3.0 | 📋 Scheduled |
|  | Go Concurrency & Goroutine Leak Sentinel | Go 1.23 / `pprof` | High | Medium | v0.4.0 | 📋 Scheduled |
|  | Rust Memory & Type-State Benchmark Track | Cargo / Clippy / Rust | High | Medium | v0.4.0 | 📋 Scheduled |
|  | Rootless Docker Sandbox Harness | Docker / cgroups v2 | High | High | v0.4.0 | 📋 Scheduled |
|  | Polyglot Tree-Sitter CST Ingestion Engine | Tree-Sitter / Multi-Lang | High | High | v0.4.0 | 📋 Scheduled |
|  | Closed-Loop PR Triage & Invariant Review Bot | FastMCP / GitHub Actions | High | High | v0.5.0 | 📋 Scheduled |
|  | Autonomous Conversation Synthesizer | Python / NLP / Metrics | High | High | v0.5.0 | 📋 Scheduled |
|  | Live Multi-Model Leaderboard & Cost Index | Python / GitHub Pages | High | High | v0.5.0 | 📋 Scheduled |
|  | AIBOM & Supply-Chain Security Scanner | Python / AST / CycloneDX | High | High | v1.0.0 | 💡 Future Vision |
|  | Certified Living Standard & Compliance Badge | Specification / Test Suite | High | High | v1.0.0 | 💡 Future Vision |
| **Fill-Ins** | Community Issue Templates & PR Rubrics | GitHub Templates | Medium | Low | v0.1.0 | ✅ Completed |
|  | Valkey L2 AST Caching & Embedding Drift Auditor | Valkey / Redis / Vector | Medium | Medium | v0.3.0 | 📋 Scheduled |
|  | Adversarial Prompt Injection Security Scanner | Red-Teaming / Python | Medium | Medium | v1.0.0 | 💡 Future Vision |
| **Foundation** | Invariant Curation Guidelines & Sanitization | `AGENTS.md` / RFC 5737 | High | Medium | v0.1.0 | ✅ Completed |
|  | Disciplined Agentic Manifesto | `docs/MANIFESTO.md` | High | Medium | v0.1.0 | ✅ Completed |
|  | Taxonomy of Agentic Engineering | `docs/TAXONOMY.md` | High | Medium | v0.1.0 | ✅ Completed |
| **Anti-Patterns** | Toy "Chat with your Code" Demos | Generic LangChain wrappers | Low | High | — | ❌ Rejected (No toy demos) |
|  | Unsanitized Transcript Dumps | Raw log files | Low | Low | — | ❌ Rejected (Zero leakage) |
|  | Ad-Hoc Pattern Matching Subsets | Arbitrary regex lists | Low | Medium | — | ❌ Rejected (Use AST / RFCs) |
