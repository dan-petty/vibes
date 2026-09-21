# Reliability Ledger

This directory is the committed error budget history for the `vibes` self-improvement loop. Each file in [`iterations/`](./iterations/) records the service level indicators measured during one iteration of the Scan → Run → Review → Feedback → Iterate cycle, as **good events over valid events**:

```json
{
  "timestamp": "2026-09-21T17:08:39Z",
  "measurements": {
    "gate_pass_rate": [132, 132],
    "invariant_compliance": [132, 132],
    "feedback_latency": [110, 110],
    "headroom_saturation": [131, 132],
    "toil_containment": [0, 1]
  }
}
```

---

## Why It Is Committed, and Why It Is Sharded

An error budget is measured over a rolling window of iterations. A ledger that lives only in gitignored `.data/` starts empty in every clone and every CI run, so the window never matures, every objective reports `INSUFFICIENT_DATA`, and the policy quietly degrades to judging the current iteration alone — which is the binary health check it was built to replace, wearing more machinery.

**One file per iteration, never one shared array.** Concurrent branches each add a distinct path, so git merges them without conflict. A single appended ledger would conflict on every parallel iteration — the failure mode this repository already decomposed its task tracking to avoid.

The retention window is bounded: recording prunes shards beyond the most recent 100, so the directory stays a rolling window rather than an archive.

> [!NOTE]
> Every value here is an aggregate count. There are no file paths, hostnames, or identifiers of any kind, which is what makes this ledger safe to commit under the zero-leakage mandate in `AGENTS.md` §2.

---

## Usage

```bash
# Record an iteration into the committed ledger, in the same commit as the work it measures.
python3 tools/resource_iteration_workbench.py --json > .data/iteration_report.json
python3 tools/reliability_slo.py record .data/iteration_report.json --history docs/reliability/iterations

# Read the budget state and the phase the loop should occupy.
python3 tools/reliability_slo.py status --history docs/reliability/iterations
```

See [`patterns/error-budget-driven-feedback-inversion.md`](../../patterns/error-budget-driven-feedback-inversion.md) for the reasoning, and `AGENTS.md` §11 for the phase policy agents must follow.
