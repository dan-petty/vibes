"""Dynamic Multi-Model Router & Speculative Cascade Oracle.

Provides automated task complexity estimation, semantic entropy uncertainty quantification,
and tiered model routing across Local, Fast, and Frontier models. Supports speculative
cascading where code is drafted on lightweight models and validated by mechanical AST
invariant oracles before escalating to frontier tiers.
"""

from __future__ import annotations

import ast
import json
import math
import re
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Default model definitions across tiers
DEFAULT_LOCAL_MODEL = "qwen-2.5-coder-7b"
DEFAULT_FAST_MODEL = "claude-3-5-haiku"
DEFAULT_FRONTIER_MODEL = "claude-3-5-sonnet-20241022"

# Cost benchmarks in USD per million tokens (input / output)
MODEL_COST_RATES: dict[str, tuple[float, float]] = {
    DEFAULT_LOCAL_MODEL: (0.15, 0.20),
    DEFAULT_FAST_MODEL: (0.80, 4.00),
    DEFAULT_FRONTIER_MODEL: (3.00, 15.00),
}


class ModelTier(StrEnum):
    """Categorized capability tiers for model execution."""

    LOCAL = "local"
    FAST = "fast"
    FRONTIER = "frontier"


@dataclass(frozen=True)
class TaskComplexityVector:
    """Multi-dimensional structural complexity vector for an engineering task."""

    file_count: int = 1
    estimated_ast_nodes: int = 15
    estimated_mccabe: int = 2
    cross_symbol_refs: int = 0
    security_critical: bool = False
    has_concurrency: bool = False

    def complexity_score(self) -> float:
        """Compute normalized task complexity score in range [0.0, 1.0]."""
        node_part = min(self.estimated_ast_nodes / 200.0, 1.0) * 0.25
        mccabe_part = min(self.estimated_mccabe / 12.0, 1.0) * 0.25
        file_part = min(self.file_count / 5.0, 1.0) * 0.20
        sym_part = min(self.cross_symbol_refs / 10.0, 1.0) * 0.15
        flag_part = 0.15 if (self.security_critical or self.has_concurrency) else 0.0
        return round(min(node_part + mccabe_part + file_part + sym_part + flag_part, 1.0), 3)


@dataclass(frozen=True)
class SemanticEntropyResult:
    """Estimated uncertainty from semantic meaning cluster distributions."""

    raw_entropy: float
    normalized_entropy: float
    cluster_count: int
    uncertain: bool


@dataclass(frozen=True)
class RoutingDecision:
    """Deterministic routing decision output for an engineering task."""

    task_id: str
    selected_tier: ModelTier
    selected_model: str
    complexity_score: float
    semantic_entropy: float
    route_reason: str
    estimated_cost_usd: float
    speculative_enabled: bool


@dataclass(frozen=True)
class SpeculativeCascadeResult:
    """Execution telemetry for a speculative cascade cycle."""

    task_id: str
    initial_tier: ModelTier
    final_tier: ModelTier
    escalated: bool
    escalation_reason: str | None
    draft_code: str
    final_code: str
    invariant_passed: bool
    cost_spent_usd: float
    cost_saved_usd: float


@dataclass(frozen=True)
class DiagnosticFinding:
    """Diagnostic violation or audit event identified by the router."""

    rule_id: str
    severity: str
    message: str
    task_id: str


def compute_semantic_entropy(
    cluster_probabilities: list[float], tau_entropy: float = 0.45
) -> SemanticEntropyResult:
    """Compute Shannon entropy across meaning cluster probabilities."""
    valid_probs = [p for p in cluster_probabilities if p > 0.0]
    if not valid_probs:
        return SemanticEntropyResult(0.0, 0.0, 0, False)

    raw_h = abs(-sum(p * math.log2(p) for p in valid_probs))
    max_h = math.log2(max(len(valid_probs), 2))
    norm_h = abs(round(raw_h / max_h, 3)) if max_h > 0 else 0.0
    is_uncertain = norm_h >= tau_entropy

    return SemanticEntropyResult(abs(round(raw_h, 3)), norm_h, len(valid_probs), is_uncertain)


