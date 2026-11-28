# Proposal

## Why

Two texts of this repository say that `.github/renovate.json` extends the shared preset of devops-cli, and that preset no longer exists (#300). A reader learns the wrong source of the schedule and the groups, so both texts must name the infra preset `github>dan-petty/infra//renovate/base`.

## What Changes

- Rewrite the Renovate bullet of `AGENTS.md` so that it names the infra preset `github>dan-petty/infra//renovate/base` in place of the shared preset of devops-cli.
- Rewrite the top-level `description` of `.github/renovate.json` so that it names the infra preset and keeps the sentence that gives the schedule and the groups.
- Leave the `extends` entry of `.github/renovate.json` unchanged, because it already holds `github>dan-petty/infra//renovate/base`.
- Keep the `packageRules` descriptions that say `the shared preset`, because they name no repository and one preset still fills `extends`.
- Check the group with the two grep commands of the acceptance criteria over both files, because no gate of this repository reads `.github/renovate.json`.
- No spec delta: the change edits two texts and no code, so it alters no behavior that a consumer observes.
- The change fits one pull request.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None.

## Impact

- Code: `.github/renovate.json`
- Documents: `AGENTS.md`

## Feasibility

- The premise holds at the staged commit: `probes` says "The path `.github/renovate.json` is a file of the staging commit." and "The path `AGENTS.md` is a file of the staging commit."
- `search_code` with the text `preset of devops-cli` answered 2 lines in 2 files: `.github/renovate.json:3:The shared preset of devops-cli sets the schedule and the groups` and `AGENTS.md:310:extends the shared preset of devops-cli`.
- `references` for `preset of devops-cli` answered "References to 'preset of devops-cli': files 2, lines 2." with `AGENTS.md` at line 310 and `.github/renovate.json` at line 3, and both files sit in the group's `changes`.
- `read_file` of `.github/renovate.json` lines 1 to 12 showed `.github/renovate.json:5:"github>dan-petty/infra//renovate/base"`, so the `extends` entry already holds the infra preset and the plan leaves it alone.
- `read_file` of `AGENTS.md` lines 300 to 316 showed `AGENTS.md:310:extends the shared preset of devops-cli`, the Renovate bullet that the group rewrites.
- `search_code` for `shared preset` showed `.github/renovate.json:27:The shared preset puts the hooks in a different group.` and `.github/renovate.json:33:(shared preset). The Python image gets its own branch`, which name no repository, so the plan keeps them.
- `read_issue` showed #298 closed and #299 merged, and #299 already set the `extends` entry to `github>dan-petty/infra//renovate/base`, so no open pull request overlaps this change and `dependencies` is empty.
- `search_code` for `renovate.json` gave no hit under `.github/workflows/` and no hit under `tests/`; its hits are the comment `.pre-commit-config.yaml:14:# the ruff floor in pyproject.toml; Renovate updates the two together (.github/renovate.json).`, the bullet `AGENTS.md:310` and the README directory map, so no gate and no test reads the file.
- The documentation validator never opens a `.json` file: `tools/doc_rules_polyglot.py:36:POLYGLOT_EXTENSIONS: Final[tuple[str, ...]] = (` lists `.md`, `.markdown`, `.html`, `.htm`, `.rst`, `.adoc`, `.asciidoc` and `.txt` at lines 37 to 44, and the gate runs it as `.github/workflows/ci.yml:143:python tools/docs_validator.py --strict`.
- The new plan answers the `verify_form` finding: `uv run devops ci docs` alone never opens `.github/renovate.json` and cannot see the phrase, so the group's check is the repeatable manual check of the two grep commands of the acceptance criteria over both files; it prints 2 hits and 1 hit on the unchanged tree and 0 hits and 3 hits after the edit, and `uv run devops ci docs` stays one step of that check for `AGENTS.md`.
- The first check that the implementation runs against the real system is `grep -rn "preset of devops-cli" AGENTS.md .github/renovate.json`, which prints 2 lines before the edit.
- The second check is `grep -n "github>dan-petty/infra//renovate/base" AGENTS.md .github/renovate.json`, which prints the `extends` entry alone before the edit and the `description` line and the Renovate bullet after it.
- The check runs `grep`, `python` and `uv` with `devops`; all three are in the runner image tool list, and none is NOT_IN_RUNNER. The job pod holds the repository checkout at its workspace, runs with no cluster token and a read-only root, and the failure class is a non-zero exit of a grep that still finds the stale phrase or a parse error of the edited config.
- The set is the two texts that the issue names, not its examples: `search_code` counts 2 lines in 2 files and `references` counts 2 files and 2 lines, so both members are removed, because the group changes both files.
- The change edits two texts and no code, so it alters no behavior that a consumer observes; `openspec/config.yaml:34:has no spec delta and sets skip_specs: true in its .openspec.yaml. Say why in What` gives such a change no spec delta, and the `capabilities` input holds no spec to change.
- The plan calls no function whose arguments need a checkout, a client or a network, so no call carries a precondition.

## Existing Tool Considered

Grep finds the stale phrase in the two files and the documentation validator of this repository, run by `uv run devops ci docs`, checks the edited `AGENTS.md`; both are open and maintained. No open tool validates the `description` of a Renovate config offline, so the change writes no code and deletes no internal code.

## Dependencies

None.
