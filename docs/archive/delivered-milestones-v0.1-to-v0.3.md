# Delivered Milestones: v0.1.0 through v0.3.0

> **Archive**: Completed roadmap scope, retained for provenance.
> **Why this is not in the roadmap**: a roadmap is a forward-looking document. Thirty-eight
> delivered items crowding eleven open ones made the next action harder to find, not easier.
> Nothing here is abandoned — every item shipped, and the detail is kept for anyone tracing
> why a subsystem exists.
>
> **TLDR**: Verbatim historical archive of delivered milestones v0.1.0 through v0.3.0, preserving feature provenance without cluttering the active roadmap.
>
> **ELI:7b**: A scrapbook of all the past goals and milestones this project has already finished and shipped.

See [`docs/ROADMAP.md`](../ROADMAP.md) for scope still ahead.

---

### Milestone 1: Foundations & The `devops-cli` Retrospective (v0.1.0 - Completed)
- [x] **Repository Architecture & Manifest**: Established four-tier directory structure (`docs/`, `observations/`, `patterns/`, `artifacts/`), Apache 2.0 open-source licensing, and flagship `README.md`.
- [x] **Agent Operating Instructions (`AGENTS.md`)**: Codified strict curation protocols, zero-trust sanitization checklists, and five-part observation authoring standards.
- [x] **The Disciplined Agentic Manifesto (`docs/MANIFESTO.md`)**: The 5 Pillars of Disciplined Agentic Development.
- [x] **Taxonomy of Agentic Engineering (`docs/TAXONOMY.md`)**: Formalized vocabulary for execution models, context topologies, verification gates, and failure modes.
- [x] **Curation Guidelines (`docs/CURATION_GUIDELINES.md`)**: Sanitization rules (RFC 5737 IPs, `example.com` domains) and contribution templates.
- [x] **Foundational Case Studies (`observations/devops-cli/`)**:
  - `01-tdd-as-living-contract.md`: Anchoring LLM non-determinism with executable boundary oracles.
  - `02-architectural-invariants-and-complexity-caps.md`: AST caps of complexity $M \le 10$ and nesting $\le 5$. In October 2026 `devops-cli` blocked nesting above 5 at pre-commit and reported complexity without blocking it.
  - `03-autonomous-project-governance.md`: State grounding via GitHub Projects v2 and atomic task specs.
  - `04-zero-trust-egress-and-sanitization.md`: Eliminating homelab leaks and standardizing dummy mock domains.
  - `05-harness-slots-and-subagent-offloading.md`: "Big decides, small types, big checks" multi-tier routing.
  - `06-rate-limits-and-anti-brittle-heuristics.md`: Surviving API quotas and eliminating arbitrary pattern lists.
- [x] **Operational Patterns (`patterns/`)**:
  - `cegis-and-hypothesis-debugging.md`: Counterexample constraint accumulation.
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
- [x] **FastMCP Token-Bucket Gateway Reference App (`examples/fastmcp-token-bucket-gateway/`)**: Asynchronous, client-side rate limiter and tool dispatcher with burst capacity and jittered backoff. It wraps plain async Python callables and uses neither the FastMCP library nor the Model Context Protocol.
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

### Milestone 3: Multi-Agent Benchmark Suites & Observability Mesh (v0.3.0 - Completed)
- [x] **Standardized Agentic Benchmark Runner (`benchmarks/`)**:
  - `ComplexityRefactoring`: Complexity reduction rate and AST $M \le 10$ compliance, measured on one built-in before-and-after code sample.
  - `CEGISConvergence`: A simulated convergence loop over fixed constants (5 rounds, 8 rejected candidates); no candidate patch is generated or evaluated.
  - `TokenEconomy`: Token savings computed from fixed illustrative token counts (`benchmarks/benchmark_runner.py`), comparing monolithic frontier prompting vs. local subagent slot offloading. No model is called.
- [x] **OpenTelemetry Agent Waterfall Trace Generator (`examples/agent-telemetry-trace-generator/`)**:
  - Reference trace generator emitting spans with W3C Trace Context IDs (16-byte trace ID, 8-byte span ID), token usage attributes, and ASCII waterfall timelines.
- [x] **Interactive Prompt Mutation Suite & Invariant Fuzzer (`examples/prompt-mutation-fuzzer/`)**:
  - Grammar-guided adversarial prompt perturbation generator testing agent resilience against instruction dilution, distraction noise, prompt injection prefixes, and context truncation.
  - Quantified drift scoring: automatically measures frequency of AST invariant violations and contract regression under perturbed system instructions.
