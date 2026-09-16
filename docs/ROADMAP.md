# Strategic Roadmap — vibes ✨

High-density product roadmap, engineering milestones, and open-source curation strategy for the `vibes` living showcase and artifact repository.

---

## Core Vision & Design Principles

1. **Living Laboratory Over Static Museum**: `vibes` is an active workbench. Every observation must be grounded in empirical engineering; every pattern must have a reproducible code or harness artifact.
2. **Beyond "Vibe Coding"**: Systematically counter the narrative that AI coding is undisciplined prompting. Highlight mechanical invariant gates, test-first boundary conditions, and formal state machines.
3. **Zero-Trust Egress & Universal Sanitization**: All artifacts, test fixtures, and logs are 100% sanitized of internal network topologies, private IPs, credentials, and confidential paths.
4. **Executable Demonstrations**: Pair every conceptual pattern with an executable, zero-dependency reference implementation under `examples/`.
5. **Autonomous Swarm Maintainability**: Maintain structured `AGENTS.md` operating instructions and GitHub Project tracking so AI agents can continuously curate, verify, and cross-reference new artifacts autonomously.

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
  - `02-architectural-invariants-and-complexity-caps.md`: Enforcing AST complexity $\le 10$ and nesting $\le 5$.
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

### Milestone 2: Interactive Testbeds & Executable Sample Apps (v0.2.0 - Active / In Flight)
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

### Milestone 3: Multi-Agent Benchmark Suites & Telemetry Mesh (v0.3.0 - Active)
- [x] **Standardized Agentic Benchmark Runner (`benchmarks/`)**:
  - `ComplexityRefactoring`: Complexity reduction rate and AST $M \le 10$ compliance on messy procedural code.
  - `CEGISConvergence`: Defect convergence velocity and candidate patch minimization.
  - `TokenEconomy`: Quantified token savings comparing monolithic prompting vs. subagent slot offloading.
- [x] **OpenTelemetry Agent Waterfall Trace Generator (`examples/agent-telemetry-trace-generator/`)**:
  - Reference trace generator emitting W3C traceparent spans, token usage attributes, and ASCII waterfall timelines.
- [ ] **Interactive Prompt Mutation Suite**: Automated tool that mutates prompts and evaluates whether agent behavior degrades or adheres to invariants.

### Milestone 4: Autonomous Swarm Curation & Enterprise Playbooks (v1.0.0 - North Star)
- [ ] **Self-Curating PR Review Bot**: Automated GitHub Action running the Multi-Persona Reviewer and AST Sentinel on incoming community pull requests.
- [ ] **Automated Case Study Synthesizer**: Tooling to ingest real conversation transcripts, redact secrets, extract metrics, and draft structured observation reports.
- [ ] **Enterprise Agent Governance Playbook**: Full organizational adoption guide: security risk matrices, token budget forecasting, and human-in-the-loop review boundaries.

---

## Value vs. Effort Prioritization Matrix

| Priority Category | Feature / Deliverable | Primary Resource | Value | Effort | Target Milestone | Status |
|---|---|---|---|---|---|---|
| **Quick Wins** | Executable Sample Apps (`sentinel`, `gateway`, `workbench`, `trace-gen`) | Python Standard Library | High | Low | v0.2.0 | ✅ Completed |
|  | Strategic Product Roadmap (`docs/ROADMAP.md`) | Markdown / Planning | High | Low | v0.2.0 | ✅ Completed |
|  | GitHub Actions CI & Recursive Workflows | GitHub Workflows / `pytest` | High | Low | v0.2.0 | ✅ Completed |
|  | Autonomous Project Tooling (`tools/`) | Python Standard Library | High | Low | v0.2.0 | ✅ Completed |
|  | Polyglot Case Studies (Rust & TypeScript) | Systems Engineering | High | Low | v0.2.0 | ✅ Completed |
| **Major Projects** | Multi-Agent Benchmark Suite (`benchmarks/`) | `pytest` / AST Analyzer | High | Medium | v0.3.0 | ✅ Completed |
|  | OpenTelemetry Agent Waterfall Generator | OpenTelemetry / Python | High | Medium | v0.3.0 | ✅ Completed |
|  | Self-Curating PR Review & Invariant Bot | FastMCP / GitHub Actions | High | High | v1.0.0 | 💡 Future Vision |
| **Fill-Ins** | Interactive Prompt Mutation Runner | Python / Pydantic | Medium | Low | v0.3.0 | 💡 Future Vision |
|  | Community Issue Templates & PR Rubrics | GitHub Templates | Medium | Low | v0.1.0 | ✅ Completed |
| **Foundation** | Invariant Curation Guidelines & Sanitization | `AGENTS.md` / RFC 5737 | High | Medium | v0.1.0 | ✅ Completed |
|  | Disciplined Agentic Manifesto | `docs/MANIFESTO.md` | High | Medium | v0.1.0 | ✅ Completed |
| **De-prioritized** | Toy "Chat with your Code" Demos | Generic LangChain wrappers | Low | High | — | ❌ Rejected (No toy demos) |
|  | Unsanitized Transcript Dumps | Raw log files | Low | Low | — | ❌ Rejected (Zero leakage) |
