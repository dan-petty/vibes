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

    def to_dict(self) -> dict[str, Any]:
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
        return {
            "resource_path": self.resource_path,
            "health_status": self.health_status.value,
            "quality_score": self.quality_score,
            "scan_metrics": self.scan_metrics.to_dict(),
            "run_result": self.run_result.to_dict() if self.run_result else None,
            "actionable_recommendations": self.actionable_recommendations,
            "metric_deltas": self.metric_deltas,
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "overall_health": self.overall_health.value,
            "overall_score": self.overall_score,
            "total_passed_tests": self.total_passed_tests,
            "total_failed_tests": self.total_failed_tests,
            "total_complexity_violations": self.total_complexity_violations,
            "prescriptive_action": self.prescriptive_action,
            "resources_evaluated": [r.to_dict() for r in self.resources_evaluated],
        }


class ASTMetricCalculator:
    """Calculates McCabe cyclomatic complexity, nesting depth, and sanitization."""

    @classmethod
    def calculate_complexity(cls, node: ast.AST) -> int:
        """Calculate McCabe complexity M = E - N + 2P for a function AST node."""
        complexity = 1
        branch_types = (ast.If, ast.While, ast.For, ast.AsyncFor, ast.ExceptHandler, ast.Assert, ast.IfExp)
        for child in ast.walk(node):
            if isinstance(child, branch_types):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
        return complexity

    @classmethod
    def calculate_max_nesting(cls, root: ast.AST) -> int:
        """Calculate the deepest indentation nesting depth inside a function."""
        nesting_types = (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler)

        def walk_depth(node: ast.AST, depth: int) -> int:
            cur_depth = depth + 1 if isinstance(node, nesting_types) else depth
            deepest = cur_depth
            for child in ast.iter_child_nodes(node):
                sub_deep = walk_depth(child, cur_depth)
                if sub_deep > deepest:
                    deepest = sub_deep
            return deepest

        max_seen = 0
        for stmt in getattr(root, "body", []):
            d = walk_depth(stmt, 1)
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

    @classmethod
    def _check_ip_string(cls, text: str, lineno: int, violations: list[str]) -> None:
        for match in IPV4_PATTERN.findall(text):
            try:
                ip_obj = ipaddress.ip_address(match)
                if ip_obj.is_private and not any(ip_obj in net for net in ALLOWED_TEST_NETWORKS):
                    violations.append(f"Line {lineno}: Hardcoded RFC 1918 IP '{match}'. Use RFC 5737 or loopback.")
            except ValueError:
                continue

    @classmethod
    def _check_subdomain_string(cls, text: str, lineno: int, violations: list[str]) -> None:
        if "http://" in text or "https://" in text:
            if CANONICAL_MOCK_DOMAIN in text:
                match = re.search(r"https?://([^/:]+)", text)
                if match:
                    host = match.group(1)
                    if host != CANONICAL_MOCK_DOMAIN and host.endswith(f".{CANONICAL_MOCK_DOMAIN}"):
                        violations.append(f"Line {lineno}: Subdomain '{host}' detected. Use standard '{CANONICAL_MOCK_DOMAIN}'.")


class ResourceScanner:
    """Scans repository files and computes structured baseline metrics."""

    @classmethod
    def scan_python_file(cls, path: Path) -> ResourceScanMetrics:
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

        max_complexity = 1
        max_depth = 1
        complexity_violations: list[str] = []

        for fn in func_nodes:
            c = ASTMetricCalculator.calculate_complexity(fn)
            d = ASTMetricCalculator.calculate_max_nesting(fn)
            if c > max_complexity:
                max_complexity = c
            if d > max_depth:
                max_depth = d
            if c > MAX_ALLOWED_COMPLEXITY:
                complexity_violations.append(f"{fn.name}:{fn.lineno} M={c} > {MAX_ALLOWED_COMPLEXITY}")
            if d > MAX_ALLOWED_NESTING:
                complexity_violations.append(f"{fn.name}:{fn.lineno} Depth={d} > {MAX_ALLOWED_NESTING}")

        # Test suites often contain intentional mock IP strings to test sanitizers
        is_test = cls._classify_resource_type(path) == ResourceType.TEST_SUITE
        sanitization_violations = [] if is_test else ASTMetricCalculator.check_sanitization(tree)

        return ResourceScanMetrics(
            file_path=str(path),
            resource_type=cls._classify_resource_type(path),
            loc=loc,
            functions_count=len(func_nodes),
            classes_count=len(class_nodes),
            max_complexity=max_complexity,
            max_depth=max_depth,
            complexity_violations=complexity_violations,
            sanitization_violations=sanitization_violations,
        )

    @classmethod
    def scan_directory(cls, root_dir: Path) -> list[ResourceScanMetrics]:
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
        cmd = [sys.executable, "-m", "pytest", str(resource_path)]
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


