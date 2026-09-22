# Landscape Survey

Every other work generator in this repository looks inward. The workbench reports decay,
the sentinel reports invariant breaches, the quantifier reports smells, and the roadmap
ingester reports what we said we would build. None of them can notice that somebody else
already solved a problem better, or that a capability treated here as finished is two
features behind the field.

[`tools/landscape_survey.py`](../../tools/landscape_survey.py) closes that loop: it scores
the maturity of comparable projects, compares features against ours, and turns the
differences into roadmap deliverables the existing prioritizer already understands.

## Why the manifest has the shape it does

A survey is a work generator, and a work generator that guesses manufactures a backlog out
of its own ignorance — the failure recorded in
[Observation 13](../../observations/systems/13-verify-the-finding-before-you-fix-it.md).
Three rules keep it honest:

- **A feature claim cannot exist without its evidence.** In
  [`capabilities.yaml`](./capabilities.yaml) a capability is asserted by adding it under an
  alternative's `has:` key, and the *value* of that key is the citation. There is no syntax
  for an uncited claim.
- **Unknown is not absent.** A feature listed in neither `has:` nor `lacks:` renders as `?`.
  "We did not check" and "it is not there" are different claims, and gap detection reads
  `has:` alone — so an unknown can never become work.
- **Facts and judgements are stored apart.** Maturity comes from the GitHub API and lives in
  [`snapshot.json`](./snapshot.json). Feature comparison is human-curated. The tool never
  infers a feature from a README keyword.

The structure forces a citation; it cannot check that the citation *supports* the claim.
Writing this manifest, the first draft cited "Wraps radon; Python only" as evidence that a
tool was multi-language — the evidence said the opposite. That is why every cell's citation
is printed in the report and carried onto the roadmap item: so a reader can overturn it.

## Maturity scoring

Five signals, weighted, scored against the moment the facts were fetched rather than today
— so re-rendering a report without refreshing produces a byte-identical file, and a diff
means the world moved rather than the calendar.

| Signal | Weight | What it reads |
|---|---:|---|
| `recent_activity` | 0.30 | Days since the last push, decaying to zero at two years |
| `release_discipline` | 0.25 | Whether versions are published, and how recently |
| `governance` | 0.20 | A license, and not archived |
| `longevity` | 0.15 | Age, saturating at five years |
| `adoption` | 0.10 | Stars, on a log scale |

**Adoption is deliberately the smallest weight.** Stars measure how many people heard of a
project; they correlate with maturity loosely and lag it by years. Release discipline and
recent activity are what predict whether a dependency will still be maintained when it
breaks.

`release_discipline` counts tags when a project publishes no GitHub Release objects.
Counting Releases alone measures use of one optional GitHub feature: `markdownlint` has 80
tags and zero Releases, and was being docked a quarter of its score for a publishing
preference until the fallback was added. The report prints which source was used.

## Usage

```bash
python3 tools/landscape_survey.py refresh                 # fetch facts (network, via gh)
python3 tools/landscape_survey.py report --out docs/landscape/SURVEY.md
python3 tools/landscape_survey.py gaps                    # or --json for the loop
python3 tools/landscape_survey.py roadmap --top 4         # dry run
python3 tools/landscape_survey.py roadmap --top 4 --write # apply
python3 tools/landscape_survey.py discover "python ast complexity linter"
```

`refresh` is the only command that needs the network. Everything else reads the committed
snapshot, so CI stays offline and deterministic.

`roadmap` is a dry run by default — writing to a human artefact is opt-in. Insertion is
idempotent, keyed on the `landscape-gap:<capability>/<feature>` marker written into each
item rather than on its title, so re-wording a deliverable by hand does not make the next
survey add it again.

`discover` returns search results for a human to curate. Search ranking is not evidence of
comparability, and a survey that adopted its own search results would be citing itself.

## What the tool deliberately does not do

- **It does not invent value or effort.** A survey can establish that a capability exists
  elsewhere; it cannot establish what that capability is worth here. Emitted items carry no
  prioritization matrix row, so the ingester ranks them on defaults and says so.
- **It does not close items.** Only the roadmap decides what shipped.
- **It does not add alternatives to the manifest.** `discover` proposes; a human curates.

## Files

```text
├── README.md           # This file: method, scoring, and what the tool refuses to do
├── capabilities.yaml   # Curated: our capabilities and cited third-party comparisons
├── snapshot.json       # Generated by `refresh`: GitHub facts, committed so CI is offline
└── SURVEY.md           # Generated by `report`: maturity ranking, feature matrices, gaps
```
