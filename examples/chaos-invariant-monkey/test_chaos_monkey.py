"""Unit tests for the Chaos Invariant Injector & Agent Resilience Benchmark."""

# sentinel: allow[ZeroTrustSanitization] — test asserting chaos egress poison payload

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chaos_monkey import (
    ChaosMutation,
    apply_chaos_mutation,
    evaluate_resilience,
    inject_assertion_desync,
    inject_complexity_spike,
    inject_egress_poison,
    inject_nesting_spike,
    inject_transient_fault,
    main,
    rollback_chaos_mutation,
    to_markdown,
    to_sarif,
)


def test_inject_complexity_spike_function_and_fallback() -> None:
    """Verify complexity spike insertion into function and raw code."""
    code_fn = "def calculate(a: int) -> int:\n    return a + 1\n"
    res_fn = inject_complexity_spike(code_fn)
    assert ("# Chaos Mutation: Complexity Spike" in res_fn, "elif x == 10:" in res_fn) == (True, True)

    raw_code = "x = 10\n"
    res_raw = inject_complexity_spike(raw_code)
    assert ("def _chaos_spike" in res_raw, "elif x == 10:" in res_raw) == (True, True)


def test_inject_nesting_spike() -> None:
    """Verify nesting depth spike injection."""
    code = "x = 42\n"
    res = inject_nesting_spike(code)
    assert ("# Chaos Mutation: Nesting Depth Spike" in res, res.count("if True:") == 6) == (True, True)


def test_inject_assertion_desync() -> None:
    """Verify assertion sprawl injection."""
    code = "assert (a, b) == (1, 2)\n"
    res = inject_assertion_desync(code)
    assert ("# Chaos Mutation: Assertion Desynchronization" in res, "assert val_f == 6" in res) == (True, True)


def test_inject_egress_poison() -> None:
    """Verify egress poison injection."""
    code = "API_HOST = 'http://example.com'\n"
    res = inject_egress_poison(code)
    assert ("# Chaos Mutation: Egress Leak" in res, "192.168.1.1" in res) == (True, True)


def test_inject_transient_fault() -> None:
    """Verify HTTP 429 fault response generation."""
    fault = inject_transient_fault()
    assert (fault["status_code"], fault["headers"]["Retry-After"]) == (429, "2.0")


def test_apply_and_rollback_chaos_mutation(tmp_path: Path) -> None:
    """Verify file mutation application and clean rollback."""
    target = tmp_path / "sample.py"
    target.write_text("def work(): pass\n", encoding="utf-8")

    mut = apply_chaos_mutation(target, "COMPLEXITY_SPIKE")
    assert (mut.mutation_type, "elif x == 10:" in target.read_text(encoding="utf-8")) == (
        "COMPLEXITY_SPIKE",
        True,
    )

    ok_rollback = rollback_chaos_mutation(mut)
    assert (ok_rollback, target.read_text(encoding="utf-8")) == (True, "def work(): pass\n")


def test_rollback_nonexistent_original(tmp_path: Path) -> None:
    """Verify rollback of newly created mutated file unlinks it."""
    target = tmp_path / "new_file.py"
    mut = ChaosMutation("mut-1", "NESTING_SPIKE", str(target), "", "mutated", 0.0)
    target.write_text("mutated", encoding="utf-8")

    ok = rollback_chaos_mutation(mut)
    assert (ok, target.exists()) == (True, False)


def test_evaluate_resilience_and_telemetry() -> None:
    """Verify resilience telemetry and repair ratio calculation."""
    mut = ChaosMutation("m1", "COMPLEXITY_SPIKE", "/tmp/p.py", "orig", "mut", 0.0)
    report = evaluate_resilience([mut], 1, 1)
    sarif = to_sarif(report)
    md = to_markdown(report)

    assert (report.total_injected, report.repair_ratio) == (1, 1.0)
    assert (sarif["version"], len(sarif["runs"][0]["results"])) == ("2.1.0", 1)
    assert ("# Chaos Invariant Resilience Benchmark Report" in md, "100.0%" in md) == (True, True)


def test_main_cli_inject_and_benchmark(tmp_path: Path, capsys: Any) -> None:
    """Verify CLI commands for inject and benchmark."""
    target = tmp_path / "test.py"
    target.write_text("x = 1\n", encoding="utf-8")

    rc_inj = main(["inject", str(target), "--type", "NESTING_SPIKE"])
    capsys.readouterr()
    assert (rc_inj, "if True:" in target.read_text(encoding="utf-8")) == (0, True)

    rc_bench = main(["benchmark", "--format", "json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert (rc_bench, payload["total_injected"] > 0) == (0, True)

    rc_md = main(["benchmark", "--format", "markdown"])
    captured_md = capsys.readouterr()
    assert (rc_md, "# Chaos Invariant Resilience Benchmark Report" in captured_md.out) == (0, True)

    rc_sarif = main(["benchmark", "--format", "sarif"])
    captured_sarif = capsys.readouterr()
    sarif_data = json.loads(captured_sarif.out)
    assert (rc_sarif, sarif_data["version"]) == (0, "2.1.0")
