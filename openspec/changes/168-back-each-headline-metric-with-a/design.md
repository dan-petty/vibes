# Design

## Decisions

- **Delete the 34.2% to 0.0% figure from observations 27, 37 and 46 and from every repeat.**: The inputs show no harness or data file that produces the figure, and three unrelated studies report the same before and after value.
- **Label observation 21's 59.6% figure as illustrative sample output.**: The inputs show the figure comes from the hard-coded `execute_mock_demo()` run in `tools/subagent_orchestrator.py`.
- **Delete the 85.6%, 100.8% and 18.2% figures.**: The inputs show no command or data source that produces these figures.
- **Reword the leaderboard text to describe supplied or sample run records.**: The inputs show `tools/model_leaderboard.py` calls no model and the repository has no Pages site.
- **Add a metric provenance rule to the curation guidelines.**: The rule prevents a new headline metric from entering the repository without a command or an illustrative label.
- **State the documentation contract as the new observation-metric-provenance capability.**: A reader observes the figures and the leaderboard text, so the corrected contract is written as requirements with offline scenarios.

## Rejected alternatives

- **Add a new benchmark harness for the 34.2% figure.**: The inputs show no harness in `examples/ast-crdt-collaborative-editor/`, and `tools/pr_thread_sync.py` is not a benchmark.
- **Label every figure as illustrative.**: The inputs show no source for the 34.2%, 85.6%, 100.8% or 18.2% figures, and `docs/CURATION_GUIDELINES.md` requires empirical grounding.
- **Keep the leaderboard dashboard claim and build a Pages site.**: The inputs show no Pages site, and a dashboard is a separate feature.
- **Change `tools/model_leaderboard.py` to call models.**: Issue #168 is a documentation accuracy change, and the tool scores supplied or sample run records.
