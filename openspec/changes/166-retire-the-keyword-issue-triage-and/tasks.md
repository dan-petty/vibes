# Tasks

## 1. Delete autonomous-triage.yml and remove triage-issue code

Reads: none. Changes: `.github/workflows/autonomous-triage.yml`, `tools/project_tooling.py`, `tests/test_project_tooling.py`.

- [ ] 1.1 Write a test that asserts classify_content and TAXONOMY_PATTERNS are not attributes of tools.project_tooling, and that .github/workflows/autonomous-triage.yml does not exist on disk, and verify that `python -c "import tools.project_tooling as m; assert not hasattr(m, 'classify_content'); assert not hasattr(m, 'TAXONOMY_PATTERNS')" && test ! -f .github/workflows/autonomous-triage.yml && pytest tests/test_project_tooling.py -q` fails on the base
- [ ] 1.2 Delete .github/workflows/autonomous-triage.yml. Remove TAXONOMY_PATTERNS, classify_content, triage_issue_content, and the triage-issue subcommand from tools/project_tooling.py. Remove all tests referencing these symbols from tests/test_project_tooling.py, and verify that `python -c "import tools.project_tooling as m; assert not hasattr(m, 'classify_content'); assert not hasattr(m, 'TAXONOMY_PATTERNS')" && test ! -f .github/workflows/autonomous-triage.yml && pytest tests/test_project_tooling.py -q` passes
- [ ] 1.3 Change no document, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## 2. Re-key recursive-hardening.yml on type/bug

Reads: none. Changes: `.github/workflows/recursive-hardening.yml`, `tests/test_workflow_contracts.py`.

- [ ] 2.1 Write a test in tests/test_workflow_contracts.py that parses recursive-hardening.yml, extracts the defect label list, and asserts type/bug is present and bug, defect, regression are absent. Also assert the follow-up issue labels array contains only type/docs, and verify that `pytest tests/test_workflow_contracts.py -v -k "type_bug or defect"` fails on the base
- [ ] 2.2 In recursive-hardening.yml, change the defect label check from ['bug', 'defect', 'regression'] to ['type/bug']. Change the follow-up issue labels from ['hardening', 'triage-needed'] to ['type/docs'], and verify that `pytest tests/test_workflow_contracts.py -v -k "type_bug or defect"` passes
- [ ] 2.3 Change no document, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## 3. Update issue templates and declare certified-by-sentinel

Reads: none. Changes: `.github/ISSUE_TEMPLATE/observation_report.md`, `.github/ISSUE_TEMPLATE/artifact_submission.md`, `.github/labels.yml`.

- [ ] 3.1 Write a test that reads both issue template frontmatter and asserts the labels field is ['type/docs']. Write a test that parses labels.yml and asserts certified-by-sentinel is in the label name set, and verify that `grep 'labels:' .github/ISSUE_TEMPLATE/observation_report.md .github/ISSUE_TEMPLATE/artifact_submission.md && grep 'certified-by-sentinel' .github/labels.yml` fails on the base
- [ ] 3.2 Set the labels: field in observation_report.md and artifact_submission.md to ["type/docs"]. Add a certified-by-sentinel entry to .github/labels.yml with a color and description, and verify that `grep 'labels:' .github/ISSUE_TEMPLATE/observation_report.md .github/ISSUE_TEMPLATE/artifact_submission.md && grep 'certified-by-sentinel' .github/labels.yml` passes
- [ ] 3.3 Change no document, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## 4. Remove documentation references to retired triage

Reads: none. Changes: `README.md`, `AGENTS.md`, `CONTRIBUTING.md`, `.github/workflows/pr-sentinel.yml`, `tests/test_workflow_contracts.py`.

- [ ] 4.1 Write a test that greps for triage-issue, autonomous-triage, triage-needed, TAXONOMY_PATTERNS, and classify_content across the repository and asserts no matches remain outside CHANGELOG.md, and verify that `grep -rn 'triage-issue\|autonomous-triage\|triage-needed\|TAXONOMY_PATTERNS\|classify_content' --include='*.md' --include='*.yml' --include='*.py' . | grep -v CHANGELOG.md | grep -v '.git/'` fails on the base
- [ ] 4.2 Remove references to autonomous-triage.yml, triage-issue, and the triage workflow from README.md, AGENTS.md, CONTRIBUTING.md, and the comment in pr-sentinel.yml. Update the AGENTS.md flowchart to remove the triage step. Remove AGENTS.md section 8 rule 1. Remove the triage-issue comment in tests/test_workflow_contracts.py, and verify that `grep -rn 'triage-issue\|autonomous-triage\|triage-needed\|TAXONOMY_PATTERNS\|classify_content' --include='*.md' --include='*.yml' --include='*.py' . | grep -v CHANGELOG.md | grep -v '.git/'` passes
- [ ] 4.3 Update README.md, AGENTS.md, CONTRIBUTING.md, and pr-sentinel.yml to remove all references to the retired triage workflow and subcommand, run `uv run devops docs generate --sync-readme`, and verify that `uv run devops ci docs` passes

## Workflow follow-up

- Pending a person: review the diff in tuicr (tuicr -r <base>..HEAD).