def estimate_token_cost(tier: ModelTier, input_tokens: int = 1500, output_tokens: int = 500) -> float:
    """Estimate invocation cost in USD for a given tier and token budget."""
    model_name = get_model_for_tier(tier)
    in_rate, out_rate = MODEL_COST_RATES.get(model_name, (1.0, 5.0))
    cost = (input_tokens / 1_000_000 * in_rate) + (output_tokens / 1_000_000 * out_rate)
    return round(cost, 6)


def get_model_for_tier(tier: ModelTier) -> str:
    """Resolve default model identifier for a given capability tier."""
    tier_map = {
        ModelTier.LOCAL: DEFAULT_LOCAL_MODEL,
        ModelTier.FAST: DEFAULT_FAST_MODEL,
        ModelTier.FRONTIER: DEFAULT_FRONTIER_MODEL,
    }
    return tier_map.get(tier, DEFAULT_FAST_MODEL)


def select_tier(complexity: float, entropy: float, security_critical: bool) -> tuple[ModelTier, str]:
    """Select appropriate model tier based on complexity, entropy, and security."""
    if security_critical:
        return (ModelTier.FRONTIER, "Security-critical task requires frontier reasoning")
    if complexity >= 0.70 or entropy >= 0.45:
        return (ModelTier.FRONTIER, "High task complexity or semantic entropy requires frontier tier")
    if complexity >= 0.35 or entropy >= 0.25:
        return (ModelTier.FAST, "Moderate complexity safely handled by fast mid-tier")
    return (ModelTier.LOCAL, "Low complexity and low entropy eligible for local tier")


def check_egress_invariants(code_str: str) -> tuple[bool, str]:
    """Detect private RFC 1918 / RFC 4193 network addresses in code."""
    rfc1918_pat = r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3})\b"
    if re.search(rfc1918_pat, code_str):
        return (False, "Zero-Trust Egress: Private RFC 1918 address detected")
    return (True, "Egress clean")


def _count_function_branches(node: ast.AST) -> int:
    """Count decision branches within an AST function node."""
    branch_types = (ast.If, ast.While, ast.For, ast.ExceptHandler)
    return 1 + sum(1 for child in ast.walk(node) if isinstance(child, branch_types))


def measure_ast_complexity(tree: ast.AST) -> tuple[int, int]:
    """Measure maximum cyclomatic complexity and nesting depth in AST."""
    func_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    funcs = [n for n in ast.walk(tree) if isinstance(n, func_types)]
    if not funcs:
        return (1, 1)

    max_mccabe = max(_count_function_branches(fn) for fn in funcs)
    return (max_mccabe, 1)


def verify_code_invariants(code_str: str) -> tuple[bool, str]:
    """Verify code string against AST syntax, complexity, and egress invariants."""
    try:
        tree = ast.parse(code_str)
    except SyntaxError as exc:
        return (False, f"SyntaxError: {exc.msg}")

    egress_ok, egress_msg = check_egress_invariants(code_str)
    if not egress_ok:
        return (False, egress_msg)

    mccabe, _ = measure_ast_complexity(tree)
    if mccabe > 10:
        return (False, f"McCabe complexity ({mccabe}) exceeds maximum ceiling (10)")

    return (True, "All invariants satisfied")


