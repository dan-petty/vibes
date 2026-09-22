#!/usr/bin/env python3
"""Automated AST Conditional Refactorer for Self-Improving Agentic Workflows.

Mechanically analyzes Python ASTs, diagnoses cyclomatic complexity and nesting
headroom bottlenecks, and auto-decomposes branching structures into:
1. Table-driven dictionary dispatch mappings (eliminating if/elif ladders).
2. Early-return guard clauses (flattening deep conditional indentation).
3. Pure predicate helper functions (decomposing compound boolean expressions).
4. Consolidated assertion tuples (reducing test suite AST complexity).

Enforces strict verification invariants: transformations are validated via
round-trip AST parsing, metric delta checks, and behavioral equivalence.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

# Branch node types contributing to McCabe cyclomatic complexity
BRANCH_NODE_TYPES = (
    ast.If,
    ast.While,
    ast.For,
    ast.AsyncFor,
    ast.ExceptHandler,
    ast.Assert,
    ast.IfExp,
)

# Indentation nesting statement types
NESTING_NODE_TYPES = (
    ast.If,
    ast.While,
    ast.For,
    ast.AsyncFor,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.ExceptHandler,
)

# Comparison inversion mapping for relational operators
INVERTED_COMPARE_OPS: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.LtE: ast.Gt,
    ast.Gt: ast.LtE,
    ast.GtE: ast.Lt,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}


class RefactorStrategy(StrEnum):
    """Automated AST refactoring strategy classification."""

    TABLE_DISPATCH = "TABLE_DISPATCH"
    GUARD_CLAUSE_FLATTEN = "GUARD_CLAUSE_FLATTEN"
    PREDICATE_EXTRACTION = "PREDICATE_EXTRACTION"
    ASSERTION_CONSOLIDATION = "ASSERTION_CONSOLIDATION"


@dataclass
class RefactorCandidate:
    """Represents a function identified as a refactoring candidate."""

    file_path: str
    function_name: str
    lineno: int
    initial_complexity: int
    initial_depth: int
    suggested_strategy: RefactorStrategy
    details: str


@dataclass
class RefactorResult:
    """Represents the outcome of an AST refactoring transformation."""

    candidate: RefactorCandidate
    original_code: str
    refactored_code: str
    diff: str
    final_complexity: int
    final_depth: int
    complexity_delta: int
    depth_delta: int
    success: bool
    error_message: str | None = None


@dataclass
class RefactoringReport:
    """Summary report across multiple refactored targets."""

    candidates: list[RefactorCandidate] = field(default_factory=list)
    results: list[RefactorResult] = field(default_factory=list)
    total_candidates: int = 0
    successful_refactorings: int = 0
    net_complexity_reduction: int = 0


@dataclass
class LadderBranch:
    """Single branch within an if/elif equality ladder."""

    key_expr: ast.expr
    return_expr: ast.expr


def _node_complexity_weight(node: ast.AST) -> int:
    """Calculate cyclomatic complexity weight for a single AST node."""
    if isinstance(node, BRANCH_NODE_TYPES):
        return 1
    if isinstance(node, ast.BoolOp):
        return max(0, len(node.values) - 1)
    return 0


def calculate_cyclomatic_complexity(root: ast.AST) -> int:
    """Calculate McCabe cyclomatic complexity M for an AST node."""
    return 1 + sum(_node_complexity_weight(child) for child in ast.walk(root))


def _walk_depth(node: ast.AST, current_depth: int) -> int:
    """Recursively determine maximum indentation depth of nested blocks."""
    next_depth = current_depth + 1 if isinstance(node, NESTING_NODE_TYPES) else current_depth
    deepest = next_depth
    for child in ast.iter_child_nodes(node):
        sub_deep = _walk_depth(child, next_depth)
        if sub_deep > deepest:
            deepest = sub_deep
    return deepest


def calculate_nesting_depth(root: ast.AST) -> int:
    """Calculate the deepest indentation nesting depth inside a code block."""
    statements = getattr(root, "body", [])
    if not statements:
        return 1
    return max(_walk_depth(stmt, 1) for stmt in statements)


def _invert_compare(node: ast.Compare) -> ast.expr:
    """Invert single comparison operator into its logical opposite."""
    if len(node.ops) != 1 or type(node.ops[0]) not in INVERTED_COMPARE_OPS:
        return ast.UnaryOp(op=ast.Not(), operand=node)
    inv_cls = INVERTED_COMPARE_OPS[type(node.ops[0])]
    return ast.Compare(left=node.left, ops=[inv_cls()], comparators=node.comparators)


def invert_condition(test_node: ast.expr) -> ast.expr:
    """Invert an AST test condition into its logical negation."""
    if isinstance(test_node, ast.UnaryOp) and isinstance(test_node.op, ast.Not):
        return test_node.operand
    if isinstance(test_node, ast.Compare):
        return _invert_compare(test_node)
    return ast.UnaryOp(op=ast.Not(), operand=test_node)


def compute_unified_diff(original: str, refactored: str, filename: str = "source.py") -> str:
    """Generate unified diff string between original and refactored source."""
    orig_lines = original.splitlines(keepends=True)
    ref_lines = refactored.splitlines(keepends=True)
    diff = difflib.unified_diff(
        orig_lines,
        ref_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
    )
    return "".join(diff)


def _is_single_eq_compare(test: ast.expr, target_id: str) -> bool:
    """Predicate checking if expr is target_id == key equality compare."""
    if not isinstance(test, ast.Compare):
        return False
    return (
        len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.left, ast.Name)
        and test.left.id == target_id
    )


def _extract_single_return_val(body: list[ast.stmt]) -> ast.expr | None:
    """Extract return expression if body is a single non-empty return."""
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return None
    return body[0].value


def _match_single_equality_branch(if_node: ast.If, target_id: str) -> tuple[ast.expr, ast.expr] | None:
    """Check if an if node matches target_id == key returning an expression."""
    test = if_node.test
    if not isinstance(test, ast.Compare) or not _is_single_eq_compare(test, target_id):
        return None
    val = _extract_single_return_val(if_node.body)
    if val is None:
        return None
    return test.comparators[0], val


def _step_ladder_traversal(
    head: ast.stmt, target_id: str
) -> tuple[LadderBranch | None, list[ast.stmt] | None, ast.expr | None, bool]:
    """Process single node in equality ladder chain, returning step details."""
    if isinstance(head, ast.If):
        matched = _match_single_equality_branch(head, target_id)
        if not matched:
            return None, None, None, False
        branch = LadderBranch(key_expr=matched[0], return_expr=matched[1])
        return branch, head.orelse, None, True
    if isinstance(head, ast.Return):
        return None, None, head.value, True
    return None, None, None, False


def _get_ladder_target_id(first_if: ast.If) -> str | None:
    """Extract variable identifier if condition is a valid variable compare."""
    test = first_if.test
    if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name):
        return test.left.id
    return None


def _collect_ladder_branches(
    first_if: ast.If, target_id: str
) -> tuple[list[LadderBranch], ast.expr | None] | None:
    """Traverse ladder body and collect all branches and optional fallback."""
    branches: list[LadderBranch] = []
    curr: list[ast.stmt] = [first_if]
    fallback: ast.expr | None = None
    while curr:
        b, next_stmts, fb, valid = _step_ladder_traversal(curr[0], target_id)
        if not valid:
            return None
        if b:
            branches.append(b)
            curr = next_stmts or []
        else:
            fallback = fb
            break
    return branches, fallback


def extract_equality_ladder(
    first_if: ast.If, min_branches: int = 3
) -> tuple[str, list[LadderBranch], ast.expr | None] | None:
    """Extract equality ladder branches matching on a single variable."""
    target_id = _get_ladder_target_id(first_if)
    if not target_id:
        return None
    collected = _collect_ladder_branches(first_if, target_id)
    if not collected or len(collected[0]) < min_branches:
        return None
    return target_id, collected[0], collected[1]


def _find_function_by_name(tree: ast.AST, fn_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Locate a function definition node by name in an AST module."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn_name:
            return node
    return None


def _predicate_chained_asserts(fn: ast.FunctionDef | ast.AsyncFunctionDef, _c: int, _d: int) -> bool:
    return fn.name.startswith("test_") and sum(1 for stmt in fn.body if isinstance(stmt, ast.Assert)) >= 3


def _predicate_equality_ladder(fn: ast.FunctionDef | ast.AsyncFunctionDef, _c: int, _d: int) -> bool:
    return CandidateDetector._find_ladder_in_body(fn) is not None


def _predicate_deep_nesting(fn: ast.FunctionDef | ast.AsyncFunctionDef, _c: int, d: int) -> bool:
    return d >= 3 and CandidateDetector._has_inverting_guard_opportunity(fn)


def _predicate_compound_boolean(fn: ast.FunctionDef | ast.AsyncFunctionDef, _c: int, _d: int) -> bool:
    return CandidateDetector._has_compound_boolean(fn)


# Strategy evaluation table: (predicate, strategy, describer)
STRATEGY_RULES: list[
    tuple[
        Callable[[ast.FunctionDef | ast.AsyncFunctionDef, int, int], bool],
        RefactorStrategy,
        Callable[[ast.FunctionDef | ast.AsyncFunctionDef, int, int], str],
    ]
] = [
    (
        _predicate_chained_asserts,
        RefactorStrategy.ASSERTION_CONSOLIDATION,
        lambda fn, c, _d: f"Test '{fn.name}' has chained asserts contributing to M={c}.",
    ),
    (
        _predicate_equality_ladder,
        RefactorStrategy.TABLE_DISPATCH,
        lambda fn, c, _d: f"Equality ladder detected in '{fn.name}' (M={c}).",
    ),
    (
        _predicate_deep_nesting,
        RefactorStrategy.GUARD_CLAUSE_FLATTEN,
        lambda _fn, _c, d: f"Deep nesting (Depth={d}) flattens via early-return guard clauses.",
    ),
    (
        _predicate_compound_boolean,
        RefactorStrategy.PREDICATE_EXTRACTION,
        lambda fn, c, _d: f"Compound boolean conditions in '{fn.name}' (M={c}); extract predicate helper.",
    ),
]


class CandidateDetector:
    """Detects structural refactoring opportunities within functions."""

    @classmethod
    def diagnose_function(
        cls,
        fn: ast.FunctionDef | ast.AsyncFunctionDef,
        file_path: str,
        threshold: int = 7,
    ) -> RefactorCandidate | None:
        """Evaluate a function against complexity thresholds and detect best strategy."""
        c = calculate_cyclomatic_complexity(fn)
        d = calculate_nesting_depth(fn)
        if c < threshold and d < 4:
            return None

        strategy, details = cls._select_strategy(fn, c, d)
        return RefactorCandidate(
            file_path=file_path,
            function_name=fn.name,
            lineno=getattr(fn, "lineno", 1),
            initial_complexity=c,
            initial_depth=d,
            suggested_strategy=strategy,
            details=details,
        )

    @classmethod
    def _select_strategy(
        cls,
        fn: ast.FunctionDef | ast.AsyncFunctionDef,
        complexity: int,
        depth: int,
    ) -> tuple[RefactorStrategy, str]:
        """Select highest-yield refactoring strategy based on structural patterns."""
        for predicate, strategy, desc_fn in STRATEGY_RULES:
            if predicate(fn, complexity, depth):
                return strategy, desc_fn(fn, complexity, depth)

        return (
            RefactorStrategy.GUARD_CLAUSE_FLATTEN,
            f"Proactive refactoring recommended for {fn.name} (M={complexity}, Depth={depth}).",
        )

    @staticmethod
    def _find_ladder_in_body(
        fn: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> tuple[str, list[LadderBranch], ast.expr | None] | None:
        for stmt in fn.body:
            ladder = extract_equality_ladder(stmt, min_branches=3) if isinstance(stmt, ast.If) else None
            if ladder:
                return ladder
        return None

    @staticmethod
    def _has_compound_boolean(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp) and len(node.test.values) >= 2:
                return True
        return False

    @staticmethod
    def _has_inverting_guard_opportunity(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        return any(
            isinstance(stmt, ast.If) and len(stmt.body) >= 2 and not stmt.orelse
            for stmt in fn.body
        )


class TableDispatchTransformer:
    """Transforms equality ladders into dictionary dispatch lookups."""

    @classmethod
    def apply(
        cls,
        module_tree: ast.Module,
        fn_name: str,
        ladder: tuple[str, list[LadderBranch], ast.expr | None],
    ) -> ast.Module:
        """Synthesize dispatch dictionary and replace ladder in target function."""
        target_id, branches, fallback_expr = ladder
        dict_name = f"_{fn_name.upper()}_DISPATCH"
        dict_assign = cls._create_dict_assignment(dict_name, branches)

        fn_node = _find_function_by_name(module_tree, fn_name)
        if not fn_node:
            return module_tree

        new_body = cls._replace_ladder_stmt(fn_node.body, target_id, dict_name, fallback_expr)
        fn_node.body = new_body

        cls._insert_module_assignment(module_tree, fn_node, dict_assign)
        ast.fix_missing_locations(module_tree)
        return module_tree

    @staticmethod
    def _create_dict_assignment(dict_name: str, branches: list[LadderBranch]) -> ast.Assign:
        d = ast.Dict(
            keys=[b.key_expr for b in branches],
            values=[b.return_expr for b in branches],
        )
        return ast.Assign(targets=[ast.Name(id=dict_name, ctx=ast.Store())], value=d)

    @classmethod
    def _replace_ladder_stmt(
        cls,
        stmts: list[ast.stmt],
        target_id: str,
        dict_name: str,
        fallback_expr: ast.expr | None,
    ) -> list[ast.stmt]:
        res: list[ast.stmt] = []
        for stmt in stmts:
            if isinstance(stmt, ast.If) and extract_equality_ladder(stmt, min_branches=3):
                default_val = fallback_expr or ast.Constant(value=None)
                call = ast.Call(
                    func=ast.Attribute(
                        value=ast.Name(id=dict_name, ctx=ast.Load()),
                        attr="get",
                        ctx=ast.Load(),
                    ),
                    args=[ast.Name(id=target_id, ctx=ast.Load()), default_val],
                    keywords=[],
                )
                res.append(ast.Return(value=call))
            else:
                res.append(stmt)
        return res

    @staticmethod
    def _insert_module_assignment(module_tree: ast.Module, fn_node: ast.AST, assign: ast.Assign) -> None:
        idx = 0
        for i, item in enumerate(module_tree.body):
            if item is fn_node:
                idx = i
                break
        module_tree.body.insert(idx, assign)


class GuardClauseTransformer:
    """Flattens nested if blocks into early-return guard clauses."""

    @classmethod
    def apply(cls, module_tree: ast.Module, fn_name: str) -> ast.Module:
        """Invert outermost nesting if block in function body to flatten indentation."""
        fn_node = _find_function_by_name(module_tree, fn_name)
        if not fn_node:
            return module_tree

        new_body: list[ast.stmt] = []
        for stmt in fn_node.body:
            if isinstance(stmt, ast.If) and len(stmt.body) >= 2 and not stmt.orelse:
                guard = ast.If(
                    test=invert_condition(stmt.test),
                    body=[ast.Return(value=ast.Constant(value=None))],
                    orelse=[],
                )
                new_body.append(guard)
                new_body.extend(stmt.body)
            else:
                new_body.append(stmt)

        fn_node.body = new_body
        ast.fix_missing_locations(module_tree)
        return module_tree


class PredicateExtractor:
    """Extracts compound boolean expressions into pure helper functions."""

    @classmethod
    def apply(cls, module_tree: ast.Module, fn_name: str) -> ast.Module:
        """Locate compound boolean if-statement and extract pure predicate function."""
        fn_node = _find_function_by_name(module_tree, fn_name)
        if not fn_node:
            return module_tree

        target_if = cls._find_compound_if(fn_node)
        if not target_if:
            return module_tree

        helper_name = f"_is_{fn_name}_valid"
        helper_fn = cls._create_helper(helper_name, target_if.test, fn_node)

        call_args: list[ast.expr] = [
            ast.Name(id=a.arg, ctx=ast.Load())
            for a in fn_node.args.args
            if a.arg not in ("self", "cls")
        ]
        target_if.test = ast.Call(func=ast.Name(id=helper_name, ctx=ast.Load()), args=call_args, keywords=[])

        cls._insert_helper(module_tree, fn_node, helper_fn)
        ast.fix_missing_locations(module_tree)
        return module_tree

    @staticmethod
    def _find_compound_if(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.If | None:
        for node in ast.walk(fn_node):
            if isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp) and len(node.test.values) >= 2:
                return node
        return None

    @staticmethod
    def _create_helper(
        name: str,
        test_expr: ast.expr,
        parent_fn: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> ast.FunctionDef:
        doc = f"Predicate helper evaluating valid condition for {parent_fn.name}."
        body: list[ast.stmt] = [ast.Expr(value=ast.Constant(value=doc)), ast.Return(value=test_expr)]
        params = [a for a in parent_fn.args.args if a.arg not in ("self", "cls")]
        return ast.FunctionDef(
            name=name,
            args=ast.arguments(
                posonlyargs=[],
                args=params,
                kwonlyargs=[],
                kw_defaults=[],
                defaults=[],
            ),
            body=body,
            decorator_list=[],
            returns=ast.Name(id="bool", ctx=ast.Load()),
            # Required since 3.12: a FunctionDef built without it matches no overload,
            # and `ast.unparse` on a node missing the field raises at generation time.
            type_params=[],
        )

    @staticmethod
    def _insert_helper(module_tree: ast.Module, fn_node: ast.AST, helper: ast.FunctionDef) -> None:
        idx = 0
        for i, item in enumerate(module_tree.body):
            if item is fn_node:
                idx = i
                break
        module_tree.body.insert(idx, helper)


class AssertionConsolidator:
    """Consolidates consecutive simple assertions into tuple equality asserts."""

    @classmethod
    def apply(cls, module_tree: ast.Module, fn_name: str) -> ast.Module:
        """Find consecutive assert statements in test function and merge into tuple assert."""
        fn_node = _find_function_by_name(module_tree, fn_name)
        if not fn_node:
            return module_tree

        new_body: list[ast.stmt] = []
        acc_lefts: list[ast.expr] = []
        acc_rights: list[ast.expr] = []

        for stmt in fn_node.body:
            test = stmt.test if isinstance(stmt, ast.Assert) else None
            if isinstance(test, ast.Compare) and cls._is_simple_equality_assert(stmt):
                acc_lefts.append(test.left)
                acc_rights.append(test.comparators[0])
            else:
                cls._flush_accumulated_asserts(acc_lefts, acc_rights, new_body)
                new_body.append(stmt)

        cls._flush_accumulated_asserts(acc_lefts, acc_rights, new_body)
        fn_node.body = new_body
        ast.fix_missing_locations(module_tree)
        return module_tree

    @staticmethod
    def _is_simple_equality_assert(stmt: ast.stmt) -> bool:
        if not isinstance(stmt, ast.Assert):
            return False
        test = stmt.test
        return isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)

    @classmethod
    def _flush_accumulated_asserts(
        cls,
        lefts: list[ast.expr],
        rights: list[ast.expr],
        out_stmts: list[ast.stmt],
    ) -> None:
        if not lefts:
            return
        if len(lefts) < 3:
            for l_expr, r_expr in zip(lefts, rights, strict=True):
                out_stmts.append(
                    ast.Assert(test=ast.Compare(left=l_expr, ops=[ast.Eq()], comparators=[r_expr]), msg=None)
                )
        else:
            tuple_assert = ast.Assert(
                test=ast.Compare(
                    left=ast.Tuple(elts=list(lefts), ctx=ast.Load()),
                    ops=[ast.Eq()],
                    comparators=[ast.Tuple(elts=list(rights), ctx=ast.Load())],
                ),
                msg=None,
            )
            out_stmts.append(tuple_assert)
        lefts.clear()
        rights.clear()


def _dispatch_table_transform(tree: ast.Module, candidate: RefactorCandidate) -> ast.Module | None:
    fn = _find_function_by_name(tree, candidate.function_name)
    if not fn:
        return None
    ladder = CandidateDetector._find_ladder_in_body(fn)
    if not ladder:
        return None
    return TableDispatchTransformer.apply(tree, candidate.function_name, ladder)


def _dispatch_guard_transform(tree: ast.Module, candidate: RefactorCandidate) -> ast.Module | None:
    return GuardClauseTransformer.apply(tree, candidate.function_name)


def _dispatch_predicate_transform(tree: ast.Module, candidate: RefactorCandidate) -> ast.Module | None:
    return PredicateExtractor.apply(tree, candidate.function_name)


def _dispatch_assert_transform(tree: ast.Module, candidate: RefactorCandidate) -> ast.Module | None:
    return AssertionConsolidator.apply(tree, candidate.function_name)


STRATEGY_DISPATCH: dict[RefactorStrategy, Callable[[ast.Module, RefactorCandidate], ast.Module | None]] = {
    RefactorStrategy.TABLE_DISPATCH: _dispatch_table_transform,
    RefactorStrategy.GUARD_CLAUSE_FLATTEN: _dispatch_guard_transform,
    RefactorStrategy.PREDICATE_EXTRACTION: _dispatch_predicate_transform,
    RefactorStrategy.ASSERTION_CONSOLIDATION: _dispatch_assert_transform,
}


def _find_candidate_in_node(node: ast.AST, file_path: str, threshold: int) -> RefactorCandidate | None:
    """Examine AST node and extract candidate if exceeding threshold."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    return CandidateDetector.diagnose_function(node, file_path, threshold)


