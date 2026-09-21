# Changelog — vibes ✨

All notable changes to the `vibes` living showcase are documented here.  
Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased — Milestone 5 Prep]

### Added
- **Directory map staleness oracle** (`tools/docs_validator.py`, `directory_map` rule): diffs every embedded `text` directory tree against the filesystem — mapped paths must exist, and a directory the map enumerates must be enumerated completely, judged per kind so a map listing only subdirectories is not faulted for omitting files. Partial listings opt out with an `...` entry. Found five omissions on its first run (`CHANGELOG.md`, `CONTRIBUTING.md`, `pytest.ini`, `benchmarks/README.md`, `tools/verify_mermaid.mjs`), two of which had been absent from the map since the release that introduced them. Box-drawing art that is not a filesystem map (trace waterfalls, AST dumps) is excluded structurally: a map marks at least one child as a directory and most of what it names exists. 6 new unit tests.
- **Mermaid render gate** (`tools/verify_mermaid.mjs`): parses every diagram in the repository with the real Mermaid 11 engine under jsdom, because no textual rule can tell whether a diagram renders. Caught 2 unrenderable diagrams on its first run that passed every regex check. Accepts N paths and fails on missing targets per the gate-integrity pattern. Wired into `ci.yml` and documented in `CONTRIBUTING.md`.
- **22 new diagrams** across the five observations that had none (`devops-cli/18`, `19`, `20`, `systems/09`, `10`), the roadmap, six example READMEs, the contribution decision tree, the gate lifecycle, and the observability stack topology, using the diagram type that matches each mechanism: `stateDiagram-v2` for the one-way embedding batch ratchet (the point being the transitions that do not exist), `sequenceDiagram` for MCP's missing transaction envelope and for silent delegation failure absorption, `quadrantChart` for the value/effort matrix, `timeline` for the milestone arc, `pie` for context-window taxation, and `xychart-beta` for monotonic pipeline context growth.
- **Mermaid accessibility & modernity gates** (`tools/docs_validator.py`): new `mermaid_style` rule computing WCAG 2.1 relative luminance and rejecting any `fill:` declared without an explicit `color:` (an inherited label color flips with the GitHub theme and vanishes against the fill) or any pair below the 4.5:1 AA floor; extended `mermaid` rule rejecting the deprecated `graph` alias. Public `contrast_ratio()` helper. 5 new unit tests, including one asserting the documented palette satisfies the rule that enforces it.
- **Observation 11 — Silent Certification Failure & Gate Integrity** (`observations/systems/`): field study of four gates simultaneously reporting success over work they never performed, and the remediation that closed them.
- **Pattern — Gate Integrity & Total Input Coverage** (`patterns/`): making "nothing was checked" impossible to confuse with "everything passed"; total input coverage, `TargetIntegrity` on missing paths, self-reported metrics over harness wall-clock, and waivers that cannot become mute buttons.
- **Auditable waiver pragmas in the AST invariant sentinel** (`# sentinel: allow[<Invariant>] <justification>`): module-header waivers parsed from real comment tokens via `tokenize`, honored only in the first 15 lines and only for `ZeroTrustSanitization`. Structural caps are never waivable; malformed, unjustified, or non-waivable waivers raise `WaiverIntegrity` and do not suppress the underlying finding. Applied to the 5 fixture modules whose negative tests must embed private IPs. 7 new unit tests.
- **Change Management & Validation hardening** (this release):
  - `.pre-commit-config.yaml`: Git pre-commit / pre-push hooks enforcing the AST invariant sentinel, docs validator, pytest collection check, and JSON schema lint — converting `AGENTS.md` §8 mandates from convention to mechanical enforcement.
  - `CONTRIBUTING.md`: Unified contributor entry-point (previously fragmented across `AGENTS.md`, `docs/CURATION_GUIDELINES.md`, and `.github/` templates).
  - `CHANGELOG.md` (this file): Structured release history.
  - **Observation structure validator** (`tools/docs_validator.py`): New `check_observation_structure` check verifying that every numbered observation document (e.g. `01-foo.md`) under `observations/` contains numbered sections `## 1.` through `## 5.`. Integrated into `validate_file`, `validate_content`, and exposed as a public `DocsValidator.check_observation_structure()` method.
  - 4 new unit tests for observation structure: valid doc, missing sections, exempt non-observation files, real-repo compliance sweep.

