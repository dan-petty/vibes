# Observation 42: Language Server Protocol & Automated AST Invariant Repair

> **Project**: AST Invariant Sentinel & Developer Tooling Integration  
> **Environment**: Python 3.12+ AST, Language Server Protocol (LSP 3.17), static analysis, astral-sh/ruff  
> **Classification**: Developer Ergonomics, Language Server Protocol, Automated Remediation, Static Analysis  
> **Related**: [Observation 41 (Systems)](./41-radon-block-complexity-ranking-in-code-smell-quantification.md), [Observation 40 (Systems)](./40-configurable-prose-style-guides-and-inclusive-terminology-gates.md), [Pattern: LSP Diagnostics and Automated Invariant Repair](../../patterns/lsp-diagnostics-and-automated-invariant-repair.md), [Exhibit: AST Invariant Sentinel](../../examples/ast-invariant-sentinel/)
> **TLDR**: Deliver in-editor diagnostics and automated QuickFix remediations, checked with `ast.parse`, from an in-process Language Server Protocol server.
> **ELI:7b**: Give the AI assistant instant compiler feedback and automated one-click fixes right inside VS Code as it writes code.

---

## 1. Executive Context & Baseline

The AST Invariant Sentinel ([`examples/ast-invariant-sentinel/sentinel.py`](../../examples/ast-invariant-sentinel/sentinel.py)) provides mechanical enforcement of non-negotiable architectural bounds ($M \le 10$, nesting depth $\le 5$, zero-trust sanitization, waiver integrity). It reports code that breaks these bounds, and its pre-commit hook blocks such commits.

Historically, however, the sentinel operated exclusively as an offline batch analyzer: invoked via CLI commands (`python3 sentinel.py <paths>`) or triggered during git pre-commit and pre-push hooks.

When evaluated against modern static analysis benchmarks (such as `astral-sh/ruff`), the comparative landscape survey ([`tools/landscape_survey.py`](../../tools/landscape_survey.py)) identified two notable capability gaps under `complexity-gating`:

1. **`auto_fix`**: The capability to rewrite source files to mechanically resolve detected invariant violations rather than solely halting the build.
2. **`editor_lsp`**: Native implementation of the Language Server Protocol (LSP 3.17) to provide real-time, in-editor feedback and code actions directly within developer IDE planes (VS Code, Cursor, Neovim, Zed).

Both gaps carried the survey disposition `integrate` — meaning the capability could be achieved by combining repository oracles with existing dependencies (`astral-sh/ruff`).

---

## 2. The Observed Phenomenon: Batch-Gate Rejection vs. Real-Time Feedback

In traditional static analysis workflows, architectural invariant gates operate at the boundary of git commits. When an AI agent or human engineer introduces a private IP, a non-canonical mock domain, or excessive control flow nesting, the violation remains invisible throughout authoring.

The feedback loop is delayed until pre-commit interception:

```mermaid
flowchart TD
    subgraph BatchLoop["Traditional Batch-Gate Workflow (High Latency)"]
        A1["Author Code in Editor"] --> A2["Stage Changes (git add)"]
        A2 --> A3["Run Pre-Commit Hook"]
        A3 -->|"Fail: M > 10 or Private IP"| A4["Context Switch: Read Terminal Log"]
        A4 --> A5["Manual Code Edit"]
        A5 --> A2
    end

    subgraph LSPLoop["LSP + Auto-Fix Workflow (Real-Time Sub-Second Feedback)"]
        B1["Type Code in Editor"] -->|textDocument/didChange| B2["In-Process Sentinel LSP Server"]
        B2 -->|textDocument/publishDiagnostics| B3["Inline Squiggly Diagnostics"]
        B3 -->|textDocument/codeAction| B4["One-Click QuickFix Remediation"]
        B4 -->|WorkspaceEdit applied| B1
    end
```

Under batch validation, a breach is found only at commit time, which requires repeated context switching between editor, terminal, and commit staging. Under Language Server Protocol integration, the editor shows the violation as you type, before the file is saved to disk. No round-trip times were measured.

---

## 3. The Underlying Failure Mode or Catalyst

Why did earlier iterations omit native LSP and automated remediation?

