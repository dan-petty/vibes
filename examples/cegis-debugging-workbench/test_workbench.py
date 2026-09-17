"""Unit tests for CEGIS Debugging Workbench."""

from workbench import (
    CANDIDATE_PATCH_CONVERGED_SOURCE,
    CANDIDATE_PATCH_RUNAWAY_LOOP,
    CEGISRunner,
    ConstraintSpec,
    Hypothesis,
    PatchExecutionResult,
    SandboxedPatchEvaluator,
    buggy_manifest_parser,
    candidate_patch_cegis_converged,
    candidate_patch_naive_workaround,
    main,
)


def test_baseline_passes_on_buggy_parser() -> None:
    baseline = [
        ConstraintSpec(
            input_data="app: web\nreplicas: 2",
            expected_output={"app": "web", "replicas": 2},
        )
    ]
    runner = CEGISRunner(baseline)
    passed, failed_test = runner.run_oracle(buggy_manifest_parser)
    assert (passed, failed_test) == (True, None)


def test_counterexample_fails_buggy_parser() -> None:
    baseline = [
        ConstraintSpec(
            input_data="app: web\nreplicas: 2",
            expected_output={"app": "web", "replicas": 2},
        )
    ]
    runner = CEGISRunner(baseline)

    counterexample = ConstraintSpec(
        input_data="app: web\nreplicas: '4'",
        expected_output={"app": "web", "replicas": 4},
        name="quoted_replicas",
    )
    runner.record_counterexample(counterexample)

    passed, failed_test = runner.run_oracle(buggy_manifest_parser)
    assert (passed, failed_test) == (False, counterexample)


def test_naive_workaround_is_rejected_by_oracle() -> None:
    baseline = [
        ConstraintSpec(
            input_data="app: web\nreplicas: 2",
            expected_output={"app": "web", "replicas": 2},
        )
    ]
    runner = CEGISRunner(baseline)

    counterexample = ConstraintSpec(
        input_data="app: web\nreplicas: '4'",
        expected_output={"app": "web", "replicas": 4},
    )
    runner.record_counterexample(counterexample)

    hypo = Hypothesis("wrap in try/except", "ignore error")
    converged = runner.verify_and_converge(candidate_patch_naive_workaround, hypo)
    assert (converged, runner.state.converged) == (False, False)


def test_correct_patch_converges_and_passes_all_constraints() -> None:
    baseline = [
        ConstraintSpec(
            input_data="app: web\nreplicas: 2",
            expected_output={"app": "web", "replicas": 2},
        )
    ]
    runner = CEGISRunner(baseline)

    counterexample = ConstraintSpec(
        input_data="app: web\nreplicas: '4'",
        expected_output={"app": "web", "replicas": 4},
    )
    runner.record_counterexample(counterexample)

    hypo = Hypothesis("strip quotes", "clean before parse")
    converged = runner.verify_and_converge(candidate_patch_cegis_converged, hypo)
    assert (converged, runner.state.converged, runner.state.verified_patches) == (True, True, 1)


def test_sandboxed_patch_evaluator_success() -> None:
    """Verify sandboxed evaluator successfully tests converged candidate source."""
    evaluator = SandboxedPatchEvaluator(timeout_seconds=0.15, force_simulator=True)
    test_case = ConstraintSpec(
        input_data="app: web\nreplicas: '8'",
        expected_output={"app": "web", "replicas": 8},
    )
    result: PatchExecutionResult = evaluator.evaluate_code(
        CANDIDATE_PATCH_CONVERGED_SOURCE,
        "parse_manifest_converged",
        test_case,
    )
    assert (result.success, result.timed_out) == (True, False)


def test_sandboxed_patch_evaluator_timeout_containment() -> None:
    """Verify runaway infinite loop candidate patch is contained safely by bounded timeout."""
    evaluator = SandboxedPatchEvaluator(timeout_seconds=0.1, force_simulator=True)
    test_case = ConstraintSpec(
        input_data="app: web\nreplicas: '8'",
        expected_output={"app": "web", "replicas": 8},
    )
    result: PatchExecutionResult = evaluator.evaluate_code(
        CANDIDATE_PATCH_RUNAWAY_LOOP,
        "parse_manifest_runaway",
        test_case,
    )
    assert (result.success, result.timed_out) == (False, True)


def test_sandboxed_cegis_runner_convergence() -> None:
    """Verify end-to-end CEGIS convergence using isolated sandboxed patch evaluation."""
    baseline = [
        ConstraintSpec(
            input_data="app: web\nreplicas: 2",
            expected_output={"app": "web", "replicas": 2},
        )
    ]
    runner = CEGISRunner(baseline)
    counterexample = ConstraintSpec(
        input_data="app: web\nreplicas: '4'",
        expected_output={"app": "web", "replicas": 4},
    )
    runner.record_counterexample(counterexample)

    hypo = Hypothesis("strip quotes", "clean before parse")
    converged = runner.verify_and_converge_sandboxed(
        CANDIDATE_PATCH_CONVERGED_SOURCE,
        "parse_manifest_converged",
        hypo,
    )
    assert (converged, runner.state.converged, runner.state.verified_patches) == (True, True, 1)


def test_workbench_main_demo(capsys: object) -> None:
    """Verify CEGIS workbench main demonstration runs to completion."""
    main()
    captured = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "CEGIS Debugging Workbench Demo" in captured
    assert "Sandboxed Container / Process Group Evaluation" in captured
    assert "successfully converged" in captured
