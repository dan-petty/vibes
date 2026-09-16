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
python3 sentinel.py /path/to/python/code
```

### Running the Tests
```bash
pytest test_sentinel.py -v
```

---

## Programmatic Integration

```python
from pathlib import Path
from sentinel import audit_directory

report = audit_directory(Path("src"))
if not report.is_clean:
    for violation in report.violations:
        print(f"{violation.file_path}:{violation.line_number} — {violation.message}")
    exit(1)
```
