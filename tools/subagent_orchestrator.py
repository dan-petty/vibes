#!/usr/bin/env python3
"""Hierarchical Subagent Slot Offloading Orchestrator.

Implements the "Big decides, small types, big checks" architectural pattern.
Coordinates tiered delegation allocating high-level planning to frontier reasoning models,
mechanical exploration and atomic typing to local open-weights slots, and verification
to deterministic AST oracles. Preserves invariant constraints across delegation boundaries
and prevents epistemic distortion.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

# Default pricing catalog (USD per 1M tokens)
DEFAULT_PRICING: Final[dict[str, tuple[float, float, bool]]] = {
    "claude-3-5-sonnet": (3.00, 15.00, False),
    "gpt-4o": (2.50, 10.00, False),
    "deepseek-v3": (0.27, 1.10, False),
    "qwen-2.5-coder-32b": (0.20, 0.60, False),
    "qwen-2.5-coder-7b-local": (0.00, 0.00, True),
}

NESTING_NODE_TYPES: Final[tuple[type[ast.AST], ...]] = (
    ast.If,
    ast.For,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.AsyncFor,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)

DECISION_NODE_TYPES: Final[tuple[type[ast.AST], ...]] = (
    ast.If,
    ast.For,
    ast.While,
    ast.ExceptHandler,
    ast.With,
    ast.Assert,
    ast.BoolOp,
)


class SlotRole(StrEnum):
    """Subagent functional slot role in the orchestration hierarchy."""

    PLANNER = "planner"
    FILE_SCOUT = "file_scout"
    AST_ANALYZER = "ast_analyzer"
    CODE_TYPER = "code_typer"
    VERIFIER = "verifier"


class TaskStatus(StrEnum):
    """Lifecycle status of a delegated subtask."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class InvariantConstraint:
    """An architectural invariant constraint that must not be stripped across boundaries."""

    constraint_id: str
    description: str
    max_complexity: int = 10
    max_depth: int = 5
    required_symbols: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SubagentTask:
    """A bounded atomic unit of work delegated to a specialized subagent slot."""

    task_id: str
    title: str
    role: SlotRole
    assigned_model: str
    prompt: str
    constraints: tuple[InvariantConstraint, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SubagentResult:
    """The structured result emitted by a subagent upon completing a subtask."""

    task_id: str
    role: SlotRole
    model_id: str
    status: TaskStatus
    output_content: str
    prompt_tokens: int
    completion_tokens: int
    duration_seconds: float
    error: str = ""
    extracted_symbols: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OrchestrationMetrics:
    """Quantitative token, economic, and invariant metrics for an orchestration run."""

    frontier_tokens: int
    offloaded_tokens: int
    total_tokens: int
    frontier_cost_usd: float
    actual_cost_usd: float
    dollar_savings: float
    offload_efficiency_ratio: float
    invariants_verified: int
    invariants_violated: int


@dataclass
class ExecutionRecord:
    """Complete record of an orchestrated multi-agent execution session."""

    plan_id: str
    goal: str
    timestamp: str
    status: TaskStatus
    results: list[SubagentResult] = field(default_factory=list)
    metrics: OrchestrationMetrics = field(
        default_factory=lambda: OrchestrationMetrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0)
    )
    final_synthesis: str = ""


def calculate_cost(prompt_tokens: int, completion_tokens: int, model_id: str) -> float:
    """Calculate token expenditure in USD given token usage and model ID."""
    rates = DEFAULT_PRICING.get(model_id, (1.0, 3.0, False))
    in_rate, out_rate, is_local = rates
    if is_local:
        return 0.0
    in_cost = (max(0, prompt_tokens) * in_rate) / 1_000_000.0
    out_cost = (max(0, completion_tokens) * out_rate) / 1_000_000.0
    return in_cost + out_cost


def _calculate_mccabe(tree: ast.AST) -> int:
    """Calculate McCabe cyclomatic complexity of an AST node."""
    decision_points = 1
    for node in ast.walk(tree):
        if isinstance(node, DECISION_NODE_TYPES):
            decision_points += 1
    return decision_points


def _walk_depth(node: ast.AST, depth: int) -> int:
    """Recursively calculate the maximum nesting depth under an AST node."""
    next_depth = depth + 1 if isinstance(node, NESTING_NODE_TYPES) else depth
    max_d = next_depth
    for child in ast.iter_child_nodes(node):
        max_d = max(max_d, _walk_depth(child, next_depth))
    return max_d


def _calculate_max_depth(tree: ast.AST) -> int:
    """Calculate maximum block nesting depth in an AST."""
    statements = [n for n in ast.iter_child_nodes(tree) if isinstance(n, ast.stmt)]
    if not statements:
        return 1
    return max(_walk_depth(stmt, 1) for stmt in statements)


