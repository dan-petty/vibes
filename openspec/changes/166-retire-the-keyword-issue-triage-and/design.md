# Design

## Decisions

- **Use type/bug as the sole defect gate label in recursive-hardening.yml**: type/bug is declared in labels.yml with description 'Defect, regression, or incorrect runtime behavior'. The roadmap intake already assigns this label. The old labels bug, defect, regression are not declared and nothing in the pipeline assigns them.
- **Use type/docs as the sole follow-up issue label**: The follow-up issue requests an AGENTS.md update, which is a documentation change. type/docs is declared with description 'Documentation additions, guides, manuals, or README updates'.
- **Use type/docs for both issue templates**: Observation reports and artifact submissions are documentation contributions. type/docs is the only declared label that matches. The old labels observation, artifact, curation do not exist in the repository.
- **Delete autonomous-triage.yml entirely rather than re-key it**: The keyword-matching approach adds noise labels to every issue. The roadmap intake already assigns one type/* label. The triage-issue subcommand and classify_content have no other callers.
- **Declare certified-by-sentinel in labels.yml rather than removing it from pr-sentinel.yml**: The label is functional and useful. Label sync only creates and updates, so it is not at risk. It is simply missing from the declared set.

## Rejected alternatives

- **Keep bug, defect, regression as defect gate labels**: None of these labels is declared in .github/labels.yml. The repository has migrated to the type/* scheme. Keeping them means the audit fires only on labels nothing in the pipeline assigns.
- **Use type/bug or type/security as defect gate**: type/security covers vulnerabilities, not general defects. AGENTS.md section 8 rule 6 says 'PRs addressing bugs, defects, or regressions', which maps to type/bug alone.
- **Use hardening and triage-needed for follow-up issues**: Neither label is declared in .github/labels.yml. GitHub does not apply a template label that does not exist. The label sync job would not create them.
- **Use type/chore for follow-up issues**: type/chore is described as 'Dependency upgrades, tooling adjustments, or maintenance'. An AGENTS.md guardrail update is a documentation addition, not a chore.
- **Re-key autonomous-triage.yml to use type/* labels**: The keyword-matching approach is fundamentally noisy. The roadmap intake already assigns one type/* label per issue. The triage-issue subcommand has no other callers. Deleting is simpler and removes the noise.
