# Artifact & Observation Curation Guidelines

This document outlines the acceptance criteria, sanitization requirements, and submission processes for contributing to the `vibes` repository.

---

## 1. Guiding Principles

Every artifact or observation included in `vibes` must satisfy three core tenets:
1. **Empirical Grounding**: Derived from real-world engineering workflows, benchmarks, or autonomous agent runs—not hypothetical or synthesized toy scenarios.
2. **Pedagogical Clarity**: Clearly demonstrates an agentic mechanism, failure mode, architectural invariant, or optimization strategy.
3. **Zero-Trust Egress Sanitization**: Completely free of private keys, internal network topology, homelab hostnames, or confidential code.

---

## 2. Sanitization Checklist (Mandatory Pre-Commit Verification)

Before committing any document, prompt, test log, or schema to `vibes`, verify against this checklist:

- [ ] **No Secrets or Credentials**: No API keys, passwords, bearer tokens, or JWTs.
- [ ] **No Private RFC 1918 IPs**: Replace all internal IPs (`10.x.x.x`, `172.16-31.x.x`, `192.168.x.x`) with RFC 5737 documentation blocks:
  - `192.0.2.0/24` (TEST-NET-1)
  - `198.51.100.0/24` (TEST-NET-2)
  - `203.0.113.0/24` (TEST-NET-3)
  - or loopback `127.0.0.1` / `localhost`
- [ ] **Standardized Hostnames**: Standardize all mock or dummy hostnames strictly to `example.com` (e.g. `http://example.com/health`). Do NOT use subdomains (e.g. avoid `api.example.com` or `vault.example.com`). Use abstract role labels (`<worker-node>`, `<storage-cluster>`) for infrastructure roles.
- [ ] **Abstract Local Paths**: Replace concrete developer paths (`/home/dan/...`, `C:\Users\dan\...`) with generic path structures (`/home/user/...` or `~/.config/...`).
- [ ] **Bounded Truncation on Logs**: Truncate raw logs and exception traces to eliminate unneeded noise and avoid token bloat.

---

## 3. Contribution Categories & Submissions

### 3.1 Submitting an Observation Report
- **Directory**: `observations/<project-name>/<number>-<topic-slug>.md`
- **Template**: Use the template provided in [`.github/ISSUE_TEMPLATE/observation_report.md`](../.github/ISSUE_TEMPLATE/observation_report.md).
- **Structure**:
  1. Executive Context & Baseline
  2. The Observed Phenomenon
  3. The Underlying Failure Mode or Catalyst
  4. Remediation & Architectural Pattern
  5. Verifiable Impact & Key Takeaways

### 3.2 Submitting an Operational Pattern
- **Directory**: `patterns/<pattern-slug>.md`
- **Content Requirements**: Must include problem context, step-by-step mechanics, visual diagram (Mermaid), concrete implementation snippet, and cross-references.

### 3.3 Submitting an Artifact
- **Directory**: `artifacts/<type>/<artifact-slug>.<ext>`
- **Supported Formats**:
  - Prompts: Markdown with YAML frontmatter or XML prompt tags.
  - Schemas: Valid JSON Schema (Draft-07 or 2020-12) or Pydantic models.
  - Task Harnesses: Markdown task templates with state machine annotations.
  - Diffs & Logs: Sanitized patch files or trace waterfall screenshots.

---

## 4. Review & Quality Gates

All pull requests to `vibes` undergo automated and manual review:
- **Link Integrity**: All relative links must resolve correctly.
- **Markdown Formatting**: Adherence to standard GitHub Flavored Markdown.
- **Sanitization Audit**: Automated regex check for IP addresses, secret patterns, and non-standard domain names.