def _extract_symbol_name(node: ast.AST) -> str | None:
    """Return symbol name if node is a public function or class definition."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
        return node.name
    return None


def extract_public_symbols(code: str) -> tuple[str, ...]:
    """Extract public top-level functions and classes from Python source code."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ()
    names = (_extract_symbol_name(n) for n in ast.iter_child_nodes(tree))
    return tuple(n for n in names if n is not None)


def verify_code_invariants(code: str, constraint: InvariantConstraint) -> tuple[bool, str]:
    """Verify that Python code satisfies complexity, depth, and symbol invariants."""
    if not code.strip():
        return True, "Empty code passes trivially."
    try:
        tree = ast.parse(code)
    except SyntaxError as err:
        return False, f"SyntaxError in generated code: {err}"

    complexity = _calculate_mccabe(tree)
    if complexity > constraint.max_complexity:
        return False, f"Cyclomatic complexity {complexity} exceeds ceiling {constraint.max_complexity}"

    depth = _calculate_max_depth(tree)
    if depth > constraint.max_depth:
        return False, f"Nesting depth {depth} exceeds ceiling {constraint.max_depth}"

    extracted = extract_public_symbols(code)
    missing = [sym for sym in constraint.required_symbols if sym not in extracted]
    if missing:
        return False, f"Preservation violation: missing required symbol(s) {missing}"

    return True, "All invariants verified."


def format_constraint_envelope(prompt: str, constraints: Sequence[InvariantConstraint]) -> str:
    """Wrap a subtask prompt in an immutable structural constraint envelope."""
    if not constraints:
        return prompt
    lines = [
        prompt,
        "",
        "### 🛡️ MANDATORY INVARIANT CONSTRAINTS (DO NOT STRIP)",
    ]
    for c in constraints:
        lines.append(f"- **{c.constraint_id}**: {c.description}")
        lines.append(f"  Ceilings: Max Complexity M <= {c.max_complexity}, Max Nesting Depth <= {c.max_depth}")
        if c.required_symbols:
            lines.append(f"  Required Public Symbols: {', '.join(c.required_symbols)}")
    return "\n".join(lines)