1. **Perceived Protocol Weight**: Many engineering teams believe Language Server Protocol implementations require heavy frameworks (e.g., `pygls` or Node.js runtime bridges). Incorporating substantial external frameworks would violate the sentinel’s core architectural principle: providing a standalone, zero-dependency reference exhibit that can run anywhere.
2. **The Mutation Hesitation Trap**: Automated source rewriters risk introducing syntax regressions if transformations are applied blindly. Without atomic round-trip AST verification, tool authors hesitate to mutate source code in place.
3. **Decoupled Editor Tooling**: Tooling was treated as an external gatekeeper rather than an interactive pair programming partner, leaving editors reliant on generic linters that lack knowledge of repository-specific zero-trust invariants.

---

## 4. The Prescribed Architectural Solution

To close both survey gaps while preserving the sentinel's zero-dependency footprint, we implemented native `auto_fix` and a complete in-process Language Server Protocol server:

### A. Lean, Zero-Dependency JSON-RPC Stdio LSP Server

We implemented the core Language Server Protocol (LSP 3.17) using standard library `json`, `io`, and `sys.stdin.buffer` / `sys.stdout.buffer`:

- **Framing**: Content-Length header parsing (`_read_lsp_headers`, `_read_lsp_message`) and serialized response emission (`_write_lsp_message`).
- **Lifecycle**: Handles `initialize`, `shutdown`, and `exit`, and ignores `initialized`. It is not fully compliant: an unknown request gets a `null` result instead of a `MethodNotFound` error, and the server exits with code 0 even without a prior `shutdown`.
- **Diagnostics**: Listens on `textDocument/didOpen`, `textDocument/didChange`, and `textDocument/didClose`, evaluating in-memory source trees via `audit_source` and publishing 0-indexed diagnostics via `textDocument/publishDiagnostics`.
- **QuickFix Code Actions**: Listens on `textDocument/codeAction`, synthesizing `WorkspaceEdit` payloads that allow editors to auto-remediate violations with a single click or keyboard shortcut.
- **Ruff Server Alternative**: `run_ruff_server()` runs `ruff server` instead of the sentinel server when `ruff` is on `PATH`, and falls back to the sentinel server otherwise. It does not combine the two servers.

### B. Safe, AST-Verified Source Auto-Remediation (`auto_fix_source`)

The auto-fix engine performs deterministic mechanical repairs in two domains, and it leaves waivers to a person:

1. **Zero-Trust IP Sanitization**: Identifies prohibited private and link-local IPv4 addresses (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`) in string literals and rewrites them to RFC 5737 safe documentation addresses (`192.0.2.1`).
2. **Mock Domain Canonicalization**: Normalizes non-canonical subdomains (`https://api.example.com`) to the standardized root dummy domain (`https://example.com`).
3. **No Waiver Justification Repair**: A bare or short `# sentinel: allow[...]` waiver stays as written, and the audit reports it as `WaiverIntegrity`. Only a person knows why a waiver exists. An earlier version added the text "sanitized mock reference", which passed the 12-character rule and gave no reason.
4. **Pre-Write AST Invariant Verification**: Verifies candidate output with `ast.parse` before emitting changes; if candidate code fails syntax parsing, the repair is aborted, so a repair never writes code that does not parse. The check does not show that behavior is unchanged: a rewritten address changes what the code connects to.

---

## 5. Verification & Key Metrics

1. **Closed Survey Capability Gaps**: Closed both `auto_fix` and `editor_lsp` gaps in `docs/landscape/capabilities.yaml`. Reduced total landscape gaps from 9 to 7, and completely eliminated all `integrate` gaps across the entire repository.
2. **In-Memory Execution**: `audit_source()` on a generated 999-line Python file took about 15 ms (best of 20 runs, measured on 2026-11-19).
3. **AST Invariant Compliance**: All new LSP and auto-fix functions strictly adhere to AST Invariant Sentinel `--preset strict` ceilings ($M \le 6$, depth $\le 3$, parameters $\le 4$).
4. **Comprehensive Test Suite**: Added 8 unit tests in `examples/ast-invariant-sentinel/test_sentinel.py` (totaling 61 passing tests) verifying framing, diagnostics emission, code actions, and CLI `--fix` execution.
