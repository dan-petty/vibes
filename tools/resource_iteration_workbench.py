#!/usr/bin/env python3
"""Resource Iteration Workbench for AI Agents.

Orchestrates the continuous Scan -> Run -> Review -> Iterate development loop:
1. SCAN: Discovers repository resources (modules, tests, apps, benchmarks).
   Measures AST complexity (M <= 10), nesting depth (<= 5), and sanitization.
2. RUN: Executes target commands or test suites in bounded subprocesses,
   capturing duration, exit codes, and output telemetry.
3. REVIEW: Evaluates outputs, extracts pass/fail metrics, detects regressions,
   and computes deltas against previous baseline runs.
4. ITERATE: Generates actionable, prescriptive remediation guidance for agents.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass, field
from enum import Enum
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Sequence

# Canonical quality thresholds
MAX_ALLOWED_COMPLEXITY = 10
MAX_ALLOWED_NESTING = 5
CANONICAL_MOCK_DOMAIN = "example.com"

# Allowed documentation networks for zero-trust compliance
ALLOWED_TEST_NETWORKS = [
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("127.0.0.0/8"),
]

IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PYTEST_PASSED_RE = re.compile(r"(\d+)\s+passed")
PYTEST_FAILED_RE = re.compile(r"(\d+)\s+failed")
PYTEST_WARNINGS_RE = re.compile(r"(\d+)\s+warnings?")


class ResourceType(str, Enum):
    """Classification of repository resources."""
    PYTHON_MODULE = "python_module"
    TEST_SUITE = "test_suite"
    SAMPLE_APP = "sample_app"
    BENCHMARK = "benchmark"
    MANIFEST = "manifest"


class HealthStatus(str, Enum):
    """Overall health status of an evaluated resource."""
    HEALTHY = "HEALTHY"
    NEEDS_REMEDIATION = "NEEDS_REMEDIATION"
    CRITICAL = "CRITICAL"


@dataclass
class ResourceScanMetrics:
    """Static AST and code metrics extracted during the Scan phase."""
    file_path: str
    resource_type: ResourceType
    loc: int = 0
    functions_count: int = 0
    classes_count: int = 0
    max_complexity: int = 1
    max_depth: int = 1
    complexity_violations: list[str] = field(default_factory=list)
    sanitization_violations: list[str] = field(default_factory=list)
    near_threshold_functions: list[str] = field(default_factory=list)
    functions_without_docstrings: list[str] = field(default_factory=list)
    functions_without_type_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize scan metrics to JSON-compatible dictionary."""
        data = asdict(self)
        data["resource_type"] = self.resource_type.value
        return data


@dataclass
class RunExecutionResult:
    """Execution telemetry captured during the Run phase."""
    target: str
    command: list[str]
    exit_code: int
    duration_seconds: float
    stdout: str
    stderr: str
    passed_count: int = 0
    failed_count: int = 0
    warnings_count: int = 0
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize run execution result to JSON-compatible dictionary."""
        return asdict(self)


@dataclass
class ReviewEvaluation:
    """Consolidated assessment produced during the Review phase."""
    resource_path: str
    health_status: HealthStatus
    quality_score: float
    scan_metrics: ResourceScanMetrics
    run_result: RunExecutionResult | None = None
    actionable_recommendations: list[str] = field(default_factory=list)
    metric_deltas: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize review evaluation assessment to JSON-compatible dictionary."""
        return {
            "resource_path": self.resource_path,
            "health_status": self.health_status.value,
            "quality_score": self.quality_score,
            "scan_metrics": self.scan_metrics.to_dict(),
            "run_result": self.run_result.to_dict() if self.run_result else None,
            "actionable_recommendations": self.actionable_recommendations,
            "metric_deltas": self.metric_deltas,
        }


class FeedbackCategory(str, Enum):
    """Categorization of actionable feedback for recursive improvement."""
    PROACTIVE_REFACTOR = "PROACTIVE_REFACTOR"
    TEST_PARITY = "TEST_PARITY"
    DOCUMENTATION = "DOCUMENTATION"
    TYPE_SAFETY = "TYPE_SAFETY"
    PERFORMANCE = "PERFORMANCE"
    POSITIVE_REINFORCEMENT = "POSITIVE_REINFORCEMENT"


class FeedbackPriority(str, Enum):
    """Priority ranking of improvement feedback."""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


