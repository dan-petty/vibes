# Sample App: CEGIS Debugging Workbench

An executable demonstration of **Counterexample-Guided Inductive Synthesis (CEGIS)** for autonomous defect remediation.

---

## Why This Exists

When an autonomous AI agent encounters a bug, the default behavior is often single-shot trial-and-error:
1. The agent guesses a superficial code modification.
2. It tries applying naive workarounds (e.g. `try/except: pass`) that mask the symptom while breaking the underlying contract.
3. If it fails, the agent wanders aimlessly through solution space.

**CEGIS** replaces unguided guessing with formal constraint accumulation:
- **Hypothesis Formulation**: State a falsifiable theory explaining the root cause.
- **Counterexample Isolation**: Author an executable test asserting the failure.
- **Oracle Verification**: Evaluate candidate patches against both baseline specifications and accumulated counterexamples.
- **Convergence**: Only declare completion when all constraints are certified green.

---

## Quick Start

### Running the Interactive Simulation
```bash
python3 workbench.py
```

### Running the Test Suite
```bash
pytest test_workbench.py -v
```

---

## Pattern Implementation

```python
from workbench import CEGISRunner, TestCase, Hypothesis

runner = CEGISRunner(baseline_tests)

# Add failing counterexample
runner.record_counterexample(failing_test_case)

# Evaluate candidate patch against full constraint set
is_valid = runner.verify_and_converge(my_patch_fn, hypothesis)
```
