# Changelog — vibes ✨

All notable changes to the `vibes` living showcase are documented here.  
Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased — Milestone 5 Prep]

### Added
- **Change Management & Validation hardening** (this release):
  - `.pre-commit-config.yaml`: Git pre-commit / pre-push hooks enforcing the AST invariant sentinel, docs validator, pytest collection check, and JSON schema lint — converting `AGENTS.md` §8 mandates from convention to mechanical enforcement.
  - `CONTRIBUTING.md`: Unified contributor entry-point (previously fragmented across `AGENTS.md`, `docs/CURATION_GUIDELINES.md`, and `.github/` templates).
  - `CHANGELOG.md` (this file): Structured release history.
  - **Observation structure validator** (`tools/docs_validator.py`): New `check_observation_structure` check verifying that every numbered observation document (e.g. `01-foo.md`) under `observations/` contains numbered sections `## 1.` through `## 5.`. Integrated into `validate_file`, `validate_content`, and exposed as a public `DocsValidator.check_observation_structure()` method.
  - 4 new unit tests for observation structure: valid doc, missing sections, exempt non-observation files, real-repo compliance sweep.

### Changed
- **`ci.yml` completely overhauled**: Replaced Milestone 2 subset (5 specific test files) with the full `pytest tests/ examples/ benchmarks/` test run, expanded sentinel audit to cover all `tools/`, and added `docs_validator.py --strict` and observation structure compliance as CI gates. Matrix remains Python 3.12 & 3.13.

### Fixed
- **Latency oracle measured the harness, not the tests** (`tools/resource_iteration_workbench.py`): the 2.0s fast-feedback ceiling compared against subprocess wall-clock, which includes ~1.7s of fixed interpreter boot, plugin loading, and collection. `test_crawler.py` was reported at 2.04s while its actual suite body runs in 0.38s — an unfixable, permanent false-positive defect that the SDLC manager ranked as the repository's top action item. `RunExecutionResult` now parses pytest's self-reported summary duration (`execution_seconds`), exposes `feedback_latency_seconds` and `harness_overhead_seconds`, and the ceiling is evaluated against test execution only. 4 new unit tests; `AGENTS.md` §11.4 codifies the guardrail.
- **Observation 12 missing `## 5.` section**: `12-polyglot-cst-boundary-guards-and-symlink-containment.md` was missing its `Verifiable Impact & Key Takeaways` section (discovered mechanically by the new structure validator); section added with concrete OOM/ELOOP/boundary-escape containment metrics.

---

## [v0.4.0 — Milestone 4: Polyglot Invariant Harnesses & Ephemeral Sandboxes]

### Added
- **Agentic IDE Lifecycle Hook Sentinel** (`examples/agentic-ide-hook-sentinel/`): Pre-/post-tool IDE hook with zero-trust egress, POSIX process group containment, and LSP diagnostic regression gating (10 tests).
- **High-Performance AST Context Packer** (`examples/binary-search-context-packer/`): Monotonic binary search truncation in $O(\log N)$ for prompt token budget packing (7 tests).
- **Streaming Reasoning Token Parser** (`examples/streaming-reasoning-sanitizer/`): Streaming FSM with boundary prefix buffers, bounded thought accumulators, and isolated thought telemetry (8 tests).
- **Automated Documentation Self-Healing** (`tools/docs_validator.py --fix`): Mermaid label quoting, unclosed fence repair, and absolute URI normalization.
- **Thread-safety hardening** in `DocsValidator`: Thread-local `MarkdownIt` instances eliminating `list index out of range` race conditions in concurrent directory scans.
- **Systems Observation 07**: Agentic IDE Protocols & LSP/MCP/ACP Tri-Protocol Convergence.
- **devops-cli Observations 13–15**: Streaming reasoning sanitization, binary search context packing, structured output repair.
- **Patterns**: `streaming-reasoning-isolation-and-token-budgeting.md`, `binary-search-context-packing.md`, `agentic-ide-lifecycle-hooks-and-lsp-oracles.md`.
- **Automated Assertion Consolidation Engine** in `tools/ast_refactorer.py`.
- **Ephemeral Container Backend** for CEGIS patch evaluation.
- **Zero-Dependency Docs Validator** (initial version) with CommonMark, Mermaid, table, link, snippet, and HTML tag validation.
- **Go Goroutine Leak Sentinel** (`examples/go-leak-sentinel/`).
- **Rust Type-State Benchmark Track** (`benchmarks/rust/`).
- **Rootless Docker Sandbox Harness** (`examples/ephemeral-container-sandbox/`).
- **Polyglot CST Ingestion Engine** (`examples/polyglot-cst-parser/`).