@dataclass
class ImprovementFeedback:
    """Meaningful feedback item driving the positive recursive improvement loop."""
    category: FeedbackCategory
    priority: FeedbackPriority
    target: str
    headline: str
    prescriptive_guidance: str
    suggested_action: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize improvement feedback item to JSON-compatible dictionary."""
        return {
            "category": self.category.value,
            "priority": self.priority.value,
            "target": self.target,
            "headline": self.headline,
            "prescriptive_guidance": self.prescriptive_guidance,
            "suggested_action": self.suggested_action,
        }


@dataclass
class IterationReport:
    """Top-level iteration report encompassing all evaluated resources."""
    timestamp: str
    overall_health: HealthStatus
    overall_score: float
    resources_evaluated: list[ReviewEvaluation]
    total_passed_tests: int = 0
    total_failed_tests: int = 0
    total_complexity_violations: int = 0
    prescriptive_action: str = ""
    improvement_feedback: list[ImprovementFeedback] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize overall iteration report to JSON-compatible dictionary."""
        return {
            "timestamp": self.timestamp,
            "overall_health": self.overall_health.value,
            "overall_score": self.overall_score,
            "total_passed_tests": self.total_passed_tests,
            "total_failed_tests": self.total_failed_tests,
            "total_complexity_violations": self.total_complexity_violations,
            "prescriptive_action": self.prescriptive_action,
            "resources_evaluated": [r.to_dict() for r in self.resources_evaluated],
            "improvement_feedback": [f.to_dict() for f in self.improvement_feedback],
        }


BRANCH_NODE_TYPES = (
    ast.If,
    ast.While,
    ast.For,
    ast.AsyncFor,
    ast.ExceptHandler,
    ast.Assert,
    ast.IfExp,
)


def _node_complexity_weight(node: ast.AST) -> int:
    """Calculate cyclomatic complexity branch contribution for an AST node."""
    if isinstance(node, BRANCH_NODE_TYPES):
        return 1
    if isinstance(node, ast.BoolOp):
        return len(node.values) - 1
    return 0


class ASTMetricCalculator:
    """Calculates McCabe cyclomatic complexity, nesting depth, and sanitization."""

    @classmethod
    def calculate_complexity(cls, node: ast.AST) -> int:
        """Calculate McCabe complexity M = E - N + 2P for a function AST node."""
        return 1 + sum(_node_complexity_weight(child) for child in ast.walk(node))

    @classmethod
    def calculate_max_nesting(cls, root: ast.AST) -> int:
        """Calculate the deepest indentation nesting depth inside a function."""
        nesting_types = (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler)

        def _walk_depth(node: ast.AST, depth: int) -> int:
            cur_depth = depth + 1 if isinstance(node, nesting_types) else depth
            deepest = cur_depth
            for child in ast.iter_child_nodes(node):
                sub_deep = _walk_depth(child, cur_depth)
                if sub_deep > deepest:
                    deepest = sub_deep
            return deepest

        max_seen = 0
        for stmt in getattr(root, "body", []):
            d = _walk_depth(stmt, 1)
            if d > max_seen:
                max_seen = d
        return max_seen

    @classmethod
    def check_sanitization(cls, tree: ast.AST) -> list[str]:
        """Detect hardcoded RFC 1918 private IPs and invalid mock subdomains."""
        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value
                cls._check_ip_string(val, getattr(node, "lineno", 0), violations)
                cls._check_subdomain_string(val, getattr(node, "lineno", 0), violations)
        return violations

    @staticmethod
    def _is_private_leak(ip_str: str) -> bool:
        """Check if an IPv4 address string is an unapproved RFC 1918 leak."""
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            return ip_obj.is_private and not any(ip_obj in net for net in ALLOWED_TEST_NETWORKS)
        except ValueError:
            return False

    @classmethod
    def _check_ip_string(cls, text: str, lineno: int, violations: list[str]) -> None:
        for match in IPV4_PATTERN.findall(text):
            if cls._is_private_leak(match):
                violations.append(f"Line {lineno}: Hardcoded RFC 1918 IP '{match}'. Use RFC 5737 or loopback.")

    @classmethod
    def _check_subdomain_string(cls, text: str, lineno: int, violations: list[str]) -> None:
        if "://" not in text or CANONICAL_MOCK_DOMAIN not in text:
            return
        match = re.search(r"https?://([^/:]+)", text)
        if not match:
            return
        host = match.group(1)
        if host != CANONICAL_MOCK_DOMAIN and host.endswith(f".{CANONICAL_MOCK_DOMAIN}"):
            violations.append(f"Line {lineno}: Subdomain '{host}' detected. Use standard '{CANONICAL_MOCK_DOMAIN}'.")


