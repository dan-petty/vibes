"""Unit tests for the Reliability SLO engine."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from reliability_slo import (
    DEFAULT_OBJECTIVES,
    BudgetStatus,
    LoopPhase,
    ServiceLevelObjective,
    decide_phase,
    evaluate_objective,
    load_history,
    main,
    measure_report,
    record_iteration,
)


def _resource(health: str = "HEALTHY", **scan: object) -> dict:
    metrics = {
        "complexity_violations": [],
        "sanitization_violations": [],
        "near_threshold_functions": [],
        **scan,
    }
    return {"resource_path": "m.py", "health_status": health, "quality_score": 100.0,
            "scan_metrics": metrics, "run_result": None}


def _report(resources: list[dict], feedback: list[dict] | None = None) -> dict:
    return {
        "timestamp": "2026-01-01T00:00:00Z",
        "resources_evaluated": resources,
        "improvement_feedback": feedback or [],
    }


def _history(sli: str, pairs: list[tuple[int, int]]) -> list[dict]:
    return [{"timestamp": str(i), "measurements": {sli: [good, valid]}} for i, (good, valid) in enumerate(pairs)]


def test_measures_report_across_every_registered_indicator() -> None:
    """Each SLI reports good events over valid events, never an average."""
    report = _report([
        _resource(),
        _resource(health="CRITICAL", complexity_violations=["f: M=12"]),
        _resource(near_threshold_functions=["g (M=9)"]),
    ])
    measured = measure_report(report)
    assert (measured["gate_pass_rate"].good_events, measured["gate_pass_rate"].valid_events) == (2, 3)
    assert measured["invariant_compliance"].ratio == pytest.approx(2 / 3)
    assert measured["headroom_saturation"].ratio == pytest.approx(2 / 3)


def test_latency_sli_prefers_self_reported_execution_time() -> None:
    """Wall-clock includes interpreter boot; the suite's own report is the honest number."""
    resource = _resource()
    resource["run_result"] = {"duration_seconds": 2.4, "execution_seconds": 0.38}
    assert measure_report(_report([resource]))["feedback_latency"].ratio == 1.0

    resource["run_result"] = {"duration_seconds": 2.4, "execution_seconds": None}
    assert measure_report(_report([resource]))["feedback_latency"].ratio == 0.0


def test_toil_sli_counts_machine_fixable_backlog_items() -> None:
    """Toil is work a machine could do that an agent is doing instead."""
    report = _report([_resource()], feedback=[
        {"category": "PROACTIVE_REFACTOR"},
        {"category": "DOCUMENTATION"},
        {"category": "TEST_PARITY"},
        {"category": "POSITIVE_REINFORCEMENT"},
    ])
    measured = measure_report(report)
    assert (measured["toil_containment"].good_events, measured["toil_containment"].valid_events) == (1, 3)


def test_spending_inside_budget_is_not_an_incident() -> None:
    """The whole point of a budget: measured failure below target is normal operation."""
    objective = ServiceLevelObjective("gate_pass_rate", target=0.90, window_iterations=10, rationale="r")
    state = evaluate_objective(objective, _history("gate_pass_rate", [(95, 100)] * 10))
    assert (state.status, round(state.consumed, 2)) == (BudgetStatus.HEALTHY, 0.5)
    assert decide_phase([state]).phase is LoopPhase.PROACTIVE_ELEVATION


def test_exhausted_budget_freezes_proactive_work() -> None:
    """Past the objective, remediation takes the whole priority."""
    objective = ServiceLevelObjective("gate_pass_rate", target=0.99, window_iterations=10, rationale="r")
    state = evaluate_objective(objective, _history("gate_pass_rate", [(90, 100)] * 10))
    assert state.status is BudgetStatus.EXHAUSTED
    assert decide_phase([state]).phase is LoopPhase.REACTIVE_REMEDIATION


def test_zero_budget_objective_trips_on_first_breach() -> None:
    """A 1.0 target reserves no budget: invariants have no acceptable failure rate."""
    objective = ServiceLevelObjective("invariant_compliance", target=1.0, window_iterations=20, rationale="r")
    assert evaluate_objective(objective, _history("invariant_compliance", [(99, 100)])).status is BudgetStatus.EXHAUSTED
    assert evaluate_objective(objective, _history("invariant_compliance", [(100, 100)])).status is BudgetStatus.HEALTHY