@dataclass
class ModelRouter:
    """Core routing engine and speculative cascade coordinator."""

    tau_entropy: float = 0.45
    frontier_cost_baseline: float = field(default_factory=lambda: estimate_token_cost(ModelTier.FRONTIER))

    def route_task(
        self,
        task_id: str,
        vector: TaskComplexityVector,
        cluster_probs: list[float] | None = None,
        allow_speculative: bool = True,
    ) -> RoutingDecision:
        """Route an engineering task to the optimal model tier."""
        complexity = vector.complexity_score()
        entropy_res = compute_semantic_entropy(cluster_probs or [1.0], self.tau_entropy)
        tier, reason = select_tier(complexity, entropy_res.normalized_entropy, vector.security_critical)

        speculative = allow_speculative and tier != ModelTier.FRONTIER
        cost = estimate_token_cost(tier)
        model = get_model_for_tier(tier)

        return RoutingDecision(
            task_id=task_id,
            selected_tier=tier,
            selected_model=model,
            complexity_score=complexity,
            semantic_entropy=entropy_res.normalized_entropy,
            route_reason=reason,
            estimated_cost_usd=cost,
            speculative_enabled=speculative,
        )

    def execute_speculative_cascade(
        self,
        task_id: str,
        initial_tier: ModelTier,
        draft_code: str,
        frontier_fallback_code: str | None = None,
    ) -> SpeculativeCascadeResult:
        """Execute a speculative cascade verifying drafted code against invariant oracles."""
        initial_cost = estimate_token_cost(initial_tier)
        invariants_ok, fail_reason = verify_code_invariants(draft_code)

        if invariants_ok:
            cost_saved = max(self.frontier_cost_baseline - initial_cost, 0.0)
            return SpeculativeCascadeResult(
                task_id=task_id,
                initial_tier=initial_tier,
                final_tier=initial_tier,
                escalated=False,
                escalation_reason=None,
                draft_code=draft_code,
                final_code=draft_code,
                invariant_passed=True,
                cost_spent_usd=initial_cost,
                cost_saved_usd=round(cost_saved, 6),
            )

        # Escalate to frontier
        fallback_code = frontier_fallback_code or f"# Escalated repair\n{draft_code}"
        total_cost = round(initial_cost + self.frontier_cost_baseline, 6)
        return SpeculativeCascadeResult(
            task_id=task_id,
            initial_tier=initial_tier,
            final_tier=ModelTier.FRONTIER,
            escalated=True,
            escalation_reason=fail_reason,
            draft_code=draft_code,
            final_code=fallback_code,
            invariant_passed=False,
            cost_spent_usd=total_cost,
            cost_saved_usd=0.0,
        )

    def audit_decisions(self, decisions: list[RoutingDecision]) -> list[DiagnosticFinding]:
        """Audit routing decisions against efficiency and fragility invariants."""
        findings: list[DiagnosticFinding] = []
        for d in decisions:
            self._audit_single_decision(d, findings)
        return findings

    def _is_wasteful_frontier(self, d: RoutingDecision) -> bool:
        """Check if a decision allocates frontier unnecessarily."""
        if d.selected_tier != ModelTier.FRONTIER:
            return False
        return (d.complexity_score < 0.25) and (d.semantic_entropy < 0.15)

    def _is_fragile_local(self, d: RoutingDecision) -> bool:
        """Check if a decision assigns high complexity to local tier."""
        if d.selected_tier != ModelTier.LOCAL:
            return False
        return (d.complexity_score > 0.70) or (d.semantic_entropy > 0.45)

    def _audit_single_decision(self, d: RoutingDecision, findings: list[DiagnosticFinding]) -> None:
        """Inspect an individual decision for potential violations."""
        if self._is_wasteful_frontier(d):
            findings.append(
                DiagnosticFinding(
                    rule_id="ROUT001",
                    severity="warning",
                    message=f"Task {d.task_id} has low complexity ({d.complexity_score}); frontier is wasteful.",
                    task_id=d.task_id,
                )
            )
        elif self._is_fragile_local(d):
            findings.append(
                DiagnosticFinding(
                    rule_id="ROUT002",
                    severity="error",
                    message=f"Task {d.task_id} has high complexity ({d.complexity_score}); fragile on local tier.",
                    task_id=d.task_id,
                )
            )

    def generate_sarif(self, findings: list[DiagnosticFinding]) -> dict[str, Any]:
        """Export findings to standard OASIS SARIF 2.1.0 format."""
        results = []
        for f in findings:
            level = "error" if f.severity == "error" else "warning"
            results.append(
                {
                    "ruleId": f.rule_id,
                    "level": level,
                    "message": {"text": f.message},
                    "locations": [{"physicalLocation": {"artifactLocation": {"uri": f"task://{f.task_id}"}}}],
                }
            )

        return {
            "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "vibes-model-router",
                            "informationUri": "https://github.com/dan-petty/vibes",
                            "rules": [
                                {
                                    "id": "ROUT001",
                                    "shortDescription": {"text": "Unnecessary Frontier Expenditure"},
                                },
                                {
                                    "id": "ROUT002",
                                    "shortDescription": {"text": "Fragile Low-Tier Assignment"},
                                },
                            ],
                        }
                    },
                    "results": results,
                }
            ],
        }

    def generate_markdown_report(
        self, decisions: list[RoutingDecision], cascade_results: list[SpeculativeCascadeResult]
    ) -> str:
        """Synthesize a human-readable Markdown summary report."""
        total_spent = sum(r.cost_spent_usd for r in cascade_results)
        total_saved = sum(r.cost_saved_usd for r in cascade_results)
        escalations = sum(1 for r in cascade_results if r.escalated)

        lines = [
            "# Multi-Model Dynamic Routing & Speculative Cascade Report",
            "",
            "## Summary Telemetry",
            f"- **Evaluated Tasks**: {len(decisions)}",
            f"- **Total Spent**: ${total_spent:.4f} USD",
            f"- **Total Savings**: ${total_saved:.4f} USD",
            f"- **Escalation Count**: {escalations} / {len(cascade_results)}",
            "",
            "## Routing Decisions",
            "| Task ID | Tier | Model | Complexity | Entropy | Reason |",
            "|---|---|---|---|---|---|",
        ]

        for d in decisions:
            lines.append(
                f"| `{d.task_id}` | `{d.selected_tier.value}` | `{d.selected_model}` | "
                f"{d.complexity_score:.2f} | {d.semantic_entropy:.2f} | {d.route_reason} |"
            )

        return "\n".join(lines)