- [x] **Fast-Feedback Test Optimization & Isolated Runner**:
  - Phase 1 (Complete): Added `-o addopts=` worker bypass in `ResourceRunner`, dropping single-file test overhead by > 85% (from 4.4s down to 0.3s) and halving repository iteration time.
  - Phase 2 (Complete): Implemented repository-level isolated `pytest.ini` preventing recursive parent configuration discovery and unpruned workspace plugin activation, achieving consistent sub-second (< 0.5s) test suite execution across all modules.
  - Phase 3 (Complete): Corrected latency oracle measurement validity — the 2.0s fast-feedback ceiling now evaluates pytest's self-reported suite duration (`RunExecutionResult.execution_seconds`) instead of subprocess wall-clock, eliminating a permanent false-positive defect where ~1.7s of fixed interpreter boot masked a 0.38s test body. Irreducible harness cost is surfaced separately via `harness_overhead_seconds`.
  - Phase 5 (Complete): **Directory Map Staleness Oracle** — `docs_validator.py` (`directory_map` rule) now diffs every embedded `text` tree against the filesystem: each mapped path must exist, and any directory the map enumerates must be enumerated completely, inferred per kind so a map listing only subdirectories is not faulted for omitting files. Deliberately partial listings declare an `...` entry. On its first run it found five omissions no other gate could see, including two root documents added in an earlier release. Trace waterfalls and AST dumps drawn with the same box characters are excluded structurally rather than by guesswork.
  - Phase 4 (Complete): Repository sweeps now execute through a single warm pytest session with per-file attribution via JUnit `xunit1` reports, which is the only family recording each test case's source file. Measured on this repository: 20.1s → 11.4s per full iteration (43% faster), with the fixed interpreter-and-collection cost paid once rather than 23 times. A suite missing from the session — collection error, timeout — falls back to its own subprocess, so the optimisation can never swallow a resource. Test attribution is also more accurate than the previous stdout parsing, which undercounted.
- [x] **Continuous File-Watcher Mode (`tools/resource_iteration_workbench.py --watch`)**:
  - Polling daemon (`ResourceWatcher`) that compares file modification times every `--watch-interval` seconds (default 1.0) and runs the 5-phase `Scan -> Run -> Review -> Feedback -> Iterate` loop when one changes. It does not use inotify.
  - Change detection discovering added, modified, or deleted Python modules across repository topology while ignoring virtual environments and bytecode caches.
  - CLI flags: `--watch`, `--watch-interval <seconds>`, and `--max-ticks <n>` with graceful `KeyboardInterrupt` signal handling.
- [x] **Automated AST Conditional Refactorer (`tools/ast_refactorer.py`)**:
  - Mechanical AST rewriting engine consuming `PROACTIVE_REFACTOR` opportunities from the Feedback Engine and auto-decomposing branching ladders ($M \ge 7$) and deep nesting into table-driven dictionary dispatch mappings, early-return guard clauses, pure predicate helpers, and consolidated assertion tuples.
  - Invariant safety verification gate re-evaluating syntax validity and cyclomatic complexity reduction, with unified diff output and `--from-feedback` backlog ingestion.
- [x] **Live OTLP Observability Mesh & Jaeger/Grafana Collector Pipeline**:
  - Reference export integration posting agent spans over OpenTelemetry Protocol (OTLP/HTTP) to a collector endpoint such as Jaeger or Tempo, for display in Grafana (`examples/agent-telemetry-trace-generator/`).
  - Agent execution attributes in a private vocabulary: `agent.persona`, `agent.tool.call_name`, `agent.tokens.prompt`, `agent.tokens.completion`, `agent.cache_hit`, and `agent.verification_result`. The generator now emits OpenTelemetry GenAI semantic convention keys instead ([RETROSPECTIVE §6](../RETROSPECTIVE.md#6-hard-won-field-incident-post-mortems--operational-lessons)).
  - CLI support for standard OTLP Protobuf-JSON (`--otlp`) and direct network streaming (`--export-otlp <endpoint>`).
- [x] **Valkey L2 Caching for AST Repomaps & Embedding Drift Auditor (`examples/valkey-l2-repomap-cache/`)**:
  - Two-tier caching coordinator (L1 fast memory + L2 Valkey distributed cache) content-addressed by SHA-256 digests.
  - Zero-dependency RESP wire protocol encoder/decoder with graceful in-memory mock fallback for offline and standalone CI execution.
  - Embedding drift auditor calculating normalized 8-dimensional structural vectors, Cosine Distance ($D_C$), and symbol topology diffs to detect semantic drift upon code refactoring.
- [x] **Formal Tool Contract Verification Gates (`examples/tool-contract-verifier/`)**:
  - Hand-written parameter contracts that validate tool-call arguments and check the return value's Python type. A contract exports as a JSON Schema Draft 2020-12 object, but no JSON Schema validator runs.
  - Negative schema assertion harness detecting hallucinated parameters, missing required fields, type mismatches, and unvalidated string lengths (CWE-400).
  - Structured corrective feedback emitting prescriptive error prompts back to the agent, so that the model can correct the call.

---
