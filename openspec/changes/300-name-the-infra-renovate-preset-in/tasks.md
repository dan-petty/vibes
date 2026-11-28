# Tasks

## 1. Name the infra preset in the Renovate texts

Kind: other. Reads: `AGENTS.md`, `.github/renovate.json`. Changes: `AGENTS.md`, `.github/renovate.json`.

- [ ] 1.1 In the Renovate bullet of `AGENTS.md` at line 310 and in the `description` field of `.github/renovate.json` at line 3, name the infra preset `github>dan-petty/infra//renovate/base` in place of the shared preset of devops-cli, keep the sentence that gives the schedule and the groups, and leave the `extends` entry and the `packageRules` descriptions unchanged, and run this check by hand: Run `grep -rn "preset of devops-cli" AGENTS.md .github/renovate.json` and confirm that it prints no line and exits 1; on the unchanged tree it prints 2 lines. Run `grep -n "github>dan-petty/infra//renovate/base" AGENTS.md .github/renovate.json` and confirm that it prints the Renovate bullet of `AGENTS.md` and the `description` line of `.github/renovate.json`; on the unchanged tree it prints the `extends` entry alone. Parse the edited config with the json.tool module of Python and confirm that it prints the parsed config and exits 0. Run `uv run devops ci docs` and confirm that it reports no documentation violation.
- [ ] 1.2 Edit the Renovate bullet of `AGENTS.md` and the `description` of `.github/renovate.json` so that each names the infra preset
  - Verification: `$ uv run devops ci docs` passes

## Criteria

- Group 1: `grep -rn "preset of devops-cli" AGENTS.md .github/renovate.json` finds no line.
- Group 1: `AGENTS.md` and the `description` of `.github/renovate.json` name the infra preset.