def test_single_iteration_does_not_trigger_burn_alert() -> None:
    """Burn rate divides by elapsed window, so early iterations always look catastrophic."""
    objective = ServiceLevelObjective("gate_pass_rate", target=0.98, window_iterations=20, rationale="r")
    early = evaluate_objective(objective, _history("gate_pass_rate", [(985, 1000)]))
    assert (early.status, early.burn_rate > 2.0) == (BudgetStatus.HEALTHY, True)

    sustained = evaluate_objective(objective, _history("gate_pass_rate", [(985, 1000)] * 4))
    assert sustained.status is BudgetStatus.BURNING


def test_permanently_unspent_budget_is_itself_a_finding() -> None:
    """A loop that never fails cannot tell reliable from unambitious."""
    objectives = [
        ServiceLevelObjective("gate_pass_rate", target=0.90, window_iterations=5, rationale="r"),
        ServiceLevelObjective("feedback_latency", target=0.90, window_iterations=5, rationale="r"),
    ]
    history = [
        {"timestamp": str(i), "measurements": {"gate_pass_rate": [10, 10], "feedback_latency": [10, 10]}}
        for i in range(5)
    ]
    states = [evaluate_objective(objective, history) for objective in objectives]
    assert all(state.status is BudgetStatus.UNSPENT for state in states)
    assert decide_phase(states).phase is LoopPhase.OBJECTIVE_REVIEW


def test_window_aggregates_events_rather_than_averaging_iterations() -> None:
    """An iteration measuring two resources must not weigh as much as one measuring two hundred."""
    objective = ServiceLevelObjective("gate_pass_rate", target=0.50, window_iterations=10, rationale="r")
    state = evaluate_objective(objective, _history("gate_pass_rate", [(0, 2), (100, 100)]))
    assert state.observed == pytest.approx(100 / 102)


def test_history_is_bounded_and_survives_a_corrupt_ledger(tmp_path: Path) -> None:
    """The ledger is telemetry, not a source of truth; a corrupt file must not halt the loop."""
    ledger = tmp_path / "slo_history.json"
    ledger.write_text("{not json", encoding="utf-8")
    assert load_history(ledger) == []

    for _ in range(3):
        history = record_iteration(_report([_resource()]), ledger)
    assert len(history) == 3
    assert len(json.loads(ledger.read_text(encoding="utf-8"))) == 3


def test_default_objectives_cover_every_registered_indicator() -> None:
    """An indicator with no objective is telemetry nobody is accountable for."""
    from reliability_slo import SLI_REGISTRY

    assert {objective.sli for objective in DEFAULT_OBJECTIVES} == set(SLI_REGISTRY)


