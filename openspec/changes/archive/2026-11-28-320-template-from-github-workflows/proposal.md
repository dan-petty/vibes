# Proposal

## Why

The reusable workflows and the repository template moved from dan-petty/infra to dan-petty/github-workflows (dan-petty/infra#217), and dan-petty/infra#235 turns off the Renovate github-actions and copier managers for each form of the name of infra (#320). This repository rendered the template of infra v0.25.2 and called its workflows. Its `.github/labels.yml` held hand-written labels, so each copier update conflicted, and two of them are labels of this repository that its workflows use: `certified-by-sentinel` and `needs-guardrail`.

## What Changes

- Set `_src_path: https://github.com/dan-petty/github-workflows.git` in `.copier-answers.yml` and run `copier recopy --skip-answered --defaults --overwrite --vcs-ref v0.2.0` with the answer `extra_labels`. `.copier-answers.yml` names `_commit: v0.2.0`.
- The answer `extra_labels` holds `certified-by-sentinel` and `needs-guardrail` with the colors and descriptions of the old file. `.github/labels.yml` holds the template scopes `ci`, `deps` and `docs`, and these two labels. The workflow labels (`type/*`, `priority/*`, `status/*`, `value/*`, `effort/*`, `owner/*`, `release`, `needs-split`, `source/agent`, `budget/borrowed`) come from devops-cli; the labels on GitHub do not change.
- The caller `.github/workflows/pr-title.yml` calls dan-petty/github-workflows at the commit of the tag v0.2.0, `7b87b8c4e7a638fa1f3cd69903e720fdd487728a`, with `# v0.2.0` in the comment. `.github/CODEOWNERS`, `github/repository.yaml` and `openspec/config.yaml` get the text of the template, and the template adds the empty files `openspec/changes/archive/.gitkeep` and `openspec/specs/.gitkeep`.
- AGENTS.md and CONTRIBUTING.md name dan-petty/github-workflows as the template. AGENTS.md says where a label of this repository is declared: the answer `extra_labels`. README.md, `tools/sdlc_project_manager.py`, `patterns/autonomous-sdlc-project-management.md` and two test docstrings say that the `type/` names are workflow labels of devops-cli.
- Copier keeps `.github/renovate.json`, `.pre-commit-config.yaml` and `.devops/project.toml` of the repository (`_skip_if_exists`).
- The change alters no behavior that a consumer observes: it changes a CI workflow caller, label data, settings files and text. It sets `skip_specs: true`.
- The change fits one pull request.

## Feasibility

- The tag v0.2.0 of dan-petty/github-workflows is the commit `7b87b8c4e7a638fa1f3cd69903e720fdd487728a`, and its `copier.yml` has the answer `extra_labels` (dan-petty/github-workflows#5).
- `pr-title.yml` of dan-petty/github-workflows at v0.2.0 runs the same steps as that of dan-petty/infra at v0.25.2. Only names, the checkout folder and the concurrency group differ.
- `copier recopy` and `copier update` of Copier 9.19.1 at v0.2.0 change no file after this change.
- No code reads `.github/labels.yml`: `tools/sdlc_project_manager.py` keys its modifiers on label names in code.

## Existing Tool Considered

Copier (MIT, maintained) renders the template and its answer `extra_labels`, and Renovate (AGPL-3.0, maintained) proposes the next copier update. The change builds no code and deletes no internal code.
