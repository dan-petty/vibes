# Proposal

## Why

Issue #168 requires every headline metric to be reproducible or labelled illustrative. It also requires the leaderboard text to match the tool.

## What Changes

- Remove the duplicated 34.2% to 0.0% figure from observations 27, 37 and 46.
- Remove every repeat of the 34.2% figure from README.md, observations/README.md, examples/ast-crdt-collaborative-editor/README.md, patterns/ast-crdts-for-real-time-agent-collaboration.md and docs/ROADMAP.md.
- Label observation 21's 59.6% token figure as illustrative output from sample data in the observation and in observations/README.md.
- Remove the unsourced 85.6% and 100.8% figures from observation 25 and README.md.
- Remove the unsourced 18.2% to 0.0% figure from observation 38.
- Reword the leaderboard text in docs/ROADMAP.md to say the tool scores supplied or sample run records and has no dashboard or GitHub Pages site.
- Add a metric provenance rule to docs/CURATION_GUIDELINES.md.
- Record the documentation contract as the new observation-metric-provenance capability.
- The change fits one pull request.

## Capabilities

### New Capabilities

- `observation-metric-provenance`: Headline metrics name a reproducing command or an illustrative label, One measurement backs one before and after figure, The leaderboard description matches the tool

### Modified Capabilities

None.

## Impact

- Documents: `observations/systems/27-agentic-project-self-documentation-and-the-phantom-architecture-trap.md`, `observations/systems/37-real-time-ast-crdts-and-concurrent-multi-agent-swarms.md`, `observations/systems/46-closed-loop-pr-review-thread-synchronization-and-atomic-resolution.md`, `README.md`, `observations/README.md`, `examples/ast-crdt-collaborative-editor/README.md`, `patterns/ast-crdts-for-real-time-agent-collaboration.md`, `docs/ROADMAP.md`, `observations/systems/21-hierarchical-subagent-slot-offloading-and-epistemic-loss.md`, `observations/systems/25-multi-model-capability-boundaries-and-dynamic-routing-oracles.md`, `observations/systems/38-graph-ranked-ast-context-optimization-and-personalized-pagerank.md`, `docs/CURATION_GUIDELINES.md`

## Feasibility

- The inputs show the cited figures and repeats in `observations/systems/27-agentic-project-self-documentation-and-the-phantom-architecture-trap.md`, `observations/systems/37-real-time-ast-crdts-and-concurrent-multi-agent-swarms.md`, `observations/systems/46-closed-loop-pr-review-thread-synchronization-and-atomic-resolution.md`, `observations/systems/21-hierarchical-subagent-slot-offloading-and-epistemic-loss.md`, `observations/systems/25-multi-model-capability-boundaries-and-dynamic-routing-oracles.md`, `observations/systems/38-graph-ranked-ast-context-optimization-and-personalized-pagerank.md`, `README.md`, `observations/README.md`, `examples/ast-crdt-collaborative-editor/README.md`, `patterns/ast-crdts-for-real-time-agent-collaboration.md` and `docs/ROADMAP.md`.
- The inputs show no harness for the 34.2% figure: `examples/ast-crdt-collaborative-editor/` has no benchmark runner and `tools/pr_thread_sync.py` is a thread synchronizer.
- The inputs show `tools/subagent_orchestrator.py` computes the 59.6% figure from `execute_mock_demo()` at lines 329-421.
- The inputs show `tools/model_leaderboard.py` scores supplied JSON or `generate_sample_benchmark_runs()` at lines 573-606 and calls no model.
- First checks: `python tools/docs_validator.py --strict`; `grep -rn '34.2' observations/ README.md patterns/ docs/ROADMAP.md examples/`; `grep -rn '59.6' observations/`; `grep -rn '85.6' observations/ README.md`; `grep -rn '18.2' observations/`; `grep -n -i 'dashboard\|GitHub Pages' docs/ROADMAP.md`.

## Existing Tool Considered

GNU grep and the repository's `tools/docs_validator.py` cover the search and validation needs. The change adds no code.