---

## [v0.3.0 — Milestone 3: Multi-Agent Benchmarks & Observability Mesh]

### Added
- **Standardized Agentic Benchmark Runner** (`benchmarks/`): `ComplexityRefactoring`, `CEGISConvergence`, `TokenEconomy` benchmark tracks.
- **OpenTelemetry Agent Waterfall Trace Generator** (`examples/agent-telemetry-trace-generator/`).
- **Prompt Mutation Suite & Invariant Fuzzer** (`examples/prompt-mutation-fuzzer/`).
- **Continuous File-Watcher Mode** (`--watch`, `ResourceWatcher`): inotify-driven 5-phase Scan→Run→Review→Feedback→Iterate daemon.
- **Automated AST Conditional Refactorer** (`tools/ast_refactorer.py`): Mechanical `elif` flattening, guard clause extraction, table-driven dispatch.
- **Live OTLP Observability Mesh**: OTLP/HTTP export to Jaeger, Tempo, Grafana.
- **Valkey L2 Caching & Embedding Drift Auditor** (`examples/valkey-l2-repomap-cache/`).
- **Formal Tool Contract Verification Gates** (`examples/tool-contract-verifier/`).
- **Fast-Feedback Test Optimization**: Isolated `pytest.ini`, sub-second single-suite execution.
- **Systems Observations 01–06** and **Polyglot Observations** (Go goroutines, Rust type-state, TypeScript CST).

---

## [v0.2.0 — Milestone 2: Interactive Testbeds & Executable Sample Apps]

### Added
- **AST Invariant Sentinel Reference App** (`examples/ast-invariant-sentinel/`).
- **FastMCP Token-Bucket Gateway** (`examples/fastmcp-token-bucket-gateway/`).
- **CEGIS Debugging Workbench** (`examples/cegis-debugging-workbench/`).
- **CI Workflow** (`.github/workflows/ci.yml`): Initial Python 3.12/3.13 matrix.
- **Autonomous Project Tooling** (`tools/project_tooling.py`).
- **Autonomous Workflow Suite**: `autonomous-triage.yml`, `pr-sentinel.yml`, `recursive-hardening.yml`.
- **Polyglot Case Studies**: Rust type-state patterns, TypeScript CST gymnastics.

---

## [v0.1.0 — Milestone 1: Foundations & The devops-cli Retrospective]

### Added
- Repository architecture: four-tier directory structure (`docs/`, `observations/`, `patterns/`, `artifacts/`).
- `AGENTS.md` curation protocol and agent operating instructions.
- `docs/MANIFESTO.md`: The 5 Pillars of Disciplined Agentic Development.
- `docs/TAXONOMY.md`: Formal vocabulary for agentic engineering.
- `docs/CURATION_GUIDELINES.md`: Sanitization checklist (RFC 5737, `example.com`).
- Foundational observations `01–06` (TDD contracts, architectural invariants, autonomous governance, zero-trust egress, harness slots, rate limits).
- Operational patterns: CEGIS, FIFO PR shepherding, root-cause hardening, epistemic hygiene.
- `artifacts/`: FastMCP tool manifest schema, multi-persona reviewer prompts, task harness templates.
- GitHub issue/PR templates and community governance files.
