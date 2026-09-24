"""Unit tests for the Agent Experience (AX) Evaluator exhibit."""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add current directory to sys.path to allow standalone import
sys.path.insert(0, str(Path(__file__).parent))

from ax_evaluator import (
    AgentExperienceAuditor,
    AXFinding,
    AXScoreReport,
    ModuleTelemetry,
    _is_ignored_path,
    collect_target_files,
    compute_aggregate_scores,
    evaluate_file,
    generate_sarif_report,
    main,
    render_markdown_summary,
)


class TestAgentExperienceAuditor(unittest.TestCase):
    """Test suite for AST-based Agent Experience auditing rules."""

    def test_tool_schema_auditor_flags_permissive_kwargs(self) -> None:
        code = "def bad_tool(param: int, **kwargs):\n    pass\n"
        tree = ast.parse(code)
        auditor = AgentExperienceAuditor("test_tool.py")
        auditor.visit(tree)

        finding_rules = [f.rule_id for f in auditor.telemetry.findings]
        self.assertEqual(
            (auditor.telemetry.total_schemas, auditor.telemetry.permissive_schemas, finding_rules),
            (1, 1, ["AX001"]),
        )

    def test_tool_schema_auditor_accepts_negative_bounds(self) -> None:
        code = (
            "schema = {\n"
            "    'type': 'object',\n"
            "    'properties': {'name': {'type': 'string'}},\n"
            "    'additionalProperties': False,\n"
            "}\n"
        )
        tree = ast.parse(code)
        auditor = AgentExperienceAuditor("test_tool.py")
        auditor.visit(tree)

        self.assertEqual(
            (auditor.telemetry.total_schemas, auditor.telemetry.permissive_schemas, len(auditor.telemetry.findings)),
            (1, 0, 0),
        )

    def test_tool_schema_auditor_flags_permissive_dict(self) -> None:
        code = (
            "schema = {\n"
            "    'type': 'object',\n"
            "    'properties': {'name': {'type': 'string'}},\n"
            "}\n"
        )
        tree = ast.parse(code)
        auditor = AgentExperienceAuditor("test_tool.py")
        auditor.visit(tree)

        finding_rules = [f.rule_id for f in auditor.telemetry.findings]
        self.assertEqual(
            (auditor.telemetry.total_schemas, auditor.telemetry.permissive_schemas, finding_rules),
            (1, 1, ["AX001"]),
        )

    def test_diagnostic_auditor_flags_unstructured_raise(self) -> None:
        code = "def fail():\n    raise ValueError('something went wrong')\n"
        tree = ast.parse(code)
        auditor = AgentExperienceAuditor("test_diag.py")
        auditor.visit(tree)

        finding_rules = [f.rule_id for f in auditor.telemetry.findings]
        self.assertEqual(
            (auditor.telemetry.total_diagnostics, auditor.telemetry.structured_diagnostics, finding_rules),
            (1, 0, ["AX002"]),
        )

    def test_diagnostic_auditor_accepts_structured_raise(self) -> None:
        code = "def fail():\n    raise CustomError('code', {'expected': 1, 'actual': 2})\n"
        tree = ast.parse(code)
        auditor = AgentExperienceAuditor("test_diag.py")
        auditor.visit(tree)

        self.assertEqual(
            (auditor.telemetry.total_diagnostics, auditor.telemetry.structured_diagnostics, len(auditor.telemetry.findings)),
            (1, 1, 0),
        )

    def test_diagnostic_auditor_flags_unstructured_assert(self) -> None:
        code = "assert x == y, 'x must equal y'\n"
        tree = ast.parse(code)
        auditor = AgentExperienceAuditor("test_assert.py")
        auditor.visit(tree)

        finding_rules = [f.rule_id for f in auditor.telemetry.findings]
        self.assertEqual(
            (auditor.telemetry.total_diagnostics, auditor.telemetry.structured_diagnostics, finding_rules),
            (1, 0, ["AX002"]),
        )

    def test_cognitive_impedance_auditor_flags_high_complexity(self) -> None:
        code = (
            "def high_complexity(x: int) -> int:\n"
            "    if x == 1: return 1\n"
            "    if x == 2: return 2\n"
            "    if x == 3: return 3\n"
            "    if x == 4: return 4\n"
            "    if x == 5: return 5\n"
            "    if x == 6: return 6\n"
            "    if x == 7: return 7\n"
            "    return 0\n"
        )
        tree = ast.parse(code)
        auditor = AgentExperienceAuditor("test_comp.py")
        auditor.visit(tree)

        finding_rules = [f.rule_id for f in auditor.telemetry.findings]
        self.assertEqual(
            (auditor.telemetry.total_functions, auditor.telemetry.high_impedance_functions, finding_rules),
            (1, 1, ["AX003"]),
        )

    def test_cognitive_impedance_auditor_flags_deep_nesting(self) -> None:
        code = (
            "def deep_nesting(a, b, c, d):\n"
            "    if a:\n"
            "        if b:\n"
            "            if c:\n"
            "                if d:\n"
            "                    return True\n"
            "    return False\n"
        )
        tree = ast.parse(code)
        auditor = AgentExperienceAuditor("test_depth.py")
        auditor.visit(tree)

        finding_rules = [f.rule_id for f in auditor.telemetry.findings]
        self.assertEqual(
            (auditor.telemetry.total_functions, auditor.telemetry.high_impedance_functions, finding_rules),
            (1, 1, ["AX003"]),
        )

    def test_visitor_and_async_nodes(self) -> None:
        code = (
            "async def async_worker(x):\n"
            "    for a in x:\n"
            "        async for b in a:\n"
            "            while b > 0:\n"
            "                try:\n"
            "                    async with lock:\n"
            "                        with context:\n"
            "                            if b == 1 and b != 2:\n"
            "                                pass\n"
            "                except ValueError:\n"
            "                    pass\n"
        )
        tree = ast.parse(code)
        auditor = AgentExperienceAuditor("test_async.py")
        auditor.visit(tree)
        self.assertEqual((auditor.telemetry.total_functions, auditor.telemetry.total_complexity > 1), (1, True))

    def test_untyped_kwonly_arg(self) -> None:
        code = "def kwonly_func(*, param):\n    pass\n"
        tree = ast.parse(code)
        auditor = AgentExperienceAuditor("test_kwonly.py")
        auditor.visit(tree)
        finding_rules = [f.rule_id for f in auditor.telemetry.findings]
        self.assertEqual((auditor.telemetry.permissive_schemas, finding_rules), (1, ["AX001"]))


