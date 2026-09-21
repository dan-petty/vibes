# Sample App: Code Smell Quantifier

A deterministic measurement of structural decay: ten metrics expressed as a **measured value against a cited threshold**. Where an established tool already defines a metric, this reports that tool's number rather than a reimplementation of its formula — `radon` for maintainability index, Halstead volume and cyclomatic complexity, `vulture` for unreferenced code.

---

## Why This Exists

This repository enforces two metrics — cyclomatic complexity and nesting depth — and both are pass/fail gates. A gate answers *may this ship*. It does not answer *where is this getting worse, and by how much*, and a self-improving loop needs the second question answered to know what to work on.

Surveying what established tools measure against what was measured here:

| Metric | Established tool | Present before |
|---|---|---|
| Cyclomatic complexity | `radon cc`, lizard, xenon | yes |
| Nesting depth | lizard, sonar | yes |
| Maintainability index | `radon mi` | no |
| Halstead volume | `radon hal` | no |
| Duplicated blocks | PMD-CPD, jscpd | no |
| Long parameter list | pylint R0913 | no |
| Long function | pylint R0915 | no |
| God class | pylint R0902/R0904 | no |
| Cohesion (LCOM4) | sonar, cohesion | no |
| Import cycles | import-linter, pylint R0401 | no |
| Unreferenced symbols | vulture | no |

Two of twelve.

These were first written by hand, under a since-retired convention that treated zero dependencies as a virtue. Measured against `radon` on the same modules, the hand-written maintainability index ran **18 to 40 points low**: the ranking held, the absolute values did not, and the threshold calibrated to radon's scale was being applied to numbers that were not on it. `radon` and `vulture` are now adopted directly, and what remains here is what no tool provided — clone detection tuned to this corpus, LCOM4, the gating/advisory split, and the loop integration.

---

## Quick Start

```bash
python3 smell_quantifier.py ../../tools ../../examples --fail-on gating
python3 smell_quantifier.py .. --json          # machine-readable, for trending
pytest test_smell_quantifier.py -v
```

`analyze(paths, include_advisory=False)` computes only what gates. The advisory detectors
are **94% of the runtime** on this corpus — vulture alone is roughly eight seconds against
one for everything that gates — so a caller consuming `report.gating` and discarding the
rest should not pay for the rest. The self-improvement loop passes it: 11.84s to 1.31s for
identical gating output.

---

## Gating Versus Advisory

Every detector is sound as a *measurement*. They differ in how confidently a finding translates into an action, and conflating those is how a backlog fills with work nobody owed:

```mermaid
flowchart LR
    classDef gate fill:#b3261e,color:#fff
    classDef advise fill:#f2b705,color:#000
    classDef metric fill:#4527a0,color:#fff

    M["Quantify every module<br/>(MI, Halstead, complexity)"]:::metric --> S{"Finding class"}
    S -->|"Structural and unambiguous"| G["GATING — fails the build<br/>long parameter list, long function,<br/>god class, import cycle"]:::gate
    S -->|"Sound measurement,<br/>judgement-dependent action"| A["ADVISORY — reported, never fails<br/>LCOM4, clones, dead symbols, MI"]:::advise
```

Each advisory smell has a known false-positive mode, stated rather than hidden:

- **LCOM4** flags any facade of independent checkers. A validator exposing six unrelated rules scores 6 and is correctly designed.
- **Duplicated blocks** flags deliberate boilerplate as readily as copied logic.
- **Unreferenced symbols** cannot see reflective access. String constants are counted as references for this reason, and modules discovered by naming convention — pytest files — are skipped entirely, because a detector that recommends deleting the test suite is worse than no detector.
- **Maintainability index** is dominated by module length; a large, well-factored module scores lower than a small tangled one.

---

## Calibration

Thresholds are the published defaults of the tool each detector mirrors, so every number traces to a source rather than to taste. Two required empirical calibration against this corpus:

- **Maintainability index floor: 20.** Radon's normalized scale grades A at 20–100, B at 10–19, C below 10. The commonly cited *65* belongs to the unnormalized SEI scale and is a transcription error; using it would mark almost every module unmaintainable.
- **Clone node-mass floor: 40.** Statement count alone makes six consecutive imports a clone of any other six imports. Measured on this corpus, import windows peak at **27** AST nodes while code windows have a median of **128**, so a 40-node floor excludes every import run and retains 90% of real code windows.

> [!IMPORTANT]
> A detector that cannot state its number is an opinion. Each finding carries the measured value, the threshold, and the excess, so a disagreement is about the threshold rather than about whether the thing is true.

---

## What It Found On Its First Run

Run against this repository, the quantifier immediately reported three things that were real:

1. Two byte-identical functions in the context packer, differing only in a docstring — now a single `_extract_declaration_signature`.
2. `audit_directory`, a compatibility shim left behind when the sentinel gained multi-path support earlier in the same session, referenced by nothing.
3. `LifecycleEventType`, an enum defined, exported, documented nowhere and used nowhere.

It also flagged its own author's `_referenced_names` at nesting depth 6, which the AST sentinel then confirmed.
