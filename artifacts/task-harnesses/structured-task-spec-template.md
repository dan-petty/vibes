# Artifact: Structured Task Specification Template

> **Artifact Type**: Task Tracking Specification Template  
> **Source Project**: `devops-cli` (`docs/agent/tasks/task-template.md`)  
> **Purpose**: Standardized schema for anchoring autonomous agent sessions into persistent task state.

---

````markdown
# Task: [Issue Number] - [Task Title]

- **Status**: [Todo | In Progress | Review | Done]
- **Milestone**: [e.g. v0.2.19]
- **Branch**: [e.g. feature/issue-123-slug]
- **Pull Request**: [e.g. #124]
- **Assignee**: [AI Agent / Human Lead]

---

## 1. Problem Statement & Context
A concise statement of the problem, background, and business/engineering justification.

---

## 2. Acceptance Criteria
- [ ] Criterion 1: Specific observable behavior
- [ ] Criterion 2: Invariant compliance (Complexity <= 10, Nesting <= 5)
- [ ] Criterion 3: Unit test coverage >= 90.0%
- [ ] Criterion 4: Zero deprecation warnings

---

## 3. Technical Design & Architecture
- Key abstractions, models, and interfaces to introduce or modify.
- External dependencies or tools involved.

---

## 4. Implementation Steps (TDD Protocol)
- [ ] **Step 1: Test Specification**: Author failing unit test in `tests/test_<submodule>.py`.
- [ ] **Step 2: Source Implementation**: Author minimal code in `src/`.
- [ ] **Step 3: Verification**: Run `uv run pytest` and verify green exit code.
- [ ] **Step 4: Quality Gate Certification**: Run full CI suite (`uv run devops ci`).

---

## 5. Verification Log & Artifacts
```bash
# Record terminal verification runs here:
uv run pytest tests/test_example.py -v
# Output summary: 14 passed in 0.42s
```
````