class SubagentOrchestrator:
    """Orchestrates hierarchical subagent task delegation and verification."""

    def __init__(self, frontier_model: str = "claude-3-5-sonnet") -> None:
        """Initialize orchestrator with designated frontier model ID."""
        self.frontier_model = frontier_model

    def evaluate_task_result(self, task: SubagentTask, output: str) -> tuple[TaskStatus, str]:
        """Verify task output against all declared constraints."""
        if not output.strip():
            return TaskStatus.FAILED, "Subagent returned empty response payload."

        for constraint in task.constraints:
            passed, reason = verify_code_invariants(output, constraint)
            if not passed:
                return TaskStatus.REJECTED, f"Constraint {constraint.constraint_id} violated: {reason}"

        return TaskStatus.COMPLETED, ""

    def _tally_tokens(self, results: Sequence[SubagentResult]) -> tuple[int, int, int]:
        """Tally frontier tokens, offloaded tokens, and total tokens."""
        frontier_tok = 0
        offloaded_tok = 0
        for r in results:
            tok = r.prompt_tokens + r.completion_tokens
            if r.role == SlotRole.PLANNER or r.role == SlotRole.VERIFIER:
                frontier_tok += tok
            else:
                offloaded_tok += tok
        return frontier_tok, offloaded_tok, frontier_tok + offloaded_tok

    def _tally_costs(self, results: Sequence[SubagentResult]) -> tuple[float, float, float]:
        """Calculate hypothetical frontier cost, actual cost, and net dollar savings."""
        hypothetical_frontier = 0.0
        actual_cost = 0.0
        for r in results:
            hypothetical_frontier += calculate_cost(r.prompt_tokens, r.completion_tokens, self.frontier_model)
            actual_cost += calculate_cost(r.prompt_tokens, r.completion_tokens, r.model_id)
        savings = max(0.0, hypothetical_frontier - actual_cost)
        return round(hypothetical_frontier, 6), round(actual_cost, 6), round(savings, 6)

    def _tally_invariants(self, results: Sequence[SubagentResult]) -> tuple[int, int]:
        """Count satisfied and violated invariant checks across results."""
        verified = sum(1 for r in results if r.status == TaskStatus.COMPLETED)
        violated = sum(1 for r in results if r.status == TaskStatus.REJECTED)
        return verified, violated

    def calculate_metrics(self, results: Sequence[SubagentResult]) -> OrchestrationMetrics:
        """Aggregate token economies and invariant adherence across subtasks."""
        f_tok, o_tok, tot_tok = self._tally_tokens(results)
        f_cost, a_cost, savings = self._tally_costs(results)
        verified, violated = self._tally_invariants(results)
        ratio = round((o_tok / max(1, tot_tok)) * 100.0, 1)

        return OrchestrationMetrics(
            frontier_tokens=f_tok,
            offloaded_tokens=o_tok,
            total_tokens=tot_tok,
            frontier_cost_usd=f_cost,
            actual_cost_usd=a_cost,
            dollar_savings=savings,
            offload_efficiency_ratio=ratio,
            invariants_verified=verified,
            invariants_violated=violated,
        )

    def execute_mock_demo(self, plan_id: str = "demo-slot-offload") -> ExecutionRecord:
        """Execute a realistic multi-tier mock demonstration of slot offloading."""
        goal = "Refactor monolithic dispatcher into table-driven handlers with type hints"
        constraint = InvariantConstraint(
            constraint_id="INV-SLOT-001",
            description="Preserve public dispatch function with M<=6 and depth<=2",
            max_complexity=6,
            max_depth=2,
            required_symbols=("dispatch_request",),
        )

        plan_task = SubagentTask(
            task_id="task-01-plan",
            title="Architectural Planning & Invariant Envelope",
            role=SlotRole.PLANNER,
            assigned_model="claude-3-5-sonnet",
            prompt="Plan refactoring of dispatcher into table dispatch.",
            constraints=(constraint,),
        )
        scout_task = SubagentTask(
            task_id="task-02-scout",
            title="Filesystem & Symbol Scouting",
            role=SlotRole.FILE_SCOUT,
            assigned_model="qwen-2.5-coder-7b-local",
            prompt="Find dispatcher files and callers.",
        )
        type_task = SubagentTask(
            task_id="task-03-type",
            title="Atomic Typing & Function Extraction",
            role=SlotRole.CODE_TYPER,
            assigned_model="qwen-2.5-coder-32b",
            prompt="Draft typed table-driven dispatch function.",
            constraints=(constraint,),
        )
        verify_task = SubagentTask(
            task_id="task-04-verify",
            title="Deterministic Invariant Gate & CI Check",
            role=SlotRole.VERIFIER,
            assigned_model="claude-3-5-sonnet",
            prompt="Audit generated Python code against AST invariants.",
            constraints=(constraint,),
        )

        code_output = (
            'def dispatch_request(route: str, payload: dict[str, str]) -> str:\n'
            '    """Dispatch incoming request based on matching table key."""\n'
            '    handlers = {"login": "auth_ok", "logout": "bye"}\n'
            '    return handlers.get(route, "not_found")\n'
        )

        results = [
            SubagentResult(plan_task.task_id, plan_task.role, plan_task.assigned_model, TaskStatus.COMPLETED, "Plan approved.", 1200, 300, 1.2),
            SubagentResult(scout_task.task_id, scout_task.role, scout_task.assigned_model, TaskStatus.COMPLETED, "Found 2 files: dispatcher.py, test_dispatcher.py", 800, 150, 0.4),
            SubagentResult(type_task.task_id, type_task.role, type_task.assigned_model, TaskStatus.COMPLETED, code_output, 2400, 450, 0.9, extracted_symbols=("dispatch_request",)),
            SubagentResult(verify_task.task_id, verify_task.role, verify_task.assigned_model, TaskStatus.COMPLETED, "All invariants verified (M=1, depth=1).", 900, 180, 0.6),
        ]

        metrics = self.calculate_metrics(results)
        return ExecutionRecord(
            plan_id=plan_id,
            goal=goal,
            timestamp=datetime.now(UTC).isoformat(),
            status=TaskStatus.COMPLETED,
            results=results,
            metrics=metrics,
            final_synthesis="Refactoring complete. 60% of tokens offloaded to local/open-weights slots with zero epistemic loss.",
        )


def _format_markdown_sequence(record: ExecutionRecord) -> str:
    """Generate Mermaid sequence diagram showing tiered delegation flow."""
    lines = [
        "```mermaid",
        "sequenceDiagram",
        "    autonumber",
        "    participant P as Frontier Planner (Big Decides)",
        "    participant S as Local Scout (Small Types)",
        "    participant T as Open-Weights Typer (Small Types)",
        "    participant V as AST Verifier (Big Checks)",
        "",
        '    P->>S: Delegate file & symbol scouting',
        '    S-->>P: Return candidate file paths & symbol map',
        '    P->>T: Delegate typed refactoring (with Invariant Envelope)',
        '    T-->>V: Forward generated source for verification',
        '    V-->>P: Invariant gate passed (M<=6, depth<=2)',
        "```",
    ]
    return "\n".join(lines)