class TestAggregateScoringAndReporting(unittest.TestCase):
    """Test suite for aggregate S_AX score computation and report generation."""

    def test_composite_scoring_and_sarif_export(self) -> None:
        clean_telemetry = ModuleTelemetry(
            total_diagnostics=10,
            structured_diagnostics=10,
            total_schemas=5,
            permissive_schemas=0,
            total_functions=20,
            high_impedance_functions=0,
            findings=[],
        )
        report = compute_aggregate_scores([clean_telemetry], threshold=0.75)
        sarif = generate_sarif_report(report)

        report_summary = (report.passed, report.dai, report.ifi, report.cim, report.composite_score)
        sarif_summary = (sarif["version"], len(sarif["runs"]), sarif["runs"][0]["tool"]["driver"]["name"])

        self.assertEqual(report_summary, (True, 1.0, 0.0, 1.0, 1.0))
        self.assertEqual(sarif_summary, ("2.1.0", 1, "ax-evaluator"))

    def test_permissive_schema_fails_verdict(self) -> None:
        dirty_finding = AXFinding(
            rule_id="AX001",
            file_path="model.py",
            line_number=12,
            symbol_name="open_interface",
            message="Unconstrained **kwargs",
            remediation_hint="Forbid extra parameters",
        )
        dirty_telemetry = ModuleTelemetry(
            total_diagnostics=10,
            structured_diagnostics=10,
            total_schemas=5,
            permissive_schemas=1,
            total_functions=20,
            high_impedance_functions=0,
            findings=[dirty_finding],
        )
        report = compute_aggregate_scores([dirty_telemetry], threshold=0.50)
        self.assertEqual((report.passed, report.ifi), (False, 0.2))

    def test_render_markdown_and_evaluate_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
            tmp.write("def foo():\n    pass\n")
            tmp_path = Path(tmp.name)

        try:
            telemetry = evaluate_file(tmp_path)
            report = compute_aggregate_scores([telemetry])
            markdown = render_markdown_summary(report)
            collected = collect_target_files([str(tmp_path)])

            self.assertEqual(
                (telemetry.total_functions, report.passed, "Evaluation Summary" in markdown, collected),
                (1, True, True, [tmp_path]),
            )
        finally:
            tmp_path.unlink()

    def test_markdown_truncation_with_many_findings(self) -> None:
        findings = [
            AXFinding("AX003", "mod.py", i, f"fn_{i}", "Exceeds headroom", "Refactor")
            for i in range(35)
        ]
        report = AXScoreReport(
            files_analyzed=1,
            dai=1.0,
            ifi=0.0,
            cim=0.5,
            composite_score=0.8,
            threshold=0.75,
            passed=True,
            findings_count=35,
            findings=findings,
        )
        md = render_markdown_summary(report)
        self.assertEqual(("Truncated 5 additional findings" in md, report.passed), (True, True))

    def test_ignored_paths(self) -> None:
        ignored_cases = (
            _is_ignored_path(Path(".git/hook.py")),
            _is_ignored_path(Path("build/temp.py")),
            _is_ignored_path(Path("src/safe.py")),
        )
        self.assertEqual(ignored_cases, (True, True, False))

    def test_cli_main_execution_flows(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
            tmp.write("def clean_fn():\n    return 42\n")
            src_path = Path(tmp.name)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as out_tmp:
            out_path = Path(out_tmp.name)

        try:
            with patch("sys.argv", ["ax_evaluator.py", str(src_path), "--format", "json", "--out", str(out_path)]):
                exit_code = main()
            written_json = json.loads(out_path.read_text(encoding="utf-8"))

            with patch("sys.argv", ["ax_evaluator.py", str(src_path), "--format", "sarif"]), patch("sys.stdout.write"):
                sarif_code = main()

            with patch("sys.argv", ["ax_evaluator.py", str(src_path), "--format", "markdown"]), patch("sys.stdout.write"):
                md_code = main()

            with patch("sys.argv", ["ax_evaluator.py", str(src_path), "--format", "text"]), patch("sys.stdout.write"):
                txt_code = main()

            self.assertEqual(
                (exit_code, written_json["passed"], sarif_code, md_code, txt_code),
                (0, True, 0, 0, 0),
            )
        finally:
            src_path.unlink()
            out_path.unlink()

    def test_cli_main_no_files_branch(self) -> None:
        with patch("sys.argv", ["ax_evaluator.py", "non_existent_empty_dir_12345/"]), \
             patch("sys.stderr.write") as mock_stderr:
            exit_code = main()
        self.assertEqual((exit_code, mock_stderr.called), (0, True))


if __name__ == "__main__":
    unittest.main()
