## Purpose
Defines the declared label vocabulary, workflow label contracts, and issue template label assignments for the repository.

## ADDED Requirements

### Requirement: All applied labels are declared
Every label applied by a workflow, issue template, or tool SHALL be declared in .github/labels.yml.

#### Scenario: Workflow label is declared
- **WHEN** a test parses all workflow YAML files and extracts label names from addLabels or labels fields
- **THEN** every extracted name appears in the parsed .github/labels.yml

#### Scenario: Template label is declared
- **WHEN** a test parses all issue template frontmatter and extracts label names
- **THEN** every extracted name appears in the parsed .github/labels.yml

### Requirement: Hardening audit gates on type/bug
The recursive-hardening workflow SHALL treat a PR labelled type/bug as a defect fix requiring AGENTS.md hardening.

#### Scenario: type/bug triggers defect classification
- **WHEN** a test parses recursive-hardening.yml and extracts the defect label list
- **THEN** type/bug is in the list

#### Scenario: Undeclared labels are not in defect list
- **WHEN** a test parses recursive-hardening.yml and extracts the defect label list
- **THEN** bug, defect, and regression are not in the list

### Requirement: Follow-up issue uses declared label
The recursive-hardening workflow SHALL open follow-up issues with only labels declared in .github/labels.yml.

#### Scenario: Follow-up labels are declared
- **WHEN** a test parses recursive-hardening.yml and extracts the labels array from the issue creation script
- **THEN** every label in the array appears in the parsed .github/labels.yml

### Requirement: No keyword triage workflow
The repository SHALL NOT contain a workflow that applies labels based on keyword matching of issue title or body.

#### Scenario: autonomous-triage.yml is absent
- **WHEN** a test checks for the existence of .github/workflows/autonomous-triage.yml
- **THEN** the file does not exist

### Requirement: Issue templates use declared labels
Issue templates SHALL reference only labels declared in .github/labels.yml.

#### Scenario: Observation template labels are declared
- **WHEN** a test reads the frontmatter of observation_report.md
- **THEN** the labels list contains only names present in .github/labels.yml

#### Scenario: Artifact template labels are declared
- **WHEN** a test reads the frontmatter of artifact_submission.md
- **THEN** the labels list contains only names present in .github/labels.yml

### Requirement: certified-by-sentinel is declared
The label certified-by-sentinel SHALL be declared in .github/labels.yml.

#### Scenario: Label entry exists
- **WHEN** a test parses .github/labels.yml and collects all label names
- **THEN** certified-by-sentinel is in the set
