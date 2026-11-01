# Proposal

## Why

Issue #166: The repository migrated to type/* labels in .github/labels.yml, but four older mechanisms still reference undeclared labels. The autonomous-triage workflow adds noise labels to every new issue. The hardening audit gates on bug/defect/regression, so it silently stops firing for PRs labelled type/bug. Two issue templates request labels that do not exist. certified-by-sentinel is applied but not declared.

## What Changes

- Delete .github/workflows/autonomous-triage.yml
- Remove triage-issue subcommand, classify_content, triage_issue_content, and TAXONOMY_PATTERNS from tools/project_tooling.py
- Remove their tests from tests/test_project_tooling.py
- Re-key recursive-hardening.yml defect gate on type/bug
- Change recursive-hardening.yml follow-up issue labels to type/docs
- Set labels field of both issue templates to ["type/docs"]
- Declare certified-by-sentinel in .github/labels.yml
- Remove all documentation references to the retired triage workflow
- The change fits one pull request.

## Capabilities

### New Capabilities

- `label-governance`: All applied labels are declared, Hardening audit gates on type/bug, Follow-up issue uses declared label, No keyword triage workflow, Issue templates use declared labels, certified-by-sentinel is declared

### Modified Capabilities

None.

## Impact

- Code: `.github/workflows/autonomous-triage.yml`, `tools/project_tooling.py`, `.github/workflows/recursive-hardening.yml`, `.github/labels.yml`, `.github/workflows/pr-sentinel.yml`
- Documents: `.github/ISSUE_TEMPLATE/observation_report.md`, `.github/ISSUE_TEMPLATE/artifact_submission.md`, `README.md`, `AGENTS.md`, `CONTRIBUTING.md`
- Tests: `tests/test_project_tooling.py`, `tests/test_workflow_contracts.py`

## Feasibility

- The issue premise is confirmed: .github/labels.yml declares type/*, priority/*, status/*, release, needs-split, source/agent, and budget/borrowed, but not bug, defect, regression, hardening, triage-needed, observation, artifact, curation, or certified-by-sentinel.
- autonomous-triage.yml exists and calls tools/project_tooling.py triage-issue, which uses TAXONOMY_PATTERNS (tools/project_tooling.py:25-32) and classify_content (tools/project_tooling.py:86-95).
- recursive-hardening.yml:34 checks ['bug', 'defect', 'regression'] and opens issues with ['hardening', 'triage-needed'].
- observation_report.md requests ["observation", "curation"] and artifact_submission.md requests ["artifact", "curation"].
- pr-sentinel.yml:83 adds certified-by-sentinel, which is absent from labels.yml.
- First checks: `test ! -f .github/workflows/autonomous-triage.yml` confirms deletion; `python -c "import tools.project_tooling as m; assert not hasattr(m, 'classify_content')"` confirms code removal; `pytest tests/test_workflow_contracts.py -v` confirms the type/bug gate test passes; `python tools/docs_validator.py --strict` confirms documentation consistency.

## Existing Tool Considered

The roadmap intake (devops-cli) already assigns one type/* label to each new issue. The change removes the custom classify_content keyword matcher in tools/project_tooling.py, which is the internal code deleted. No new code is built.