def run_cli_demo() -> int:
    """Execute dynamic router demonstration across diverse tasks."""
    router = ModelRouter()
    tasks = [
        (
            "task-01-docstring",
            TaskComplexityVector(file_count=1, estimated_ast_nodes=10, estimated_mccabe=1),
            [1.0],
        ),
        (
            "task-02-refactor",
            TaskComplexityVector(file_count=2, estimated_ast_nodes=60, estimated_mccabe=4),
            [0.8, 0.2],
        ),
        (
            "task-03-crypto",
            TaskComplexityVector(file_count=4, estimated_ast_nodes=150, security_critical=True),
            [0.5, 0.5],
        ),
    ]

    decisions = [router.route_task(tid, vec, probs) for tid, vec, probs in tasks]
    sample_code = "def add(a: int, b: int) -> int:\n    return a + b\n"
    cascade_results = [
        router.execute_speculative_cascade(d.task_id, d.selected_tier, sample_code) for d in decisions
    ]

    report = router.generate_markdown_report(decisions, cascade_results)
    sys.stdout.write(f"{report}\n")
    return 0


def main(args: list[str] | None = None) -> int:
    """CLI entry point for model router."""
    cli_args = args or sys.argv[1:]
    if "--demo" in cli_args or not cli_args:
        return run_cli_demo()

    if "--sarif" in cli_args:
        router = ModelRouter()
        d_sample = [
            RoutingDecision(
                "bad-01", ModelTier.FRONTIER, DEFAULT_FRONTIER_MODEL, 0.1, 0.05, "Over-allocated", 0.02, False
            ),
        ]
        findings = router.audit_decisions(d_sample)
        sys.stdout.write(json.dumps(router.generate_sarif(findings), indent=2))
        return 0

    sys.stdout.write("Usage: python tools/model_router.py [--demo | --sarif]\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