def format_markdown_report(record: ExecutionRecord) -> str:
    """Format an ExecutionRecord into GitHub-flavored Markdown."""
    m = record.metrics
    lines = [
        "# 🤖 Hierarchical Subagent Slot Offloading Report",
        "",
        f"> **Plan**: `{record.plan_id}` | **Status**: `{record.status}` | **Timestamp**: `{record.timestamp}`",
        f"> **Goal**: *{record.goal}*",
        "",
        "## 1. Executive Summary & Token Economics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| **Frontier Tokens** | {m.frontier_tokens:,} |",
        f"| **Offloaded Tokens** | {m.offloaded_tokens:,} |",
        f"| **Total Tokens** | {m.total_tokens:,} |",
        f"| **Offload Efficiency** | **{m.offload_efficiency_ratio}%** |",
        f"| **Hypothetical Frontier Cost** | ${m.frontier_cost_usd:.4f} |",
        f"| **Actual Orchestrated Cost** | ${m.actual_cost_usd:.4f} |",
        f"| **Net Dollar Savings** | **${m.dollar_savings:.4f}** |",
        f"| **Invariants Verified** | {m.invariants_verified} |",
        f"| **Invariants Violated** | {m.invariants_violated} |",
        "",
        "## 2. Multi-Tier Delegation Flow",
        "",
        _format_markdown_sequence(record),
        "",
        "## 3. Subtask Execution Breakdown",
        "",
        "| Task ID | Role | Model | Status | Tokens | Latency |",
        "|---|---|---|:---:|:---:|:---:|",
    ]
    for r in record.results:
        toks = r.prompt_tokens + r.completion_tokens
        lines.append(f"| `{r.task_id}` | {r.role} | `{r.model_id}` | `{r.status}` | {toks:,} | {r.duration_seconds:.2f}s |")

    lines.extend([
        "",
        "## 4. Synthesis & Architectural Guidance",
        "",
        f"{record.final_synthesis}",
        "",
        "- **Preserve Invariant Envelopes**: Always pass structured invariants (`InvariantConstraint`) to prevent prompt compression loss.",
        "- **Explicit Failure Signals**: Reject invalid outputs immediately with `TaskStatus.REJECTED` rather than silently hallucinating success.",
        "- **Leverage Local Slots for Chores**: Scout files and generate type annotations on local models to reduce frontier expenditure.",
    ])
    return "\n".join(lines)


def format_json_report(record: ExecutionRecord) -> str:
    """Format an ExecutionRecord into indented JSON."""
    return json.dumps(asdict(record), indent=2)


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for subagent orchestrator."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    demo_parser = subparsers.add_parser("demo", help="Run multi-tier slot offloading demonstration")
    demo_parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    demo_parser.add_argument("--output", type=Path, default=None, help="Optional output destination")

    verify_parser = subparsers.add_parser("verify", help="Audit code against invariant constraints")
    verify_parser.add_argument("code_file", type=Path, help="Path to Python file to verify")
    verify_parser.add_argument("--max-complexity", type=int, default=10, help="Maximum allowed complexity")
    verify_parser.add_argument("--max-depth", type=int, default=5, help="Maximum allowed nesting depth")

    return parser


def _execute_demo(format_choice: str, output: Path | None) -> int:
    """Execute demo tiered run and output report."""
    orchestrator = SubagentOrchestrator()
    record = orchestrator.execute_mock_demo()
    text = format_markdown_report(record) if format_choice == "markdown" else format_json_report(record)
    if output:
        output.write_text(text, encoding="utf-8")
        print(f"Report written to {output}")
    else:
        print(text)
    return 0


def _execute_verify(file_path: Path, max_c: int, max_d: int) -> int:
    """Audit Python source code file against invariants."""
    if not file_path.exists():
        print(f"Error: file {file_path} not found.", file=sys.stderr)
        return 1
    code = file_path.read_text(encoding="utf-8")
    constraint = InvariantConstraint(
        constraint_id="CLI-VERIFY",
        description="CLI file audit",
        max_complexity=max_c,
        max_depth=max_d,
    )
    passed, reason = verify_code_invariants(code, constraint)
    if passed:
        print(f"✓ {file_path.name} passed invariant checks (M<={max_c}, depth<={max_d}).")
        return 0
    print(f"❌ {file_path.name} failed invariant checks: {reason}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for Subagent Orchestrator."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "demo":
        return _execute_demo(args.format, args.output)
    if args.command == "verify":
        return _execute_verify(args.code_file, args.max_complexity, args.max_depth)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
