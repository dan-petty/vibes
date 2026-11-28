# Design

## Decisions

- **Name the infra preset `github>dan-petty/infra//renovate/base` in the two texts that name a devops-cli preset, and leave the `extends` entry alone.**: `.github/renovate.json:5:"github>dan-petty/infra//renovate/base"` already holds the infra preset, and the issue names only the two prose texts.
- **Keep the `packageRules` descriptions that say `the shared preset`.**: Those lines name no repository: `.github/renovate.json:27:The shared preset puts the hooks in a different group.` and `.github/renovate.json:33:(shared preset). The Python image gets its own branch` are the whole of them, and one preset still fills `extends`.
- **Set `skip_specs` to true and carry no spec delta.**: The change edits two documents and no code, and `openspec/config.yaml:34:has no spec delta and sets skip_specs: true in its .openspec.yaml. Say why in What` is the rule of this repository for a change that alters no behavior a consumer observes.
- **Give the group the two grep commands of the acceptance criteria as its `process`, a repeatable manual check, in place of a `verify` command.**: `tools/doc_rules_polyglot.py:36:POLYGLOT_EXTENSIONS: Final[tuple[str, ...]] = (` reads `.md`, `.markdown`, `.html`, `.htm`, `.rst`, `.adoc`, `.asciidoc` and `.txt` only, so `uv run devops ci docs` never opens `.github/renovate.json`, and `search_code` for `renovate.json` found no workflow, hook or test that reads it.

## Rejected alternatives

- **Rewrite the `packageRules` descriptions that say `the shared preset` so that each names the infra preset.**: `search_code` for `shared preset` returns `.github/renovate.json:27:The shared preset puts the hooks in a different group.` and `.github/renovate.json:33:(shared preset). The Python image gets its own branch`, and neither line names a repository, so the issue's two texts are the whole defect.
- **Create an OpenSpec capability whose scenario asserts the two texts.**: The `capabilities` input holds no spec, and `openspec/config.yaml:34:has no spec delta and sets skip_specs: true in its .openspec.yaml. Say why in What` withholds a spec delta from a change that keeps behavior.
- **Change the `extends` entry of `.github/renovate.json` to point at the infra preset.**: `.github/renovate.json:5:"github>dan-petty/infra//renovate/base"` already holds the infra preset, so the edit would change nothing.
- **Verify the group with `uv run devops ci docs` alone.**: The `verify_form` finding of the review: the documentation validator reads the extensions of `tools/doc_rules_polyglot.py:36:POLYGLOT_EXTENSIONS: Final[tuple[str, ...]] = (` — `.md`, `.markdown`, `.html`, `.htm`, `.rst`, `.adoc`, `.asciidoc` and `.txt` — so it never opens `.github/renovate.json` and passes on the unchanged tree.
- **Give the group's check as one `! git grep` search for the stale phrase.**: That form exits 0 only when it finds nothing, so it proves the removal alone; the second acceptance criterion asks that `AGENTS.md` and the `description` of `.github/renovate.json` name the infra preset, and `search_code` for `preset of devops-cli` counts 2 lines in 2 files, so the check carries both greps of the criteria instead.
