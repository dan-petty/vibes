# Sample App: AST Invariant Sentinel

An executable, zero-dependency Python static analyzer that mechanically enforces architectural invariants on codebases developed by AI agents.

---

## Why This Exists

When AI coding assistants are asked to add features or fix bugs, their path of least resistance is to wrap existing logic in another conditional block or procedural ladder. Without automated mechanical gates, code quickly degrades into high cyclomatic complexity and deep nesting.

The **AST Invariant Sentinel** converts soft architectural guidelines into deterministic verification gates:
- **Cyclomatic Complexity**: Enforces $M \le 10$ across all functions and methods.
- **Nesting Depth**: Enforces indentation depth $\le 5$ levels.
- **Zero-Trust Sanitization**: Flags private RFC 1918 IPs (`192.168.x`, `10.x`) and enforces standardized dummy domains (`example.com` with no subdomains).

---

## Quick Start

### Running the Sentinel
```bash
# One or many targets; files and directories may be mixed freely.
python3 sentinel.py /path/to/python/code
python3 sentinel.py tools examples tests benchmarks

# Use calibrated rule presets or override limits
python3 sentinel.py tools --preset strict
python3 sentinel.py tools --max-complexity 6 --max-depth 3

# Granular rule selection and omission by code or alias
python3 sentinel.py tools --select CC001,ND001 --ignore ZT001

# Inspect all available presets
python3 sentinel.py --list-presets
```

> [!IMPORTANT]
> Every supplied path is audited. Pre-commit hooks with `pass_filenames: true` hand the sentinel
> N staged files per invocation, so an entrypoint that reads only `argv[1]` certifies code it never
> opened. Targets that do not exist raise `TargetIntegrity` rather than passing as a clean audit of
> zero files.

### Running the Tests
```bash
pytest test_sentinel.py -v
```

---

## Selectable Rule Presets

The sentinel provides six calibrated presets out-of-the-box:

| Preset Name | Complexity ($M$) | Max Depth | Active Invariant Rules | Description |
|---|---|---|---|---|
| `standard` | $\le 10$ | $\le 5$ | All 6 rules | Standard baseline invariants (M <= 10, depth <= 5, zero-trust sanitization) |
| `strict` | $\le 6$ | $\le 3$ | All 6 rules | Strict proactive headroom invariants (M <= 6, depth <= 3, zero-trust sanitization) |
| `pedantic` | $\le 4$ | $\le 2$ | All 6 rules | Pedantic ultra-compact invariants for critical concurrency/functional kernels (M <= 4, depth <= 2) |
| `relaxed` | $\le 15$ | $\le 6$ | All 6 rules | Relaxed migration thresholds for legacy codebases (M <= 15, depth <= 6) |
| `security_only` | $\le 999$ | $\le 99$ | Security & Integrity | Zero-trust egress and sanitization audit only; complexity/nesting ignored |
| `structural_only` | $\le 10$ | $\le 5$ | Structural AST & Integrity | Structural AST complexity and nesting depth audit only; sanitization ignored |

---

## Configuration & TOML Support

The sentinel automatically discovers configuration from `pyproject.toml` or `sentinel.toml` in the current or ancestor directories. You can also specify a custom configuration file via `--config path/to/config.toml`.

### Example `pyproject.toml`
```toml
[tool.sentinel]
preset = "strict"
max_complexity = 6
max_depth = 3
select = ["CC001", "ND001", "ZT001"]
ignore = []
extend_select = ["TI001"]
```

### Rule Codes & Aliases

Rules can be referenced by canonical name, rule code, or ergonomic alias:

| Rule Code | Canonical Name | Aliases | Description | Waivable |
|---|---|---|---|---|
| `CC001` | `CyclomaticComplexity` | `complexity`, `c901`, `mccabe` | McCabe cyclomatic complexity ceiling | No |
| `ND001` | `NestingDepth` | `nesting`, `depth` | Maximum statement nesting depth | No |
| `ZT001` | `ZeroTrustSanitization` | `sanitization`, `security`, `egress` | RFC 1918 private IPs and mock domain subdomains | Yes |
| `SI001` | `SyntaxIntegrity` | `syntax`, `parse` | Valid Python AST syntax parsing | No |
| `TI001` | `TargetIntegrity` | `targets`, `paths` | Verification that all audit targets exist | No |
| `WI001` | `WaiverIntegrity` | `waivers`, `waiver` | Auditable justification and waiver syntax | No |

```mermaid
flowchart TD
    classDef failure fill:#b3261e,color:#fff
    classDef success fill:#1b5e20,color:#fff
    classDef accent fill:#4527a0,color:#fff

    In["N paths: files and directories"] --> Exists{"All targets exist?"}:::accent
    Exists -->|"No"| TI["TargetIntegrity"]:::failure
    Exists -->|"Yes"| Expand["Expand and dedupe<br/>by resolved identity"]
    Expand --> Parse["ast.parse each module"]
    Parse --> Visit["ComplexityVisitor + SanitizationVisitor"]

    Visit --> CV["CyclomaticComplexity, NestingDepth"]
    Visit --> ZT["ZeroTrustSanitization"]

    Parse --> Tok["tokenize: header COMMENT tokens"]:::accent
    Tok --> W{"Waiver well-formed,<br/>waivable, justified?"}
    W -->|"No"| WI["WaiverIntegrity<br/>(original finding still reported)"]:::failure
    W -->|"Yes"| Sup["Suppress that invariant only"]

    CV -->|"never waivable"| Report["Consolidated report:<br/>files_checked + violations"]
    ZT --> Sup
    Sup --> Report
    WI --> Report
    TI --> Report
    Report --> Verdict{"Zero violations?"}
    Verdict -->|"Yes"| Pass["Exit 0"]:::success
    Verdict -->|"No"| Fail["Exit 1, prescriptive findings"]:::failure
```

---

## Auditable Waivers

A detector's own negative fixtures must embed the strings it hunts. Such modules declare a justified
waiver in the first 15 lines, parsed from real comment tokens so text inside a string literal can
never disarm the gate:

```python
"""Unit tests for the egress guard."""

# sentinel: allow[ZeroTrustSanitization] — negative fixtures asserting the detector fires
```

Only `ZeroTrustSanitization` is waivable. `CyclomaticComplexity` and `NestingDepth` are not: a metric
you can opt out of is not an invariant. A malformed waiver, one naming a non-waivable invariant, or one
lacking a justification of at least 12 characters raises `WaiverIntegrity` **and** leaves the original
violation reported.

---

## Programmatic Integration

```python
from pathlib import Path
from sentinel import audit_targets

report = audit_targets([Path("tools"), Path("tests")])
if not report.is_clean:
    for violation in report.violations:
        print(f"{violation.file_path}:{violation.line_number} — {violation.message}")
    exit(1)
```
