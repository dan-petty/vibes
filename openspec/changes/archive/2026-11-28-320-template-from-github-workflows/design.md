# Design

## Decisions

- **dan-petty/github-workflows is the source of the template and of the reusable workflows.** The owner chose a new public repository for them (dan-petty/infra#217).
- **The answer `extra_labels` holds the two labels that the workflows of this repository use.** `pr-sentinel.yml` applies `certified-by-sentinel`, and `recursive-hardening.yml` gates on `needs-guardrail`. The template writes them into `.github/labels.yml`, so a copier update has nothing to merge.
- **The other labels of the old file are not answers.** The workflow labels come from devops-cli, and no workflow of this repository applies `budget/borrowed`, `release`, `status/triage` or `status/ready-to-merge`. They stay on GitHub.
- **Merge after dan-petty/infra#235.** Its Renovate presets leave the pin of the template caller to the copier update.

## Rejected alternatives

- **Stay on the template of infra v0.25.2.** After dan-petty/infra#235, Renovate proposes no update of infra, and a copier update to a later release of infra writes the root files of infra into the repository (dan-petty/infra#235, renovate/README.md, "Move to dan-petty/github-workflows").
- **Re-adopt at dan-petty/github-workflows v0.1.0.** That release has no answer for a label that is not a scope, so the recopy deletes `certified-by-sentinel` and `needs-guardrail` from `.github/labels.yml`.
