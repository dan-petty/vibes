# Tasks

## 1. Remove the duplicate 34.2% figure from the three observations

Reads: `observations/systems/27-agentic-project-self-documentation-and-the-phantom-architecture-trap.md`, `observations/systems/37-real-time-ast-crdts-and-concurrent-multi-agent-swarms.md`, `observations/systems/46-closed-loop-pr-review-thread-synchronization-and-atomic-resolution.md`. Changes: `observations/systems/27-agentic-project-self-documentation-and-the-phantom-architecture-trap.md`, `observations/systems/37-real-time-ast-crdts-and-concurrent-multi-agent-swarms.md`, `observations/systems/46-closed-loop-pr-review-thread-synchronization-and-atomic-resolution.md`.

- [x] 1.1 Remove the 34.2% to 0.0% row and every sentence that states the figure from the three observations. Keep the surrounding argument, and verify that `! grep -rn '34.2' observations/systems/27-agentic-project-self-documentation-and-the-phantom-architecture-trap.md observations/systems/37-real-time-ast-crdts-and-concurrent-multi-agent-swarms.md observations/systems/46-closed-loop-pr-review-thread-synchronization-and-atomic-resolution.md` passes
- [x] 1.2 `observations/systems/27-agentic-project-self-documentation-and-the-phantom-architecture-trap.md`, `observations/systems/37-real-time-ast-crdts-and-concurrent-multi-agent-swarms.md`, `observations/systems/46-closed-loop-pr-review-thread-synchronization-and-atomic-resolution.md`, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## 2. Remove repeats of the 34.2% figure

Reads: `README.md`, `observations/README.md`, `examples/ast-crdt-collaborative-editor/README.md`, `patterns/ast-crdts-for-real-time-agent-collaboration.md`, `docs/ROADMAP.md`. Changes: `README.md`, `observations/README.md`, `examples/ast-crdt-collaborative-editor/README.md`, `patterns/ast-crdts-for-real-time-agent-collaboration.md`, `docs/ROADMAP.md`.

- [ ] 2.1 Remove the 34.2% to 0.0% figure from each file. Keep the remaining description of the AST-CRDT work, and verify that `! grep -rn '34.2' README.md observations/README.md examples/ast-crdt-collaborative-editor/README.md patterns/ast-crdts-for-real-time-agent-collaboration.md docs/ROADMAP.md` passes
- [ ] 2.2 `README.md`, `observations/README.md`, `examples/ast-crdt-collaborative-editor/README.md`, `patterns/ast-crdts-for-real-time-agent-collaboration.md`, `docs/ROADMAP.md`, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## 3. Label the 59.6% token figure as illustrative

Reads: `observations/systems/21-hierarchical-subagent-slot-offloading-and-epistemic-loss.md`, `observations/README.md`. Changes: `observations/systems/21-hierarchical-subagent-slot-offloading-and-epistemic-loss.md`, `observations/README.md`.

- [ ] 3.1 Add the label 'Illustrative output from sample data' next to the 59.6% figure in the observation and in the index. Name `tools/subagent_orchestrator.py` and `execute_mock_demo()` as the source, and verify that `grep -q '59.6' observations/systems/21-hierarchical-subagent-slot-offloading-and-epistemic-loss.md && grep -q 'Illustrative output from sample data' observations/systems/21-hierarchical-subagent-slot-offloading-and-epistemic-loss.md && grep -q 'Illustrative output from sample data' observations/README.md` passes
- [ ] 3.2 `observations/systems/21-hierarchical-subagent-slot-offloading-and-epistemic-loss.md`, `observations/README.md`, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## 4. Remove the unsourced 85.6% and 100.8% figures

Reads: `observations/systems/25-multi-model-capability-boundaries-and-dynamic-routing-oracles.md`, `README.md`. Changes: `observations/systems/25-multi-model-capability-boundaries-and-dynamic-routing-oracles.md`, `README.md`.

- [ ] 4.1 Remove the 85.6% cost reduction and 100.8% APGR figures from the observation and from the model router description in `README.md`, and verify that `! grep -rn -e '85.6' -e '100.8' observations/systems/25-multi-model-capability-boundaries-and-dynamic-routing-oracles.md README.md` passes
- [ ] 4.2 `observations/systems/25-multi-model-capability-boundaries-and-dynamic-routing-oracles.md`, `README.md`, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## 5. Remove the unsourced 18.2% fracture figure

Reads: `observations/systems/38-graph-ranked-ast-context-optimization-and-personalized-pagerank.md`. Changes: `observations/systems/38-graph-ranked-ast-context-optimization-and-personalized-pagerank.md`.

- [ ] 5.1 Remove the 18.2% to 0.0% fracture rate figure from the observation. Keep the description of the graph-ranked context method, and verify that `! grep -n '18.2' observations/systems/38-graph-ranked-ast-context-optimization-and-personalized-pagerank.md` passes
- [ ] 5.2 `observations/systems/38-graph-ranked-ast-context-optimization-and-personalized-pagerank.md`, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## 6. Correct the leaderboard description

Reads: `docs/ROADMAP.md`, `tools/model_leaderboard.py`. Changes: `docs/ROADMAP.md`.

- [ ] 6.1 Replace the leaderboard description with text that says the tool scores supplied run records or built-in sample runs. Remove the public dashboard and GitHub Pages claims, and verify that `! grep -n -i -e 'GitHub Pages' -e 'dashboard' docs/ROADMAP.md` passes
- [ ] 6.2 `docs/ROADMAP.md`, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## 7. Add the metric provenance rule

Reads: `docs/CURATION_GUIDELINES.md`. Changes: `docs/CURATION_GUIDELINES.md`.

- [ ] 7.1 Add a short rule to the curation guidelines: every headline metric names the command and input that produce it, or is labelled illustrative, and verify that `grep -q 'headline metric' docs/CURATION_GUIDELINES.md` passes
- [ ] 7.2 `docs/CURATION_GUIDELINES.md`, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## Workflow follow-up

- Pending a person: review the diff in tuicr (tuicr -r <base>..HEAD).
