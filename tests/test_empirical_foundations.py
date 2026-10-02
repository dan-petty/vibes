"""Tests for empirical foundations and critical synthesis of agentic assertions.

Verifies the behavioral characteristics, boundary conditions, and failure modes
analyzed in docs/EMPIRICAL_FOUNDATIONS.md, including:
1. Greedy vs. lazy dictionary dispatch evaluation.
2. Branch smuggling past naive cyclomatic complexity counters.
3. Test suite overfitting vs. property-based generalization.
4. Micro-function fragmentation call overhead.
"""

from __future__ import annotations

import ast
from collections.abc import Callable

# --- 1. Greedy vs Lazy Dictionary Dispatch ---

def eager_dispatch_sample(actions: list[str]) -> dict[str, str]:
    """Demonstrates eager evaluation executing side effects at dict instantiation time."""
    result = {
        "action_a": _side_effect_logger(actions, "executed_a"),
        "action_b": _side_effect_logger(actions, "executed_b"),
    }
    return result


def lazy_dispatch_sample(
    actions: list[str],
) -> dict[str, Callable[[], str]]:
    """Demonstrates lazy evaluation deferring execution until invocation."""
    return {
        "action_a": lambda: _side_effect_logger(actions, "executed_a"),
        "action_b": lambda: _side_effect_logger(actions, "executed_b"),
    }


def _side_effect_logger(log_target: list[str], message: str) -> str:
    """Helper appending message to log target and returning message."""
    log_target.append(message)
    return message


def test_greedy_vs_lazy_dispatch_evaluation() -> None:
    """Asserts that eager dictionary creation executes all branches unconditionally."""
    eager_log: list[str] = []
    lazy_log: list[str] = []

    _ = eager_dispatch_sample(eager_log)
    lazy_table = lazy_dispatch_sample(lazy_log)

    # In eager dispatch, both actions ran even though neither was requested
    assert (len(eager_log), len(lazy_log)) == (2, 0)

    # In lazy dispatch, only the requested action runs
    result = lazy_table["action_a"]()
    assert (result, len(lazy_log), lazy_log) == ("executed_a", 1, ["executed_a"])


# --- 2. Branch Smuggling & AST Cyclomatic Metrics ---

def naive_ast_branch_counter(code: str) -> int:
    """Computes naive McCabe complexity by counting If, While, For, and ExceptHandler."""
    tree = ast.parse(code)
    branches = 1
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
            branches += 1
    return branches


def test_branch_smuggling_past_naive_ast_counter() -> None:
    """Demonstrates that boolean short-circuiting and comprehensions evade naive AST counters."""
    standard_branching = """
def process(x, y, z):
    if x:
        return 1
    elif y:
        return 2
    elif z:
        return 3
    return 0
"""
    smuggled_branching = """
def process(x, y, z):
    return 1 if x else (2 if y else (3 if z else 0))
"""
    comprehension_smuggling = """
def process(items):
    return [x for x in items if x > 0 and x % 2 == 0]
"""

    std_score = naive_ast_branch_counter(standard_branching)
    smuggled_score = naive_ast_branch_counter(smuggled_branching)
    comp_score = naive_ast_branch_counter(comprehension_smuggling)

    # Standard if/elif ladder registers 4 branches in naive counter
    # Smuggled ternary expression registers 1 branch (IfExp is not ast.If)
    # Comprehension registers 1 branch (comprehension if is not ast.If)
    assert (std_score, smuggled_score, comp_score) == (4, 1, 1)


# --- 3. Test Suite Overfitting & The Oracle Problem ---

def overfitted_agent_implementation(n: int) -> int:
    """Plausible but overfitted implementation passing hardcoded fixture tests."""
    fixtures = {0: 0, 1: 1, 2: 1, 3: 2, 4: 3, 5: 5}
    return fixtures.get(n, 0)


def correct_fibonacci_implementation(n: int) -> int:
    """General implementation computing the correct Fibonacci sequence."""
    if n <= 0:
        return 0
    if n == 1:
        return 1
    prev, curr = 0, 1
    for _ in range(2, n + 1):
        prev, curr = curr, prev + curr
    return curr


def test_overfitted_patch_passes_narrow_suite_but_fails_generalization() -> None:
    """Asserts that an overfitted patch satisfies a 5-element test suite but fails outside it."""
    # Narrow suite (inputs 0..5)
    narrow_inputs = [0, 1, 2, 3, 4, 5]
    overfitted_narrow = list(map(overfitted_agent_implementation, narrow_inputs))
    correct_narrow = list(map(correct_fibonacci_implementation, narrow_inputs))

    # Both implementations pass 100% of the narrow test suite!
    assert (overfitted_narrow, len(overfitted_narrow)) == (correct_narrow, 6)

    # Generalized inputs (inputs 6..8)
    extended_inputs = [6, 7, 8]
    overfitted_extended = list(map(overfitted_agent_implementation, extended_inputs))
    correct_extended = list(map(correct_fibonacci_implementation, extended_inputs))

    # The overfitted patch catastrophically fails outside the fixture horizon
    assert (overfitted_extended, correct_extended) == ([0, 0, 0], [8, 13, 21])


# --- 4. Micro-Function Fragmentation vs. Cohesive Logic ---

def cohesive_pipeline(data: list[int]) -> int:
    """Cohesive 8-line function with local context."""
    total = 0
    for val in data:
        if val > 0:
            total += val * 2
    return total


def _frag_validate(val: int) -> bool:
    return val > 0


def _frag_transform(val: int) -> int:
    return val * 2


def _frag_accumulate(total: int, term: int) -> int:
    return total + term


def fragmented_pipeline(data: list[int]) -> int:
    """Decomposed micro-functions passing strict M<=1 but increasing call overhead."""
    total = 0
    for val in data:
        if _frag_validate(val):
            total = _frag_accumulate(total, _frag_transform(val))
    return total


def test_cohesive_vs_fragmented_equivalence_and_structure() -> None:
    """Verifies that while both implementations compute identical results, fragmentation adds call depth."""
    sample_input = [1, -2, 3, 4, -5]

    cohesive_res = cohesive_pipeline(sample_input)
    fragmented_res = fragmented_pipeline(sample_input)

    assert (cohesive_res, fragmented_res) == (16, 16)