### Changed
- **Single rule registry in the docs validator**: `validate_file` and `validate_content` each carried their own list of checks, so the `directory_map` rule added to one silently did not run on the other — including the CLI path, which is how it was discovered. Both now delegate to `_run_all_checks`, the one place a rule is registered, with a test asserting the two entry points return identical findings.
- **Unified fenced-block extraction**: the Mermaid and directory-map rules each hand-rolled a fence state machine, the second reaching M=7/depth=5 through an `elif` ladder — the AST nesting trap `AGENTS.md` §10.1 documents. Both now share `_extract_fenced_blocks`, and the dead `_is_mermaid_start` helper is removed.
- **Sentinel headroom restored** (`examples/ast-invariant-sentinel/sentinel.py`): the waiver and multi-path work left `_iter_header_comments` at M=5/depth=4 and `_expand_targets` at M=4/depth=4, which the workbench ranked as the repository's top proactive item. `_header_tokens` now separates *which tokens* (an `itertools.takewhile` generator bounded to the module header) from *which of them are comments*, and target expansion chains the per-path generators into a first-wins ordered mapping. Both land at depth 2 with no behavioural change.
- **Pre-push hook environment missing runtime dependencies**: `pytest-collection-check` declared only `[pytest, pytest-asyncio]`, but collection imports every test module and two of them import `yaml` and `markdown_it`, so the hook aborted with `ModuleNotFoundError` and blocked the push. The gap was invisible while the `python3.12` pin prevented the hook environment from building at all. Dependencies now mirror `ci.yml`.
- **Pre-commit interpreter pin relaxed**: `default_language_version` pinned `python3.12`, so every hook aborted with `failed to find interpreter for Builtin discover of python_spec='python3.12'` on any machine without that exact minor version — including the Python 3.14 devcontainer this repository develops in, where installing the hooks made commits impossible. Now resolves `python3`. CI matrix extended to 3.12, 3.13, and 3.14.
- **Semicolons in sequence diagram text rejected**: Mermaid treats `;` as a statement separator in `sequenceDiagram` blocks, silently truncating the message and failing to parse the remainder. New `mermaid` rule catches it in 200ms locally; the render gate catches the general class in CI. 3 new unit tests.
- **Diagram palette standardized and migrated**: the ad-hoc `#f66`/`#6a6` fills used across 17 styled nodes rendered white labels at 2.8:1 — below the WCAG AA floor and unreadable for low-vision readers in both GitHub themes. Replaced with an 8-entry documented palette (`failure`, `success`, `caution`, `accent`, `neutral`, and three subgraph `zone*` containers) spanning 6.5:1 to 18.5:1. All 9 legacy `graph TD|LR` declarations modernized to `flowchart`. `AGENTS.md` §6 now specifies the palette, the `classDef` idiom, and a diagram-type selection table.
- **Stale directory map regenerated**: `README.md` listed observations frozen at `devops-cli/15` and `systems/07` (20 and 11 exist) and 13 of 16 patterns. Text trees break no links and fail no test, so the drift was invisible to every gate; a `docs_validator` rule to diff embedded trees against the filesystem is queued as Milestone 3 Phase 5.
- **Stale index counts corrected**: `observations/README.md` simultaneously claimed 32 case studies (scope line) and 25 (three body references) against 33 actual files, and reported two divergent test totals (192/192 and 217/217); the root `README.md` claimed 21. All now read 34 observations and 209/209 tests.
- **`ci.yml` completely overhauled**: Replaced Milestone 2 subset (5 specific test files) with the full `pytest tests/ examples/ benchmarks/` test run, expanded sentinel audit to cover all `tools/`, and added `docs_validator.py --strict` and observation structure compliance as CI gates. Matrix remains Python 3.12 & 3.13.

### Fixed
- **Sentinel verdict depended on how a file was named on the command line**: `_collect_py_targets` honored `skip_tests=True` when expanding a directory but returned any explicitly named file unconditionally, so `sentinel.py tests/` skipped a module that `sentinel.py tests/test_x.py` audited. With pre-commit now correctly passing every staged filename (above), staging a test file began flagging the negative fixtures that the detectors are tested against. The `skip_tests` exemption is removed — all 41 Python files are now audited uniformly, which also puts test modules under the §10.2 complexity caps they were always supposed to satisfy (they already pass: zero structural violations).
- **Unaudited `tests/` directory in CI**: `ci.yml` enumerated `examples`, `tools`, and a single benchmark file, leaving 6 test modules ungated — the `structure-driven-ci-directory-contracts` anti-pattern this repository documents. Replaced with one invocation over `tools examples tests benchmarks`; `pr-sentinel.yml` now audits all changed files in a single consolidated run; `CONTRIBUTING.md` matches.
- **Sentinel certified nonexistent paths as clean**: a typo'd or deleted target expanded to zero files and printed `✅ All architectural invariants PASSED!`. Missing targets now raise `TargetIntegrity`.
- **Sentinel and docs validator silently audited only the first supplied path**: `sentinel.py` read `argv[1]` alone and `docs_validator.py` declared `nargs="?"`, while `AGENTS.md` §8.2 instructs agents to run `sentinel.py <modified_files>` (plural) and `.pre-commit-config.yaml` invokes the sentinel with `pass_filenames: true`. Every pre-commit run and every documented pre-push audit therefore certified the first staged file and silently ignored the rest, printing `✅ All architectural invariants PASSED!` over unread code. Both entrypoints now parse `nargs="*"` via `argparse`, aggregate findings across all targets (`audit_targets`, `_merge_reports`), deduplicate overlapping directory/file arguments, and reject unknown flags non-zero. 6 new unit tests; `AGENTS.md` §8.4 codifies the guardrail.
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