@dataclass
class _FunctionAggregate:
    """Helper accumulator for function-level AST metrics."""
    max_complexity: int = 1
    max_depth: int = 1
    complexity_violations: list[str] = field(default_factory=list)
    near_threshold_functions: list[str] = field(default_factory=list)
    functions_without_docstrings: list[str] = field(default_factory=list)
    functions_without_type_hints: list[str] = field(default_factory=list)

    def update(
        self,
        c: int,
        d: int,
        c_viol: str | None,
        d_viol: str | None,
        near_thr: str | None,
        miss_doc: str | None,
        miss_type: str | None,
    ) -> None:
        """Accumulate single function AST metrics and bounds into aggregate."""
        self.max_complexity = max(self.max_complexity, c)
        self.max_depth = max(self.max_depth, d)
        if c_viol:
            self.complexity_violations.append(c_viol)
        if d_viol:
            self.complexity_violations.append(d_viol)
        if near_thr:
            self.near_threshold_functions.append(near_thr)
        if miss_doc:
            self.functions_without_docstrings.append(miss_doc)
        if miss_type:
            self.functions_without_type_hints.append(miss_type)


class ResourceScanner:
    """Scans repository files and computes structured baseline metrics."""

    @classmethod
    def scan_python_file(cls, path: Path) -> ResourceScanMetrics:
        """Parse and extract AST metrics, contracts, and sanitization from a Python file."""
        try:
            content = path.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError) as err:
            return ResourceScanMetrics(
                file_path=str(path),
                resource_type=cls._classify_resource_type(path),
                complexity_violations=[f"Parse error: {str(err)[:256]}"],
            )

        loc = len([ln for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")])
        func_nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        class_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]

        agg = _FunctionAggregate()
        for fn in func_nodes:
            agg.update(*cls._inspect_function(fn))

        # Test suites often contain intentional mock IP strings to test sanitizers
        is_test = cls._classify_resource_type(path) == ResourceType.TEST_SUITE
        sanitization_violations = [] if is_test else ASTMetricCalculator.check_sanitization(tree)

        return ResourceScanMetrics(
            file_path=str(path),
            resource_type=cls._classify_resource_type(path),
            loc=loc,
            functions_count=len(func_nodes),
            classes_count=len(class_nodes),
            max_complexity=agg.max_complexity,
            max_depth=agg.max_depth,
            complexity_violations=agg.complexity_violations,
            sanitization_violations=sanitization_violations,
            near_threshold_functions=agg.near_threshold_functions,
            functions_without_docstrings=agg.functions_without_docstrings,
            functions_without_type_hints=agg.functions_without_type_hints,
        )

    @classmethod
    def _inspect_function(
        cls, fn: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> tuple[int, int, str | None, str | None, str | None, str | None, str | None]:
        c = ASTMetricCalculator.calculate_complexity(fn)
        d = ASTMetricCalculator.calculate_max_nesting(fn)
        c_viol, d_viol, near_thr = cls._check_complexity_bounds(fn.name, fn.lineno, c, d)
        miss_doc, miss_type = cls._check_function_contract(fn)
        return c, d, c_viol, d_viol, near_thr, miss_doc, miss_type

    @staticmethod
    def _check_complexity_bounds(
        name: str, lineno: int, c: int, d: int
    ) -> tuple[str | None, str | None, str | None]:
        c_viol = f"{name}:{lineno} M={c} > {MAX_ALLOWED_COMPLEXITY}" if c > MAX_ALLOWED_COMPLEXITY else None
        d_viol = f"{name}:{lineno} Depth={d} > {MAX_ALLOWED_NESTING}" if d > MAX_ALLOWED_NESTING else None
        is_near = (7 <= c <= MAX_ALLOWED_COMPLEXITY) or (4 <= d <= MAX_ALLOWED_NESTING)
        near_thr = f"{name} (M={c}, Depth={d})" if is_near else None
        return c_viol, d_viol, near_thr

    @staticmethod
    def _check_function_contract(
        fn: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> tuple[str | None, str | None]:
        if fn.name.startswith("_"):
            return None, None
        miss_doc = fn.name if not ast.get_docstring(fn) else None
        missing_annotation = fn.returns is None or any(
            a.annotation is None for a in fn.args.args if a.arg not in ("self", "cls")
        )
        miss_type = fn.name if missing_annotation else None
        return miss_doc, miss_type

    @classmethod
    def scan_directory(cls, root_dir: Path) -> list[ResourceScanMetrics]:
        """Recursively scan directory for Python files and compute scan metrics."""
        results: list[ResourceScanMetrics] = []
        for py_path in sorted(root_dir.rglob("*.py")):
            if ".venv" in py_path.parts or "__pycache__" in py_path.parts:
                continue
            results.append(cls.scan_python_file(py_path))
        return results

    @staticmethod
    def _classify_resource_type(path: Path) -> ResourceType:
        name = path.name
        parts = path.parts
        if name.startswith("test_") or name.endswith("_test.py"):
            return ResourceType.TEST_SUITE
        if "benchmarks" in parts:
            return ResourceType.BENCHMARK
        if "examples" in parts:
            return ResourceType.SAMPLE_APP
        return ResourceType.PYTHON_MODULE


class ResourceRunner:
    """Executes target verification commands in bounded subprocesses."""

    @classmethod
    def run_command(cls, command: list[str], target: str, cwd: Path, timeout: int = 30) -> RunExecutionResult:
        """Execute subprocess command with bounded timeout and capture execution telemetry."""
        start_time = time.monotonic()
        try:
            proc = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False)
            stdout = proc.stdout
            stderr = proc.stderr
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            stdout = ""
            stderr = f"Command timed out after {timeout} seconds."
            exit_code = 124
        except OSError as err:
            stdout = ""
            stderr = f"Execution failed: {str(err)[:256]}"
            exit_code = 1

        duration = round(time.monotonic() - start_time, 3)
        passed, failed, warnings = cls._parse_pytest_counts(stdout)

        return RunExecutionResult(
            target=target,
            command=command,
            exit_code=exit_code,
            duration_seconds=duration,
            stdout=stdout,
            stderr=stderr,
            passed_count=passed,
            failed_count=failed,
            warnings_count=warnings,
        )

    @classmethod
    def run_tests_for_resource(cls, resource_path: Path, cwd: Path) -> RunExecutionResult:
        """Execute pytest test suite with optimized plugins and isolated cache."""
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cov",
            "-p",
            "no:logfire",
            "-p",
            "no:xdist",
            "-p",
            "no:anyio",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            str(resource_path),
        ]
        return cls.run_command(cmd, str(resource_path), cwd=cwd)

    @staticmethod
    def _parse_pytest_counts(output: str) -> tuple[int, int, int]:
        p_match = PYTEST_PASSED_RE.search(output)
        f_match = PYTEST_FAILED_RE.search(output)
        w_match = PYTEST_WARNINGS_RE.search(output)

        passed = int(p_match.group(1)) if p_match else 0
        failed = int(f_match.group(1)) if f_match else 0
        warnings = int(w_match.group(1)) if w_match else 0
        return passed, failed, warnings


def _evaluate_scan_penalties(scan: ResourceScanMetrics, recs: list[str]) -> float:
    penalty = 0.0
    if scan.complexity_violations:
        penalty += len(scan.complexity_violations) * 20.0
        recs.append(f"Decompose functions to satisfy M <= {MAX_ALLOWED_COMPLEXITY}: {scan.complexity_violations}")
    if scan.sanitization_violations:
        penalty += len(scan.sanitization_violations) * 15.0
        recs.append(f"Remediate private IP/subdomain leaks: {scan.sanitization_violations}")
    return penalty


def _evaluate_run_penalties(run: RunExecutionResult, file_path: str, recs: list[str]) -> float:
    penalty = 0.0
    if run.failed_count > 0 or run.exit_code != 0:
        penalty += max(run.failed_count * 25.0, 30.0)
        recs.append(f"Remediate {run.failed_count} failing tests in {file_path}")
    if run.warnings_count > 0:
        penalty += run.warnings_count * 5.0
        recs.append(f"Resolve {run.warnings_count} deprecation warnings")
    return penalty


def _is_critical_health(scan: ResourceScanMetrics, run: RunExecutionResult | None) -> bool:
    if scan.complexity_violations:
        return True
    return bool(run and (run.failed_count > 0 or run.exit_code != 0))


def _is_needs_remediation(score: float, scan: ResourceScanMetrics, run: RunExecutionResult | None) -> bool:
    if score < 85.0 or scan.sanitization_violations:
        return True
    return bool(run and run.warnings_count > 0)


class OutputReviewer:
    """Reviews scan and execution metrics, computing quality scores and recommendations."""

    @classmethod
    def evaluate(
        cls,
        scan: ResourceScanMetrics,
        run: RunExecutionResult | None = None,
        baseline: dict[str, Any] | None = None,
    ) -> ReviewEvaluation:
        """Evaluate scan metrics and test run results to produce quality score and health."""
        recs: list[str] = []
        deltas: dict[str, float] = {}

        score = 100.0 - _evaluate_scan_penalties(scan, recs)
        if run is not None:
            score -= _evaluate_run_penalties(run, scan.file_path, recs)
        if baseline:
            cls._calculate_deltas(scan, baseline, deltas)

        final_score = max(0.0, min(score, 100.0))
        status = cls._determine_health(final_score, scan, run)

        return ReviewEvaluation(
            resource_path=scan.file_path,
            health_status=status,
            quality_score=round(final_score, 1),
            scan_metrics=scan,
            run_result=run,
            actionable_recommendations=recs,
            metric_deltas=deltas,
        )

    @staticmethod
    def _calculate_deltas(scan: ResourceScanMetrics, baseline: dict[str, Any], deltas: dict[str, float]) -> None:
        prev_m = baseline.get("max_complexity")
        if prev_m is not None:
            deltas["complexity_delta"] = scan.max_complexity - prev_m

    @staticmethod
    def _determine_health(score: float, scan: ResourceScanMetrics, run: RunExecutionResult | None) -> HealthStatus:
        if _is_critical_health(scan, run):
            return HealthStatus.CRITICAL
        if _is_needs_remediation(score, scan, run):
            return HealthStatus.NEEDS_REMEDIATION
        return HealthStatus.HEALTHY


class FeedbackAnalyzer:
    """Analyzes scan metrics, test runs, and repository topology to generate feedback."""

    @classmethod
    def generate_feedback(
        cls,
        evaluations: list[ReviewEvaluation],
        root_dir: Path,
    ) -> list[ImprovementFeedback]:
        """Analyze review evaluations to generate prioritized improvement feedback."""
        feedback: list[ImprovementFeedback] = []
        for eval_item in evaluations:
            cls._analyze_refactoring_headroom(eval_item, feedback)
            cls._analyze_documentation_and_typing(eval_item, feedback)
            cls._analyze_execution_telemetry(eval_item, feedback)
            cls._analyze_positive_reinforcement(eval_item, feedback)
        cls._analyze_test_parity(evaluations, feedback)
        return sorted(feedback, key=lambda f: (cls._priority_rank(f.priority), f.target))

    @staticmethod
    def _priority_rank(priority: FeedbackPriority) -> int:
        rank_map = {
            FeedbackPriority.HIGH: 0,
            FeedbackPriority.MEDIUM: 1,
            FeedbackPriority.LOW: 2,
            FeedbackPriority.INFO: 3,
        }
        return rank_map.get(priority, 4)

    @classmethod
    def _analyze_refactoring_headroom(
        cls,
        item: ReviewEvaluation,
        feedback: list[ImprovementFeedback],
    ) -> None:
        scan = item.scan_metrics
        if not scan.near_threshold_functions:
            return
        funcs_str = ", ".join(scan.near_threshold_functions[:3])
        feedback.append(
            ImprovementFeedback(
                category=FeedbackCategory.PROACTIVE_REFACTOR,
                priority=FeedbackPriority.MEDIUM,
                target=scan.file_path,
                headline=f"Complexity/nesting near ceiling in {Path(scan.file_path).name}",
                prescriptive_guidance=(
                    f"Functions operating near threshold: {funcs_str}. "
                    "Proactive decomposition prevents future invariant violations."
                ),
                suggested_action="Decompose branching logic into predicate helpers or table-driven dispatch.",
            )
        )

    @classmethod
    def _analyze_documentation_and_typing(
        cls,
        item: ReviewEvaluation,
        feedback: list[ImprovementFeedback],
    ) -> None:
        scan = item.scan_metrics
        if scan.resource_type == ResourceType.TEST_SUITE:
            return

        stem = Path(scan.file_path).name
        if scan.functions_without_docstrings:
            funcs = ", ".join(scan.functions_without_docstrings[:4])
            feedback.append(
                ImprovementFeedback(
                    category=FeedbackCategory.DOCUMENTATION,
                    priority=FeedbackPriority.LOW,
                    target=scan.file_path,
                    headline=f"{len(scan.functions_without_docstrings)} public function(s) in {stem} lack docstrings",
                    prescriptive_guidance=f"Public functions without docstrings: {funcs}. Clear docstrings clarify contracts for peer agents.",
                    suggested_action="Add descriptive docstrings with argument and return types.",
                )
            )

        if scan.functions_without_type_hints:
            funcs = ", ".join(scan.functions_without_type_hints[:4])
            feedback.append(
                ImprovementFeedback(
                    category=FeedbackCategory.TYPE_SAFETY,
                    priority=FeedbackPriority.LOW,
                    target=scan.file_path,
                    headline=f"{len(scan.functions_without_type_hints)} public function(s) in {stem} lack complete typing",
                    prescriptive_guidance=f"Functions lacking return/param hints: {funcs}. Complete annotations enforce static guarantees.",
                    suggested_action="Add explicit return types and parameter annotations across public functions.",
                )
            )

    @classmethod
    def _analyze_execution_telemetry(
        cls,
        item: ReviewEvaluation,
        feedback: list[ImprovementFeedback],
    ) -> None:
        run = item.run_result
        if not run:
            return
        if run.duration_seconds > 2.0:
            stem = Path(item.resource_path).name
            feedback.append(
                ImprovementFeedback(
                    category=FeedbackCategory.PERFORMANCE,
                    priority=FeedbackPriority.MEDIUM,
                    target=item.resource_path,
                    headline=f"Test latency in {stem} ({run.duration_seconds:.2f}s) exceeds 2.0s fast-feedback ceiling",
                    prescriptive_guidance="Fast feedback loops (< 1.5s) are vital for autonomous agent agility.",
                    suggested_action="Profile slow fixtures, parallelize independent cases, or isolate mock timeouts.",
                )
            )

    @classmethod
    def _analyze_positive_reinforcement(
        cls,
        item: ReviewEvaluation,
        feedback: list[ImprovementFeedback],
    ) -> None:
        scan = item.scan_metrics
        if (
            item.health_status == HealthStatus.HEALTHY
            and item.quality_score == 100.0
            and scan.max_complexity <= 5
            and not scan.near_threshold_functions
        ):
            stem = Path(scan.file_path).name
            feedback.append(
                ImprovementFeedback(
                    category=FeedbackCategory.POSITIVE_REINFORCEMENT,
                    priority=FeedbackPriority.INFO,
                    target=scan.file_path,
                    headline=f"Architectural excellence in {stem}",
                    prescriptive_guidance=f"Zero defects, compact complexity (M={scan.max_complexity}, depth={scan.max_depth}), and clean structure.",
                    suggested_action="Preserve this structure and use as a reference design pattern for new resources.",
                )
            )

    @classmethod
    def _analyze_test_parity(
        cls,
        evaluations: list[ReviewEvaluation],
        feedback: list[ImprovementFeedback],
    ) -> None:
        test_stems = {
            Path(e.resource_path).stem.lower()
            for e in evaluations
            if e.scan_metrics.resource_type == ResourceType.TEST_SUITE
        }

        for item in evaluations:
            scan = item.scan_metrics
            if scan.resource_type not in (ResourceType.PYTHON_MODULE, ResourceType.SAMPLE_APP):
                continue
            stem = Path(scan.file_path).stem.lower()
            has_test = (
                f"test_{stem}" in test_stems
                or f"{stem}_test" in test_stems
                or any(stem in ts for ts in test_stems)
            )
            if not has_test:
                feedback.append(
                    ImprovementFeedback(
                        category=FeedbackCategory.TEST_PARITY,
                        priority=FeedbackPriority.HIGH,
                        target=scan.file_path,
                        headline=f"Missing companion test suite for {Path(scan.file_path).name}",
                        prescriptive_guidance=f"Module '{scan.file_path}' does not have a matching test suite in tests/.",
                        suggested_action=f"Create 'tests/test_{Path(scan.file_path).stem}.py' to expand automated verification.",
                    )
                )


class ResourceIterationWorkbench:
    """Coordinates the end-to-end Scan -> Run -> Review -> Feedback -> Iterate loop."""

    def __init__(self, root_dir: Path, baseline_path: Path | None = None) -> None:
        self.root_dir = root_dir
        self.baseline_path = baseline_path or (root_dir / ".data" / "iteration_baseline.json")

    def run_cycle(self, target_pattern: str | None = None, execute_tests: bool = True) -> IterationReport:
        """Execute full Scan -> Run -> Review -> Feedback -> Iterate cycle."""
        # Phase 1: SCAN
        all_scans = ResourceScanner.scan_directory(self.root_dir)
        filtered_scans = self._filter_scans(all_scans, target_pattern)
        baseline = self._load_baseline()

        evaluations: list[ReviewEvaluation] = []
        total_passed = 0
        total_failed = 0
        total_violations = 0

        # Phase 2 & 3: RUN & REVIEW
        for scan in filtered_scans:
            run_res = None
            if execute_tests and scan.resource_type == ResourceType.TEST_SUITE:
                run_res = ResourceRunner.run_tests_for_resource(Path(scan.file_path), cwd=self.root_dir)
                total_passed += run_res.passed_count
                total_failed += run_res.failed_count

            total_violations += len(scan.complexity_violations)
            base_entry = baseline.get(scan.file_path)
            evaluation = OutputReviewer.evaluate(scan, run_res, base_entry)
            evaluations.append(evaluation)

        # Phase 4: FEEDBACK (Meaningful Improvement Analysis)
        feedback_items = FeedbackAnalyzer.generate_feedback(evaluations, self.root_dir)

        # Phase 5: ITERATE
        report = self._synthesize_report(
            evaluations, total_passed, total_failed, total_violations, feedback_items
        )
        self._save_baseline(evaluations)
        return report

    def _filter_scans(self, scans: list[ResourceScanMetrics], pattern: str | None) -> list[ResourceScanMetrics]:
        if not pattern:
            return scans
        regex = re.compile(pattern)
        return [s for s in scans if regex.search(s.file_path)]

    @staticmethod
    def _resolve_overall_health_and_action(
        evals: list[ReviewEvaluation],
        failed: int,
        violations: int,
        avg_score: float,
        fb_list: list[ImprovementFeedback],
    ) -> tuple[HealthStatus, str]:
        if failed > 0 or violations > 0 or any(e.health_status == HealthStatus.CRITICAL for e in evals):
            return HealthStatus.CRITICAL, "Remediate failing tests and AST complexity violations before proceeding."
        if avg_score < 90.0 or any(e.health_status == HealthStatus.NEEDS_REMEDIATION for e in evals):
            return HealthStatus.NEEDS_REMEDIATION, "Refactor modules with high nesting or warnings to reach 100% quality gate."
        return HealthStatus.HEALTHY, ResourceIterationWorkbench._determine_healthy_action(fb_list)

    def _synthesize_report(
        self,
        evals: list[ReviewEvaluation],
        passed: int,
        failed: int,
        violations: int,
        feedback: list[ImprovementFeedback] | None = None,
    ) -> IterationReport:
        fb_list = feedback or []
        if not evals:
            return IterationReport(
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                overall_health=HealthStatus.HEALTHY,
                overall_score=100.0,
                resources_evaluated=[],
                improvement_feedback=fb_list,
            )

        avg_score = round(sum(e.quality_score for e in evals) / len(evals), 1)
        overall_health, action = self._resolve_overall_health_and_action(evals, failed, violations, avg_score, fb_list)

        return IterationReport(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            overall_health=overall_health,
            overall_score=avg_score,
            resources_evaluated=evals,
            total_passed_tests=passed,
            total_failed_tests=failed,
            total_complexity_violations=violations,
            prescriptive_action=action,
            improvement_feedback=fb_list,
        )

    @staticmethod
    def _determine_healthy_action(feedback: list[ImprovementFeedback]) -> str:
        actionable = [f for f in feedback if f.category != FeedbackCategory.POSITIVE_REINFORCEMENT]
        if actionable:
            top = actionable[0]
            return f"Certified healthy. Positive feedback opportunity [{top.category.value}]: {top.headline}"
        return "All repository resources certified. Ready for push or release."

    def render_report(self, report: IterationReport) -> str:
        """Render iteration scorecard and feedback section into formatted ASCII table."""
        lines = [
            "==========================================================================================",
            "🔬 RESOURCE ITERATION WORKBENCH — SCORECARD & METRICS",
            "==========================================================================================",
            f"Timestamp: {report.timestamp} | Overall Health: [{report.overall_health.value}] | Score: {report.overall_score}/100",
            f"Tests Passed: {report.total_passed_tests} | Tests Failed: {report.total_failed_tests} | Complexity Violations: {report.total_complexity_violations}",
            f"Prescriptive Action: {report.prescriptive_action}",
            "------------------------------------------------------------------------------------------",
            f"{'RESOURCE':<45} | {'TYPE':<13} | {'M':<3} | {'SCORE':<5} | {'STATUS'}",
            "------------------------------------------------------------------------------------------",
        ]

        for e in sorted(report.resources_evaluated, key=lambda x: x.quality_score):
            rel_path = str(Path(e.resource_path).name)
            m_val = e.scan_metrics.max_complexity
            lines.append(
                f"{rel_path[:45]:<45} | {e.scan_metrics.resource_type.value[:13]:<13} | "
                f"{m_val:<3} | {e.quality_score:<5.1f} | {e.health_status.value}"
            )
            for rec in e.actionable_recommendations:
                lines.append(f"    👉 {rec}")

        if report.improvement_feedback:
            lines.extend(self._render_feedback_section(report.improvement_feedback))

        lines.append("==========================================================================================")
        return "\n".join(lines)

    @staticmethod
    def _render_feedback_section(feedback: list[ImprovementFeedback]) -> list[str]:
        lines = [
            "------------------------------------------------------------------------------------------",
            f"💡 POSITIVE FEEDBACK LOOP — MEANINGFUL IMPROVEMENT OPPORTUNITIES ({len(feedback)} IDENTIFIED)",
            "------------------------------------------------------------------------------------------",
        ]
        for fb in feedback[:8]:
            lines.append(f"[{fb.priority.value}] [{fb.category.value}] {Path(fb.target).name}")
            lines.append(f"  Headline: {fb.headline}")
            lines.append(f"  Guidance: {fb.prescriptive_guidance}")
            lines.append(f"  Next Action: {fb.suggested_action}")
            lines.append("")
        return lines

    def _load_baseline(self) -> dict[str, Any]:
        if not self.baseline_path.is_file():
            return {}
        try:
            return json.loads(self.baseline_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_baseline(self, evals: list[ReviewEvaluation]) -> None:
        try:
            self.baseline_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                e.resource_path: {
                    "max_complexity": e.scan_metrics.max_complexity,
                    "max_depth": e.scan_metrics.max_depth,
                    "quality_score": e.quality_score,
                }
                for e in evals
            }
            self.baseline_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def export_feedback_to_sdlc(feedback_items: list[ImprovementFeedback]) -> list[dict[str, Any]]:
        """Converts improvement feedback into structured SDLC backlog tasks."""
        tasks: list[dict[str, Any]] = []
        priority_map = {
            FeedbackPriority.HIGH: "P1_HIGH",
            FeedbackPriority.MEDIUM: "P2_MEDIUM",
            FeedbackPriority.LOW: "P3_LOW",
            FeedbackPriority.INFO: "P3_LOW",
        }
        for idx, item in enumerate(feedback_items, start=1):
            if item.category == FeedbackCategory.POSITIVE_REINFORCEMENT:
                continue
            tasks.append({
                "resource_id": f"fb-{idx}",
                "kind": "issue",
                "number": 1000 + idx,
                "title": f"[{item.category.value}] {item.headline}",
                "labels": ["enhancement", item.category.value.lower()],
                "lifecycle_state": "Backlog",
                "priority": priority_map.get(item.priority, "P2_MEDIUM"),
                "effort_points": 2 if item.priority == FeedbackPriority.LOW else 5,
                "business_value": 8 if item.priority == FeedbackPriority.HIGH else 4,
                "prescriptive_guidance": item.prescriptive_guidance,
                "suggested_action": item.suggested_action,
                "target": item.target,
            })
        return tasks


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for resource iteration workbench."""
    parser = argparse.ArgumentParser(description="Resource Iteration Workbench for AI Agents.")
    parser.add_argument("--root", "-r", type=Path, default=Path("."), help="Repository root directory.")
    parser.add_argument("--pattern", "-p", type=str, default=None, help="Regex pattern to filter target files.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip executing test suites.")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON report.")
    parser.add_argument("--export-backlog", type=Path, default=None, help="Export feedback to SDLC backlog JSON.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute resource iteration workbench CLI runner."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    workbench = ResourceIterationWorkbench(root_dir=args.root)
    report = workbench.run_cycle(target_pattern=args.pattern, execute_tests=not args.skip_tests)

    if args.export_backlog:
        tasks = workbench.export_feedback_to_sdlc(report.improvement_feedback)
        args.export_backlog.parent.mkdir(parents=True, exist_ok=True)
        args.export_backlog.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
        print(f"Exported {len(tasks)} feedback items to {args.export_backlog}")

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(workbench.render_report(report))

    return 0 if report.overall_health in (HealthStatus.HEALTHY, HealthStatus.NEEDS_REMEDIATION) else 1


if __name__ == "__main__":
    sys.exit(main())
