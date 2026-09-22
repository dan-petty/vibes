"""The documented quality gates must be the gates that actually run.

`CONTRIBUTING.md` tells contributors which gates every pull request runs. Nothing
executed that claim, so a gate was documented and never wired into `ci.yml` — the
convention-versus-enforcement gap this repository exists to close, reappearing in the
document that describes the closing of it.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The tool column of the gate table names a command; these are the invocations CI must carry.
_TOOL_CELL_RE = re.compile(r"^\|\s*\*\*[^|]+\*\*\s*\|\s*([^|]+)\|")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")


def _documented_gate_tools() -> list[str]:
    """Extract the tool invocation named by each row of the quality gate table."""
    lines = CONTRIBUTING.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## Quality Gates"))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    rows = (_TOOL_CELL_RE.match(line) for line in lines[start:end])
    cells = [match.group(1).strip() for match in rows if match]
    return [span.group(1) for cell in cells if (span := _CODE_SPAN_RE.search(cell))]


def _executable_token(invocation: str) -> str:
    """Reduce a documented invocation to the executable or script CI must reference."""
    first = invocation.split()[0]
    return Path(first).name if "/" in first else first


def test_every_documented_gate_is_invoked_by_ci() -> None:
    """A gate that exists only in prose is a claim, not a gate."""
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    documented = _documented_gate_tools()
    missing = [tool for tool in documented if _executable_token(tool) not in workflow]

    assert documented, "Gate table parsed as empty; the table format changed."
    assert missing == [], f"Documented in CONTRIBUTING.md but absent from ci.yml: {missing}"


def test_gate_table_covers_the_reliability_objectives_gate() -> None:
    """Regression guard for the specific gate that was documented and never wired."""
    assert any("reliability_slo.py" in tool for tool in _documented_gate_tools())
    assert "reliability_slo.py status" in CI_WORKFLOW.read_text(encoding="utf-8")