class OutputReviewer:
    """Reviews scan and execution metrics, computing quality scores and recommendations."""

    @classmethod
    def evaluate(
        cls,
        scan: ResourceScanMetrics,
        run: RunExecutionResult | None = None,
        baseline: dict[str, Any] | None = None,
    ) -> ReviewEvaluation:
        score = 100.0
        recs: list[str] = []
        deltas: dict[str, float] = {}

        # 1. Deduct for complexity and sanitization violations
        if scan.complexity_violations:
            score -= len(scan.complexity_violations) * 20.0
            recs.append(f"Decompose functions to satisfy M <= {MAX_ALLOWED_COMPLEXITY}: {scan.complexity_violations}")

        if scan.sanitization_violations:
            score -= len(scan.sanitization_violations) * 15.0
            recs.append(f"Remediate private IP/subdomain leaks: {scan.sanitization_violations}")

        # 2. Factor in execution run results
        if run is not None:
            if run.failed_count > 0 or run.exit_code != 0:
                score -= max(run.failed_count * 25.0, 30.0)
                recs.append(f"Remediate {run.failed_count} failing tests in {scan.file_path}")
            if run.warnings_count > 0:
                score -= run.warnings_count * 5.0
                recs.append(f"Resolve {run.warnings_count} deprecation warnings")

        # 3. Factor in baseline comparisons (deltas)
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
        if scan.complexity_violations or (run and (run.failed_count > 0 or run.exit_code != 0)):
            return HealthStatus.CRITICAL
        if score < 85.0 or scan.sanitization_violations or (run and run.warnings_count > 0):
            return HealthStatus.NEEDS_REMEDIATION
        return HealthStatus.HEALTHY


class ResourceIterationWorkbench:
    """Coordinates the end-to-end Scan -> Run -> Review -> Iterate loop."""

    def __init__(self, root_dir: Path, baseline_path: Path | None = None) -> None:
        self.root_dir = root_dir
        self.baseline_path = baseline_path or (root_dir / ".data" / "iteration_baseline.json")

    def run_cycle(self, target_pattern: str | None = None, execute_tests: bool = True) -> IterationReport:
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

        # Phase 4: ITERATE
        report = self._synthesize_report(evaluations, total_passed, total_failed, total_violations)
        self._save_baseline(evaluations)
        return report

    def _filter_scans(self, scans: list[ResourceScanMetrics], pattern: str | None) -> list[ResourceScanMetrics]:
        if not pattern:
            return scans
        regex = re.compile(pattern)
        return [s for s in scans if regex.search(s.file_path)]

    def _synthesize_report(
        self, evals: list[ReviewEvaluation], passed: int, failed: int, violations: int
    ) -> IterationReport:
        if not evals:
            return IterationReport(time.strftime("%Y-%m-%dT%H:%M:%SZ"), HealthStatus.HEALTHY, 100.0, [])

        avg_score = round(sum(e.quality_score for e in evals) / len(evals), 1)
        any_critical = any(e.health_status == HealthStatus.CRITICAL for e in evals)
        any_remediation = any(e.health_status == HealthStatus.NEEDS_REMEDIATION for e in evals)

        if any_critical or failed > 0 or violations > 0:
            overall_health = HealthStatus.CRITICAL
            action = "Remediate failing tests and AST complexity violations before proceeding."
        elif any_remediation or avg_score < 90.0:
            overall_health = HealthStatus.NEEDS_REMEDIATION
            action = "Refactor modules with high nesting or warnings to reach 100% quality gate."
        else:
            overall_health = HealthStatus.HEALTHY
            action = "All repository resources certified. Ready for push or release."

        return IterationReport(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            overall_health=overall_health,
            overall_score=avg_score,
            resources_evaluated=evals,
            total_passed_tests=passed,
            total_failed_tests=failed,
            total_complexity_violations=violations,
            prescriptive_action=action,
        )

    def render_report(self, report: IterationReport) -> str:
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

        lines.append("==========================================================================================")
        return "\n".join(lines)

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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resource Iteration Workbench for AI Agents.")
    parser.add_argument("--root", "-r", type=Path, default=Path("."), help="Repository root directory.")
    parser.add_argument("--pattern", "-p", type=str, default=None, help="Regex pattern to filter target files.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip executing test suites.")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON report.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    workbench = ResourceIterationWorkbench(root_dir=args.root)
    report = workbench.run_cycle(target_pattern=args.pattern, execute_tests=not args.skip_tests)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(workbench.render_report(report))

    return 0 if report.overall_health in (HealthStatus.HEALTHY, HealthStatus.NEEDS_REMEDIATION) else 1


if __name__ == "__main__":
    sys.exit(main())
