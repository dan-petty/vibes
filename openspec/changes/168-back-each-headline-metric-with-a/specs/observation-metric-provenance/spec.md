## Purpose
Keeps every headline metric in the repository documentation tied to a command that reproduces it or to an explicit illustrative label.

## ADDED Requirements

### Requirement: Headline metrics name a reproducing command or an illustrative label
Every headline metric in an observation, pattern, example README or roadmap SHALL name the repository command and input that produce it, or SHALL carry the label 'Illustrative output from sample data'.

#### Scenario: The token reduction figure names its source
- **WHEN** A reader opens `observations/systems/21-hierarchical-subagent-slot-offloading-and-epistemic-loss.md`
- **THEN** The 59.6% figure sits next to the label 'Illustrative output from sample data' and names `tools/subagent_orchestrator.py`.

#### Scenario: A figure with no source is absent
- **WHEN** A reader searches `observations/systems/38-graph-ranked-ast-context-optimization-and-personalized-pagerank.md` for the 18.2% fracture rate
- **THEN** The document holds no such figure.

### Requirement: One measurement backs one before and after figure
Two observations SHALL NOT report the same before and after figure unless they cite the same measurement.

#### Scenario: The 34.2% figure is gone from the three observations
- **WHEN** A reader searches observations 27, 37 and 46 for the 34.2% figure
- **THEN** No document holds the figure.

### Requirement: The leaderboard description matches the tool
The roadmap SHALL describe `tools/model_leaderboard.py` as scoring supplied or sample run records, and SHALL NOT claim a dashboard or a GitHub Pages site.

#### Scenario: The roadmap names no dashboard
- **WHEN** A reader searches `docs/ROADMAP.md` for 'dashboard' and 'GitHub Pages'
- **THEN** The document holds neither term in the leaderboard description.