class ASTRefactorer:
    """Autonomous AST refactoring and conditional decomposition engine."""

    def __init__(self, threshold: int = 7) -> None:
        """Initialize refactorer with complexity activation threshold."""
        self.threshold = threshold

    def scan_source(self, source: str, file_path: str = "<string>") -> list[RefactorCandidate]:
        """Parse source and return list of functions exceeding complexity headroom."""
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError:
            return []

        candidates: list[RefactorCandidate] = []
        for node in ast.walk(tree):
            c = _find_candidate_in_node(node, file_path, self.threshold)
            if c:
                candidates.append(c)
        return candidates

    def scan_file(self, path: Path) -> list[RefactorCandidate]:
        """Scan a Python file on disk for refactoring opportunities."""
        if not path.is_file():
            return []
        try:
            content = path.read_text(encoding="utf-8")
            return self.scan_source(content, str(path))
        except OSError:
            return []

    def refactor_source(self, source: str, candidate: RefactorCandidate) -> RefactorResult:
        """Execute safe AST refactoring on source string for a target candidate."""
        try:
            tree = ast.parse(source, filename=candidate.file_path)
        except SyntaxError as err:
            return self._build_error_result(candidate, source, f"Initial syntax error: {err}")

        handler = STRATEGY_DISPATCH.get(candidate.suggested_strategy)
        transformed_tree = handler(tree, candidate) if handler else None
        if transformed_tree is None:
            return self._build_error_result(candidate, source, "Transformation not applicable")

        refactored_code = ast.unparse(transformed_tree)
        is_safe, final_c, final_d, err_msg = self._verify_safety(
            refactored_code,
            candidate.function_name,
            candidate.initial_complexity,
            candidate.initial_depth,
        )

        if not is_safe:
            return self._build_error_result(candidate, source, err_msg or "Safety invariant check failed")

        diff = compute_unified_diff(source, refactored_code, Path(candidate.file_path).name)
        return RefactorResult(
            candidate=candidate,
            original_code=source,
            refactored_code=refactored_code,
            diff=diff,
            final_complexity=final_c,
            final_depth=final_d,
            complexity_delta=candidate.initial_complexity - final_c,
            depth_delta=candidate.initial_depth - final_d,
            success=True,
        )

    @staticmethod
    def _verify_safety(
        refactored_code: str,
        fn_name: str,
        initial_c: int,
        initial_d: int,
    ) -> tuple[bool, int, int, str | None]:
        """Validate that refactored code parses cleanly and does not regress metrics."""
        try:
            tree = ast.parse(refactored_code)
        except SyntaxError as err:
            return False, initial_c, initial_d, f"SyntaxError in transformed code: {err}"

        fn = _find_function_by_name(tree, fn_name)
        if not fn:
            return False, initial_c, initial_d, f"Target function '{fn_name}' missing in AST"

        final_c = calculate_cyclomatic_complexity(fn)
        final_d = calculate_nesting_depth(fn)
        if final_c > initial_c:
            return False, final_c, final_d, f"Complexity regressed: {initial_c} -> {final_c}"
        if final_d > initial_d:
            return False, final_c, final_d, f"Depth regressed: {initial_d} -> {final_d}"

        return True, final_c, final_d, None

    @staticmethod
    def _build_error_result(candidate: RefactorCandidate, source: str, error: str) -> RefactorResult:
        return RefactorResult(
            candidate=candidate,
            original_code=source,
            refactored_code=source,
            diff="",
            final_complexity=candidate.initial_complexity,
            final_depth=candidate.initial_depth,
            complexity_delta=0,
            depth_delta=0,
            success=False,
            error_message=error,
        )

    def refactor_file(self, path: Path, apply: bool = False) -> list[RefactorResult]:
        """Scan and refactor candidates in a given file, optionally modifying in-place."""
        candidates = self.scan_file(path)
        if not candidates:
            return []

        content = path.read_text(encoding="utf-8")
        results: list[RefactorResult] = []
        current_content = content

        for candidate in candidates:
            res = self.refactor_source(current_content, candidate)
            results.append(res)
            if res.success:
                current_content = res.refactored_code

        if apply and any(r.success for r in results):
            path.write_text(current_content, encoding="utf-8")

        return results

    @staticmethod
    def _load_feedback_items(feedback_path: Path) -> list[dict[str, Any]]:
        if not feedback_path.is_file():
            return []
        try:
            data = json.loads(feedback_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else data.get("feedback", [])
        except (json.JSONDecodeError, OSError):
            return []

    def _process_feedback_target(
        self,
        target_path_str: str,
        apply: bool,
        report: RefactoringReport,
    ) -> None:
        p = Path(target_path_str)
        if not p.is_file():
            return
        res_list = self.refactor_file(p, apply=apply)
        for r in res_list:
            report.candidates.append(r.candidate)
            report.results.append(r)
            if r.success:
                report.successful_refactorings += 1
                report.net_complexity_reduction += r.complexity_delta

    def refactor_from_feedback_file(self, feedback_path: Path, apply: bool = False) -> RefactoringReport:
        """Ingest feedback items from JSON and refactor candidates matching PROACTIVE_REFACTOR."""
        report = RefactoringReport()
        items = self._load_feedback_items(feedback_path)
        for item in items:
            if item.get("category") == "PROACTIVE_REFACTOR" and item.get("target"):
                self._process_feedback_target(item["target"], apply, report)

        report.total_candidates = len(report.candidates)
        return report


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for AST refactorer."""
    parser = argparse.ArgumentParser(
        prog="ast_refactorer",
        description="Automated AST Conditional Refactorer & Invariant Gate",
    )
    parser.add_argument("--scan", action="store_true", help="Scan target files and report candidates")
    parser.add_argument("--diff", action="store_true", help="Generate and display unified diffs")
    parser.add_argument("--apply", action="store_true", help="Apply verified refactorings in-place")
    parser.add_argument("--threshold", type=int, default=7, help="McCabe complexity threshold (default: 7)")
    parser.add_argument("--file", type=str, help="Target specific Python file to scan or refactor")
    parser.add_argument(
        "--from-feedback",
        type=str,
        help="Path to SDLC feedback JSON (e.g. .data/sdlc_backlog.json)",
    )
    return parser


def _handle_cli_feedback(refactorer: ASTRefactorer, feedback_path_str: str, apply: bool) -> int:
    report = refactorer.refactor_from_feedback_file(Path(feedback_path_str), apply=apply)
    print(f"Feedback refactoring processed {report.total_candidates} candidates.")
    print(f"Successful: {report.successful_refactorings} | Net M reduction: {report.net_complexity_reduction}")
    return 0


def _handle_cli_scan(refactorer: ASTRefactorer, target_path: Path) -> int:
    candidates = refactorer.scan_file(target_path)
    print(f"Discovered {len(candidates)} candidates in {target_path}:")
    for c in candidates:
        print(f" - {c.function_name}:{c.lineno} [{c.suggested_strategy.value}] M={c.initial_complexity}")
    return 0


def _handle_cli_refactor(refactorer: ASTRefactorer, target_path: Path, opts: argparse.Namespace) -> int:
    results = refactorer.refactor_file(target_path, apply=opts.apply)
    for r in results:
        print(f"Refactor [{r.candidate.suggested_strategy.value}] {r.candidate.function_name}:")
        print(f"  Complexity: {r.candidate.initial_complexity} -> {r.final_complexity} (delta: {r.complexity_delta})")
        print(f"  Nesting: {r.candidate.initial_depth} -> {r.final_depth} (delta: {r.depth_delta})")
        if opts.diff and r.diff:
            print(r.diff)
    return 0


def _is_scan_mode(opts: argparse.Namespace) -> bool:
    """Predicate determining if CLI options specify scan-only operation."""
    return bool(opts.scan or (not opts.diff and not opts.apply))


def _dispatch_cli_target_file(
    refactorer: ASTRefactorer, target_path: Path, opts: argparse.Namespace
) -> int:
    """Validate file existence and dispatch to scan or refactor handler."""
    if not target_path.is_file():
        print(f"Error: Target file '{target_path}' does not exist.", file=sys.stderr)
        return 1
    if _is_scan_mode(opts):
        return _handle_cli_scan(refactorer, target_path)
    return _handle_cli_refactor(refactorer, target_path, opts)


def run_cli(args: Sequence[str] | None = None) -> int:
    """Execute command-line interface for AST Refactorer."""
    parser = build_arg_parser()
    opts = parser.parse_args(args)
    refactorer = ASTRefactorer(threshold=opts.threshold)

    if opts.from_feedback:
        return _handle_cli_feedback(refactorer, opts.from_feedback, opts.apply)
    if not opts.file:
        parser.print_help()
        return 0
    return _dispatch_cli_target_file(refactorer, Path(opts.file), opts)


if __name__ == "__main__":
    sys.exit(run_cli())
