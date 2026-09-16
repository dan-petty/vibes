"""Unit tests for CEGIS Debugging Workbench."""

from workbench import (
    CEGISRunner,
    Hypothesis,
    ConstraintSpec,
    buggy_manifest_parser,
    candidate_patch_cegis_converged,
    candidate_patch_naive_workaround,
)


def test_baseline_passes_on_buggy_parser():
    baseline = [
        ConstraintSpec(
            input_data="app: web\nreplicas: 2",
            expected_output={"app": "web", "replicas": 2},
        )
    ]
    runner = CEGISRunner(baseline)
    passed, failed_test = runner.run_oracle(buggy_manifest_parser)
    assert passed is True
    assert failed_test is None


def test_counterexample_fails_buggy_parser():
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
    assert passed is False
    assert failed_test == counterexample


def test_naive_workaround_is_rejected_by_oracle():
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
    assert converged is False
    assert runner.state.converged is False


def test_correct_patch_converges_and_passes_all_constraints():
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
    assert converged is True
    assert runner.state.converged is True
    assert runner.state.verified_patches == 1
