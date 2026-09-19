# Tool Cardinality Saturation & Schema Context Taxation

## 1. Executive Context & Baseline

`devops-cli` exposes a rich MCP tool surface — over 100 FastMCP tools spanning `k8s_*`, `argo_*`, `docker_*`, `scan_*`, `gh_*`, `ai_*`, `vault_*`, `tf_*`, and `sandbox_*` namespaces — plus an internal `ToolSlot` registry (`src/devops_cli/ai/harness/slots.py:235-300`) and a `SkillSlot` capped at 20 dynamically loaded capability packages (`slots.py:150`). Each tool carries a full JSON Schema definition serialized into the system prompt or tool manifest.

When IDE agents (Copilot, Claude, Antigravity) connect to the FastMCP server, the entire tool schema corpus is hydrated into the model's context window before any user request is processed. This creates a structural tax: hundreds of `"type": "object", "properties": {...}, "required": [...]` blocks consuming thousands of tokens before the agent reasons about a single line of code.

## 2. The Observed Phenomenon

Three empirically grounded failure modes emerge as tool cardinality scales:

**2a. Selection Precision Collapse**: When the tool manifest exceeds ~40 tools with overlapping semantic domains, models increasingly select adjacent-but-wrong tools. Tools with similar verb prefixes (`scan_trivy` vs `scan_semgrep` vs `scan_checkov` vs `scan_gitleaks` vs `scan_complexity` vs `scan_aibom` vs `scan_sbom`) or noun suffixes (`k8s_status` vs `k8s_pods` vs `k8s_logs_query` vs `k8s_lint` vs `k8s_validate` vs `k8s_audit`) trigger vacillation — the model oscillates between plausible candidates across consecutive turns rather than committing to one.

**2b. Schema Token Budget Displacement**: A conservative estimate of 80 tokens per tool schema (name, description, parameters, required fields) means 100 tools consume ~8,000 tokens of context window before any code, conversation, or instructions are loaded. For models with 32K context windows, this represents a 25% fixed overhead. The `SkillSlot` cap of 20 skills (`slots.py:150`) implicitly acknowledges this budget ceiling but applies it only to skills, not tools.

**2c. Pretraining Prior Override**: Models override explicit schema parameter names in favor of pretraining priors. The `devops-cli` tool `review_path` requires `path` as a parameter, but models frequently hallucinate `file_path`, `filepath`, or `target` — names more common in pretraining corpora. The negative schema assertion (`additionalProperties: false` / `extra="forbid"`) from Observation 10 catches these at validation time, but the root cause is attention dilution across too many competing schema definitions.

## 3. The Underlying Failure Mode or Catalyst

The fundamental issue is **attention budget competition**. Transformer attention is a finite, shared resource. Every tool schema definition injected into context competes for attention weight with:

1. **System instructions** (`AGENTS.md` rules, safety constraints, formatting requirements)
2. **User intent** (the actual task description)
3. **Codebase context** (files, diffs, test output packed via `ContextPacker`)
4. **Conversation history** (prior turns, tool results, memory entries)

As tool count grows linearly, the attention allocated to any single tool's schema shrinks hyperbolically. The model's ability to precisely recall parameter names, required fields, and type constraints degrades — not because the information isn't present in context, but because attention weight has been redistributed across competing schema blocks.

This is compounded by **semantic namespace collision**. When 7 tools share the `scan_*` prefix, the model must discriminate based on the description string alone. But descriptions are often structurally similar ("Run X scanner on the workspace"), forcing the model into a probabilistic coin flip among plausible candidates.

## 4. Remediation & Architectural Pattern

**4a. Lazy Schema Hydration**: Rather than loading all 100+ tool schemas at session initialization, hydrate tool schemas on-demand based on the user's task domain. The MCP protocol already supports this via lazy-loaded tool discovery — `devops-cli` implements it with the `Lazy:` tool category in its MCP server manifest. Extend this pattern to eagerly load only a core subset (~10-15 high-frequency tools) and lazily resolve domain-specific tools when the agent's reasoning trajectory enters that namespace.

**4b. Hierarchical Tool Namespacing with Dispatcher**: Replace flat tool registries with a two-tier dispatch pattern. Expose a single `dispatch` meta-tool that accepts a `domain` parameter (`k8s`, `scan`, `gh`, `ai`, `docker`, `vault`, `tf`). The dispatcher returns the specific tool schemas for that domain only, amortizing schema hydration across the conversation rather than front-loading it.

**4c. Schema Compression via Structural Minimization**: Strip verbose `description` fields from tool schemas after the initial hydration turn. Models retain tool semantics from the first exposure; subsequent turns need only the structural skeleton (parameter names, types, required flags). This can reduce per-tool token cost from ~80 to ~30 tokens.

**4d. Negative Namespace Disambiguation**: For tools with overlapping prefixes, inject a concise disambiguation preamble: `"scan_trivy: CVE vulnerabilities. scan_semgrep: SAST code patterns. scan_gitleaks: leaked secrets."` This costs ~50 tokens total but eliminates the vacillation failure mode by providing a dense discriminative signal.

See pattern: [tool-cardinality-budget-management](../../patterns/tool-cardinality-budget-management.md)

## 5. Verifiable Impact & Key Takeaways

- **Budget arithmetic**: 100 tools × 80 tokens/schema = 8,000 tokens of fixed overhead (25% of a 32K window). Lazy hydration reduces this to ~1,200 tokens (15 core tools), reclaiming ~6,800 tokens for code context.
- **Vacillation elimination**: Namespace disambiguation preambles provide $O(1)$ discriminative signal versus $O(N)$ schema scanning, converting probabilistic tool selection into deterministic lookup.
- **The `SkillSlot` cap precedent**: The existing `max_skills = 20` ceiling in `slots.py:150` is the correct engineering instinct — cardinality must be bounded — but it applies only to skills while leaving the tool surface unbounded.

> **Aphorism**: A model with 100 tools in its prompt has zero tools it can reliably select. Cardinality is the enemy of precision; lazy hydration is the antidote.

> **The Schema Taxation Principle**: Every tool schema in the system prompt is a tax on every other token in the context window. Budget tools like you budget tokens — because they are tokens.
