# Pattern: Iterative Resource Refinement Loop (Scan-Run-Review-Iterate)

> **Pattern Class**: Continuous Verification & Self-Improvement
> **Problem**: Open-loop single-shot execution produces changes nobody measured against the repository they landed in
> **Solution**: A closed Scan-Run-Review-Feedback-Iterate cycle emitting prescriptive, ranked next actions
> **Reference Implementation**: [`tools/resource_iteration_workbench.py`](../tools/resource_iteration_workbench.py)

A continuous, metric-driven engineering loop enabling AI agents to iteratively inspect, execute, evaluate, and refine repository resources before committing changes.

---

## 1. Problem Statement

AI coding agents often operate in an open-loop, single-shot execution mode:
1. An agent writes or edits code.
2. It claims the change is complete without measuring whether cyclomatic complexity spiked, tests failed, or private identifiers were leaked.
3. Regressions, unhandled warnings, and architectural drift accumulate until manual human intervention or remote CI breaks.

Without continuous **closed-loop verification**, agent development quickly devolves into unstructured code churn.

---

## 2. Core Mechanics: The 5-Phase Iteration Engine

The **Iterative Resource Refinement Loop** formalizes the agentic development cycle into five disciplined phases:

```mermaid
flowchart LR
    subgraph Phase 1: SCAN
        Scan[Resource Scanner] --> AST[AST Analysis: M <= 10, Depth <= 5]
        Scan --> Sanitize[Zero-Trust Egress Audit]
    end

    subgraph Phase 2: RUN
        AST --> Runner[Resource Runner]
        Sanitize --> Runner
        Runner --> BoundedExec[Bounded Subprocess: pytest / benchmark]
        BoundedExec --> Telemetry[Capture ExitCode, Latency, Output]
    end

    subgraph Phase 3: REVIEW
        Telemetry --> Reviewer[Output Reviewer]
        Reviewer --> Score[Compute Quality Score 0-100]
        Reviewer --> Delta[Compare Against Baseline Deltas]
    end

    subgraph Phase 4: FEEDBACK
        Score --> Advisor[Feedback Analyzer]
        Delta --> Advisor
        Advisor --> Headroom[Refactoring Headroom Analysis]
        Advisor --> Parity[Test Parity & Verification Coverage]
        Advisor --> Quality[Docstrings & Type Annotations]
        Advisor --> Speed[Fast-Feedback Latency Ceilings]
        Advisor --> Praise[Positive Architectural Reinforcement]
    end

    subgraph Phase 5: ITERATE
        Advisor --> Backlog[Autonomous SDLC Backlog Tasks]
        Backlog --> Agent[Agent Dispatches Next Refactoring Cycle]
        Agent --> Scan
    end
```

### 1. Phase 1: SCAN (Static Invariant Baseline)
- Discovers and classifies repository resources (`PYTHON_MODULE`, `TEST_SUITE`, `SAMPLE_APP`, `BENCHMARK`, `MANIFEST`).
- Measures AST McCabe cyclomatic complexity ($M \le 10$) and indentation nesting depth ($\le 5$).
- Checks string literals for private RFC 1918 IPs (`10.x`, `172.16-31.x`, `192.168.x`) or invalid mock subdomains.

### 2. Phase 2: RUN (Bounded Execution Telemetry)
- Executes target verification commands (`pytest`, `benchmarks`, `sentinels`) with bounded timeouts.
- Captures wall-clock duration, exit codes, and standard streams without hanging or leaking zombie processes.

### 3. Phase 3: REVIEW (Output Evaluation & Delta Tracking)
- Parses test pass/fail counts, assertion errors, and deprecation warnings.
- Computes an objective **Quality Score** (0–100):
  - Base: 100 points
  - Deductions: -20 per complexity violation, -25 per failed test, -15 per sanitization leak, -5 per deprecation warning.
- Compares metrics against persistent baseline runs (`.data/iteration_baseline.json`) to track complexity deltas ($\Delta M$) and latency regressions ($\Delta t$).

### 4. Phase 4: FEEDBACK (Meaningful Improvement Analysis)
Rather than going passive when code meets passing thresholds, the feedback engine actively probes for continuous improvement opportunities:
- **Refactoring Headroom**: Warns on functions operating near thresholds ($7 \le M \le 10$ or nesting depth $\ge 4$) to encourage preventive modularization.
- **Test Parity**: Flags non-test modules lacking companion test suites in `tests/test_<stem>.py`.
- **Contract Completeness**: Identifies public functions missing descriptive docstrings or explicit type annotations.
- **Latency Budget**: Flags test suites exceeding fast-feedback targets ($> 2.0$s) to protect agent iteration agility.
- **Positive Reinforcement**: Certifies and highlights exemplary architectural patterns to guide peer agents.

### 5. Phase 5: ITERATE / BACKLOG (Recursive Action Dispatch)
- Ranks identified opportunities by priority (`HIGH`, `MEDIUM`, `LOW`, `INFO`).
- Exports feedback directly to GitHub Projects / SDLC backlog tasks via `--export-backlog`.
- Dispatches the highest priority improvement into the agent's next development cycle.

---

## 3. Implementation Blueprint

```python
from pathlib import Path
from tools.resource_iteration_workbench import ResourceIterationWorkbench

# Initialize workbench pointing to repository root
workbench = ResourceIterationWorkbench(root_dir=Path("."))

# Execute complete Scan -> Run -> Review -> Iterate cycle
report = workbench.run_cycle(execute_tests=True)

# Render ASCII Scorecard & Metrics
print(workbench.render_report(report))

# Automated gating: block push if overall health is not clean
if report.overall_health.value == "CRITICAL":
    raise SystemExit(f"Quality gate blocked: {report.prescriptive_action}")
```

---

## 4. Key Takeaways

1. **Closed-Loop Feedback Anchors LLM Tokens**: Providing agents with immediate, structured metrics (pass rate, complexity, deltas) turns non-deterministic code edits into predictable engineering progress.
2. **Delta Tracking Exposes Regressions**: Comparing current AST metrics against a saved baseline ensures complexity never creeps upward silently.
3. **Prescriptive Guidance Prevents Thrashing**: Clear error localization (exact line numbers and function names) directs agent attention to root causes rather than symptoms.