def test_cli_status_exits_non_zero_when_remediation_is_required(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CI and agents branch on the exit code, so it must carry the phase decision."""
    ledger = tmp_path / "slo_history.json"
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report([_resource(health="CRITICAL", complexity_violations=["f: M=12"])])), encoding="utf-8")

    assert main(["slo", "record", str(report), "--history", str(ledger)]) == 0
    capsys.readouterr()
    assert main(["slo", "status", "--history", str(ledger)]) == 1
    assert "PHASE_1_REACTIVE_REMEDIATION" in capsys.readouterr().out


def test_small_sample_reports_insufficient_data_rather_than_freezing_the_loop() -> None:
    """A ratio over a handful of events is noise, and acting on noise is the twitchiness to remove."""
    objective = ServiceLevelObjective("toil_containment", target=0.50, window_iterations=20, rationale="r")
    state = evaluate_objective(objective, _history("toil_containment", [(0, 1)]))
    assert (state.status, state.observed) == (BudgetStatus.INSUFFICIENT_DATA, 0.0)
    assert decide_phase([state]).phase is LoopPhase.PROACTIVE_ELEVATION


def test_zero_budget_objective_overrides_the_sample_floor() -> None:
    """One resource violating an invariant is a breach, however few were measured."""
    objective = next(o for o in DEFAULT_OBJECTIVES if o.sli == "invariant_compliance")
    assert objective.min_valid_events == 1
    state = evaluate_objective(objective, _history("invariant_compliance", [(0, 1)]))
    assert (state.status, decide_phase([state]).phase) == (
        BudgetStatus.EXHAUSTED,
        LoopPhase.REACTIVE_REMEDIATION,
    )


def test_sharded_history_writes_one_file_per_iteration(tmp_path: Path) -> None:
    """One file per iteration is what lets concurrent branches merge without conflict."""
    shards = tmp_path / "iterations"
    for _ in range(3):
        history = record_iteration(_report([_resource()]), shards)

    assert (len(sorted(shards.glob("*.json"))), len(history)) == (3, 3)


def test_sharded_history_is_read_in_chronological_order(tmp_path: Path) -> None:
    """Filename order must be iteration order, or the rolling window slices the wrong end."""
    shards = tmp_path / "iterations"
    for index in range(3):
        report = _report([_resource()])
        report["timestamp"] = f"2026-01-0{index + 1}T00:00:00Z"
        record_iteration(report, shards)

    assert [entry["timestamp"] for entry in load_history(shards)] == [
        "2026-01-01T00:00:00Z",
        "2026-01-02T00:00:00Z",
        "2026-01-03T00:00:00Z",
    ]


def test_sharded_history_prunes_beyond_the_retention_window(tmp_path: Path, monkeypatch) -> None:
    """The ledger is a rolling window, not an archive."""
    import reliability_slo

    monkeypatch.setattr(reliability_slo, "MAX_HISTORY_ITERATIONS", 3)
    shards = tmp_path / "iterations"
    for _ in range(6):
        record_iteration(_report([_resource()]), shards)

    assert len(sorted(shards.glob("*.json"))) == 3


def test_corrupt_shard_is_skipped_rather_than_halting_the_loop(tmp_path: Path) -> None:
    """Telemetry degrades the window; it never blocks the measurement about to be taken."""
    shards = tmp_path / "iterations"
    record_iteration(_report([_resource()]), shards)
    (shards / "99999999T000000Z-00099.json").write_text("{not json", encoding="utf-8")

    assert len(load_history(shards)) == 1


def test_steering_objective_moves_the_phase_without_blocking_release() -> None:
    """"Too much of your backlog is automatable" steers priority; it is not a ship blocker."""
    from reliability_slo import gating_states

    steering = ServiceLevelObjective(
        "toil_containment", target=0.50, window_iterations=10, rationale="r", gating=False
    )
    gating = ServiceLevelObjective("gate_pass_rate", target=0.90, window_iterations=10, rationale="r")
    states = [
        evaluate_objective(steering, _history("toil_containment", [(2, 11)] * 10)),
        evaluate_objective(gating, _history("gate_pass_rate", [(100, 100)] * 10)),
    ]

    assert states[0].status is BudgetStatus.EXHAUSTED
    assert decide_phase(states).phase is LoopPhase.REACTIVE_REMEDIATION
    assert decide_phase(gating_states(states)).phase is not LoopPhase.REACTIVE_REMEDIATION


def test_gating_objective_blocks_release(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A breached gating objective must still fail the build."""
    ledger = tmp_path / "slo_history.json"
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(_report([_resource(health="CRITICAL", complexity_violations=["f: M=12"])])),
        encoding="utf-8",
    )
    main(["slo", "record", str(report), "--history", str(ledger)])
    capsys.readouterr()

    assert main(["slo", "status", "--history", str(ledger)]) == 1
    assert "Release gate: BLOCKED" in capsys.readouterr().out


def test_only_defect_indicators_gate_the_release() -> None:
    """A breach must mean "this must not ship", not "we should work on this next"."""
    gating = {o.sli for o in DEFAULT_OBJECTIVES if o.gating}
    steering = {o.sli for o in DEFAULT_OBJECTIVES if not o.gating}

    assert gating == {"invariant_compliance", "gate_pass_rate"}
    assert steering == {"feedback_latency", "headroom_saturation", "toil_containment"}


def test_near_ceiling_complexity_does_not_block_a_release() -> None:
    """Sitting near the ceiling is Phase 2 elevation work; crossing it is what gates."""
    from reliability_slo import gating_states

    saturation = next(o for o in DEFAULT_OBJECTIVES if o.sli == "headroom_saturation")
    states = [evaluate_objective(saturation, _history("headroom_saturation", [(50, 100)] * 10))]

    assert states[0].status is BudgetStatus.EXHAUSTED
    assert gating_states(states) == []
