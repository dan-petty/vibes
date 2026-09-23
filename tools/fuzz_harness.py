#!/usr/bin/env python3
"""Fuzz this repository's own instruments and keep every input that broke one.

Every oracle here is a parser pointed at whatever is in the tree, and each is tested with
inputs its author thought of. §10a records what that misses: an error path that has never
run is not known to work, and an instrument that answers differently for identical input
is broken. Both are properties of a parser under inputs nobody wrote by hand.

Four properties are checked, and only these four, because each one is mechanically
decidable without a model of what the instrument *should* say:

| Property | Question | Severity |
|---|---|---|
| `crash` | Did it raise something outside its declared tolerances? | error |
| `nondeterministic` | Did two identical calls disagree? | error |
| `non_idempotent` | Did applying a fixer twice differ from applying it once? | error |
| `slow` | Did one call exceed the time budget? | warning |

`slow` is a warning and the other three are errors, deliberately. A time budget measures
the host as much as the code — `is_dir()` costs 0.764ms on this repository's bind mount
and 0.001ms on tmpfs — so gating a release on it would block whichever machine happened to
be slow that morning. It still earns its place: catastrophic regex backtracking and
unbounded recursion show up here and nowhere else.

**The fuzzer is not the gate. The corpus is.** A time-boxed random search that must pass
is a flaky gate: it fails on the run that happened to find something and passes on the run
that happened not to, and neither outcome is about the commit under test. So `run` explores
and writes what it found into `artifacts/fuzz-corpus/`, and `replay` — which is
deterministic, fast, and only ever grows — is what CI and the pre-push hook execute. A
finding becomes a permanent regression test at the moment it is committed.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fuzz_core import SUFFIXES, Case, generate, mutate, shrink

# The instruments live beside this tool, not inside whatever tree is being fuzzed.
TOOLING_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS_DIR: Final[Path] = Path("artifacts/fuzz-corpus")
DEFAULT_BUDGET_SECONDS: Final[float] = 10.0
DEFAULT_CASES: Final[int] = 60

# Memory addresses appear in the `repr` of any object without a `__repr__`, and differ
# between two calls that agreed about everything that matters. Normalizing them is what
# keeps the determinism check from reporting every target as broken.
_ADDRESS_RE: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]+")


@dataclass(frozen=True)
class Observation:
    """What one invocation of an instrument produced."""

    fingerprint: str | None
    error: str | None
    elapsed: float


@dataclass(frozen=True)
class Failure:
    """One property violated by one input, with enough provenance to reproduce it."""

    target: str
    prop: str
    corpus: str
    seed: int
    origin: str
    detail: str
    digest: str = ""

    @property
    def severity(self) -> str:
        """Return whether this failure gates a release or informs one."""
        return SEVERITIES[self.prop]


@dataclass(frozen=True)
class Target:
    """One instrument under test, and what it is allowed to raise."""

    name: str
    corpus: str
    module: str
    summary: str
    invoke: Callable[[str, Path], object]
    tolerated: tuple[type[BaseException], ...] = ()
    fixer: Callable[[str, Path], str] | None = None


@dataclass(frozen=True)
class Plan:
    """How one campaign runs: how far to search, where the corpus is, and what to keep.

    A parameter object rather than five arguments. `explore` reached six and the code
    smell quantifier said so, which is the gate catching its author for the second time
    in the change that added it — the normal case, per §10a.5.
    """

    cases: int = DEFAULT_CASES
    seed: int = 0
    corpus_dir: Path = DEFAULT_CORPUS_DIR
    budget: float = DEFAULT_BUDGET_SECONDS
    save: bool = True

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> Plan:
        """Build a plan from parsed CLI arguments."""
        return cls(
            cases=args.cases,
            seed=args.seed,
            corpus_dir=args.corpus_dir,
            budget=args.budget,
            save=not args.no_save,
        )


@dataclass
class Campaign:
    """The outcome of one exploration or replay."""

    executed: int = 0
    failures: list[Failure] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """Render as the report the SARIF adapter and CI consume."""
        return {
            "executed": self.executed,
            "failures": [asdict(f) | {"severity": f.severity} for f in self.failures],
            "saved": self.saved,
        }


# --- Targets --------------------------------------------------------------------------
#
# Each entry names an instrument, the corpus that feeds it, and the exceptions it is
# allowed to raise. An empty tolerance is a claim: this function handles malformed input
# by reporting it, not by propagating. Widening a tolerance is how that claim gets
# withdrawn, and it should be a deliberate edit rather than a silent `except`.


def _example(name: str, module: str) -> Any:
    """Import a module from `examples/`, which is a directory of standalone programs."""
    directory = (TOOLING_ROOT / "examples" / name).resolve()
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return __import__(module)


def _write(workspace: Path, target: str, text: str, suffix: str) -> Path:
    """Materialize a case in the target's own scratch directory and return its path."""
    directory = workspace / target
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"case{suffix}"
    path.write_text(text, encoding="utf-8", errors="surrogateescape")
    return path


def _invoke_sentinel(text: str, workspace: Path) -> object:
    """Audit a generated module for complexity, nesting and egress invariants."""
    path = _write(workspace, "sentinel", text, ".py")
    return _example("ast-invariant-sentinel", "sentinel").audit_file(path)


def _invoke_smells(text: str, workspace: Path) -> object:
    """Quantify structural decay in a generated module, advisory detectors included.

    The SARIF adapter excludes advisory smells and the workbench keeps them out of the
    backlog, both for the same reason: they need judgement. Neither reason applies here,
    where the point is to execute the detector rather than to act on what it says, and
    excluding them left 25 of 30 generated modules producing no finding at all. The cost
    that motivated the exclusion is a whole-corpus cost — vulture over every module — and
    does not transfer: measured over 15 generated modules, advisory detection took 0.08s
    against 0.02s, and moved the yield from 3 findings to 263.
    """
    path = _write(workspace, "smells", text, ".py")
    analyze = _example("code-smell-quantifier", "smell_quantifier").analyze
    return analyze([path], include_advisory=True).findings


def _invoke_refactorer(text: str, workspace: Path) -> object:
    """Scan a generated module for decomposable functions."""
    del workspace
    from ast_refactorer import ASTRefactorer

    return ASTRefactorer().scan_source(text, "case.py")


def _invoke_docs(text: str, workspace: Path) -> object:
    """Validate a generated document's fences, tables, links, diagrams and egress."""
    from docs_validator import DocsValidator

    path = _write(workspace, "docs", text, ".md")
    return DocsValidator().validate_file(path)


def _invoke_docs_fix(text: str, workspace: Path) -> object:
    """Auto-repair a generated document, returning the repaired text and repair count."""
    del workspace
    from docs_validator import auto_fix_content

    return auto_fix_content(text, Path("case.md"))


def _fix_docs(text: str, workspace: Path) -> str:
    """Return only the repaired text, which is what idempotence is asserted over."""
    del workspace
    from docs_validator import auto_fix_content

    return auto_fix_content(text, Path("case.md"))[0]


def _invoke_roadmap(text: str, workspace: Path) -> object:
    """Parse a generated document as a roadmap, extracting whatever items it declares."""
    from roadmap_ingest import parse_roadmap

    return parse_roadmap(_write(workspace, "roadmap", text, ".md"))


def _invoke_requirements(text: str, workspace: Path) -> object:
    """Inventory the dependencies a generated pyproject declares."""
    from supply_chain_audit import inventory_requirements

    return inventory_requirements(_write(workspace, "requirements", text, ".toml"))


def _invoke_actions(text: str, workspace: Path) -> object:
    """Inventory the actions a generated workflow invokes, and how each is pinned."""
    from supply_chain_audit import inventory_actions

    return inventory_actions(_write(workspace, "actions", text, ".yml").parent)


TARGETS: Final[dict[str, Target]] = {
    target.name: target
    for target in (
        Target(
            name="sentinel",
            corpus="python",
            module="examples/ast-invariant-sentinel/sentinel.py",
            summary="AST invariant audit: complexity, nesting depth, egress sanitization",
            invoke=_invoke_sentinel,
        ),
        Target(
            name="smells",
            corpus="python",
            module="examples/code-smell-quantifier/smell_quantifier.py",
            summary="Quantified structural decay across a module",
            invoke=_invoke_smells,
        ),
        Target(
            name="refactorer",
            corpus="python",
            module="tools/ast_refactorer.py",
            summary="Detection of functions eligible for mechanical decomposition",
            invoke=_invoke_refactorer,
        ),
        Target(
            name="docs",
            corpus="markdown",
            module="tools/docs_validator.py",
            summary="Documentation integrity: fences, tables, links, diagrams, egress",
            invoke=_invoke_docs,
        ),
        Target(
            name="docs_fix",
            corpus="markdown",
            module="tools/docs_validator.py",
            summary="Documentation auto-repair, which must converge in one application",
            invoke=_invoke_docs_fix,
            fixer=_fix_docs,
        ),
        Target(
            name="roadmap",
            corpus="roadmap",
            module="tools/roadmap_ingest.py",
            summary="Roadmap item extraction from a Markdown backlog",
            invoke=_invoke_roadmap,
        ),
        Target(
            name="requirements",
            corpus="requirements",
            module="tools/supply_chain_audit.py",
            # A malformed manifest is a legitimate hard error: there is no dependency list
            # to report, and inventing an empty one would let a broken pyproject audit clean.
            summary="Dependency inventory from a pyproject manifest",
            invoke=_invoke_requirements,
            tolerated=(tomllib.TOMLDecodeError,),
        ),
        Target(
            name="actions",
            corpus="workflow",
            module="tools/supply_chain_audit.py",
            summary="GitHub Action inventory and pinning classification from a workflow",
            invoke=_invoke_actions,
        ),
    )
}


# --- Properties -----------------------------------------------------------------------

SEVERITIES: Final[dict[str, str]] = {
    "crash": "error",
    "nondeterministic": "error",
    "non_idempotent": "error",
    "slow": "warning",
}


def _fingerprint(value: object, workspace: Path) -> str:
    """Hash a normalized `repr`, so two runs can be compared without keeping either.

    Two things are normalized away, and both were found by this tool reporting them as
    defects in the instruments rather than in itself. Memory addresses appear in the
    `repr` of any object without a `__repr__`. The scratch directory appears in every
    finding's `file_path`, and it is a fresh random name per process — so the first
    cross-seed run reported all three hash seeds disagreeing for every target, which was
    not a property of any instrument but of where this harness had put the file.

    **The order of the two substitutions is load-bearing.** Addresses were normalized
    first, and `mkdtemp` draws its suffix from a pool that includes `0`, `x` and the hex
    digits: roughly one scratch directory in three hundred is named something like
    `vibes-fuzz-8t0x1im`, which the address pattern rewrites to `vibes-fuzz-8t0xX`. The
    path replacement then no longer matched, the scratch name reached the hash, and that
    run disagreed with every other. It presented as a 0.3% flake that vanished whenever it
    was investigated serially, because contention only raised the number of directories
    drawn, never the odds for any one of them. Replace the path first.
    """
    normalized = _ADDRESS_RE.sub("0xX", repr(value).replace(str(workspace), "<workspace>"))
    return hashlib.sha256(normalized.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def observe(target: Target, text: str, workspace: Path) -> Observation:
    """Invoke one instrument once, recording what it returned, raised, and cost.

    The instrument's own diagnostics are captured rather than left on the terminal. They
    are not discarded — a campaign of 480 cases produced several hundred lines of parser
    complaints from the dead-code pass, and a report buried under them is a report nobody
    reads. What an instrument prints is not a property being tested; what it returns is.
    """
    start = time.perf_counter()
    noise = io.StringIO()
    try:
        with contextlib.redirect_stderr(noise), contextlib.redirect_stdout(noise):
            value = target.invoke(text, workspace)
    except target.tolerated as err:
        return Observation(f"tolerated:{type(err).__name__}", None, time.perf_counter() - start)
    # Catching broadly is this tool's entire job: an exception the instrument did not
    # declare is the finding, whatever type it happens to be.
    except Exception as err:
        detail = f"{type(err).__name__}: {err}".replace("\n", " ")[:300]
        return Observation(None, detail, time.perf_counter() - start)
    return Observation(_fingerprint(value, workspace), None, time.perf_counter() - start)


def _prop_crash(target: Target, text: str, ws: Path, first: Observation, budget: float) -> str | None:
    """Report an exception outside the instrument's declared tolerances."""
    del target, text, ws, budget
    return first.error


def _prop_nondeterministic(
    target: Target, text: str, ws: Path, first: Observation, budget: float
) -> str | None:
    """Report two identical invocations that disagreed."""
    del budget
    if first.error is not None:
        return None
    second = observe(target, text, ws)
    if second.fingerprint == first.fingerprint:
        return None
    return f"two identical calls returned {first.fingerprint} then {second.fingerprint}"


def _prop_non_idempotent(
    target: Target, text: str, ws: Path, first: Observation, budget: float
) -> str | None:
    """Report a repair that changed the document again on a second application."""
    del budget
    if target.fixer is None or first.error is not None:
        return None
    once = target.fixer(text, ws)
    twice = target.fixer(once, ws)
    if once == twice:
        return None
    return f"repair did not converge: {len(once)} chars became {len(twice)} on reapplication"


def _prop_slow(target: Target, text: str, ws: Path, first: Observation, budget: float) -> str | None:
    """Report an invocation that exceeded the time budget."""
    del target, text, ws
    if first.elapsed <= budget:
        return None
    return f"one call took {first.elapsed:.2f}s against a {budget:.2f}s budget"


PROPERTIES: Final[dict[str, Callable[[Target, str, Path, Observation, float], str | None]]] = {
    "crash": _prop_crash,
    "nondeterministic": _prop_nondeterministic,
    "non_idempotent": _prop_non_idempotent,
    "slow": _prop_slow,
}

# Ordered so the most explanatory failure is the one reported. An instrument that raised
# cannot meaningfully be asked whether it is deterministic.
PROPERTY_ORDER: Final[tuple[str, ...]] = ("crash", "nondeterministic", "non_idempotent", "slow")


def evaluate(
    target: Target, case: Case, workspace: Path, budget: float = DEFAULT_BUDGET_SECONDS
) -> Failure | None:
    """Check every property against one input, returning the first that fails."""
    first = observe(target, case.text, workspace)
    for name in PROPERTY_ORDER:
        detail = PROPERTIES[name](target, case.text, workspace, first, budget)
        if detail:
            return Failure(target.name, name, case.corpus, case.seed, case.origin, detail)
    return None


# --- Campaigns ------------------------------------------------------------------------


def _case_stream(target: Target, cases: int, seed: int) -> Iterator[Case]:
    """Yield the inputs for one target: a third well-formed, the rest damaged.

    Keeping a share of the corpus well-formed is not politeness. A parser that rejects
    everything it is given exercises one `except` and nothing behind it, and the branches
    worth reaching are the ones that run after a successful parse.
    """
    rng = random.Random(seed)
    for index in range(cases):
        base = generate(target.corpus, seed + index)
        yield base if index % 3 == 0 else mutate(base, rng, generate(target.corpus, seed - index).text)


def _failure_class(failure: Failure) -> tuple[str, str, str]:
    """Group failures by what is wrong rather than by which input exposed it.

    Two hundred mutations of the same module find the same unhandled exception two hundred
    times. Reporting each one would bury a second, rarer defect under the first.
    """
    return (failure.target, failure.prop, failure.detail.split(":")[0])


def _minimize(target: Target, case: Case, failure: Failure, workspace: Path, plan: Plan) -> Case:
    """Shrink a failing input to the smallest one that fails the same property.

    The predicate insists on the *same* property, not merely on some failure. Without that
    the shrinker is free to wander into a different, easier defect and report a minimized
    input that never demonstrated the one that was found.
    """

    def still_fails(text: str) -> bool:
        found = evaluate(target, Case(case.corpus, text, case.seed, case.origin), workspace, plan.budget)
        return found is not None and found.prop == failure.prop

    return Case(case.corpus, shrink(case.text, still_fails), case.seed, case.origin)


def _explore_case(
    target: Target,
    case: Case,
    workspace: Path,
    plan: Plan,
    seen: set[tuple[str, str, str]],
    campaign: Campaign,
) -> None:
    """Evaluate one generated case and record novel failures."""
    campaign.executed += 1
    failure = evaluate(target, case, workspace, plan.budget)
    if failure is None:
        return
    f_class = _failure_class(failure)
    if f_class in seen:
        return
    seen.add(f_class)
    minimized = _minimize(target, case, failure, workspace, plan)
    _record(campaign, target, failure, minimized, plan)


def explore(targets: Sequence[Target], workspace: Path, plan: Plan) -> Campaign:
    """Search for inputs that violate a property, and keep the ones that do."""
    campaign = Campaign()
    seen: set[tuple[str, str, str]] = set()
    for target in targets:
        for case in _case_stream(target, plan.cases, plan.seed):
            _explore_case(target, case, workspace, plan, seen, campaign)
    return campaign


def _record(
    campaign: Campaign, target: Target, failure: Failure, minimized: Case, plan: Plan
) -> None:
    """Attach a minimized failure to the campaign, persisting it when the plan keeps it."""
    campaign.failures.append(replace(failure, digest=minimized.digest()))
    if plan.save:
        campaign.saved.append(str(save_case(plan.corpus_dir, target.name, minimized)))


def _replay_case(
    target: Target, case: Case, workspace: Path, plan: Plan, campaign: Campaign
) -> None:
    """Evaluate one replayed case and record failures."""
    campaign.executed += 1
    failure = evaluate(target, case, workspace, plan.budget)
    if failure is not None:
        campaign.failures.append(replace(failure, digest=case.digest()))


def replay(targets: Sequence[Target], workspace: Path, plan: Plan) -> Campaign:
    """Re-run every input the corpus has ever kept. This is the gate."""
    campaign = Campaign()
    for target in targets:
        for case in iter_corpus(plan.corpus_dir, target):
            _replay_case(target, case, workspace, plan, campaign)
    return campaign


def save_case(corpus_dir: Path, target: str, case: Case) -> Path:
    """Write a case into the regression corpus, named by its content address."""
    directory = corpus_dir / target
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / case.filename()
    path.write_text(case.text, encoding="utf-8", errors="surrogateescape")
    return path


def iter_corpus(corpus_dir: Path, target: Target) -> Iterator[Case]:
    """Yield every stored input for one target, in a stable order."""
    directory = corpus_dir / target.name
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.case")):
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
        yield Case(target.corpus, text, 0, f"corpus:{path.name}")


def gating_failures(campaign: Campaign) -> list[Failure]:
    """Return only the failures that stop a release, excluding host-sensitive timings."""
    return [failure for failure in campaign.failures if failure.severity == "error"]


# --- Cross-process determinism --------------------------------------------------------
#
# String hashing is randomized per process, which is how the cohesion detector's asymmetric
# edge relation surfaced: 15, 16 and 17 findings on three consecutive runs of unchanged
# files (§10a.2). A same-process repeat cannot see that class of defect, because the seed
# is fixed for the life of an interpreter. Only a second process can vary it, so this check
# spawns one per seed. It costs an interpreter startup per case and therefore belongs in
# CI rather than in a pre-commit hook.

HASH_SEEDS: Final[tuple[str, ...]] = ("0", "1", "524287")
CROSSCHECK_TIMEOUT: Final[float] = 120.0


def _seed_fingerprint(target: str, path: Path, seed: str) -> str:
    """Return one instrument's fingerprint for one input under a given hash seed."""
    command = [sys.executable, str(Path(__file__).resolve()), "fingerprint"]
    command += ["--targets", target, "--input", str(path)]
    # Fixed argv, no shell, and every path is one this process wrote.
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONHASHSEED=seed),
        timeout=CROSSCHECK_TIMEOUT,
        check=False,
    )
    return proc.stdout.strip() or f"worker-exit-{proc.returncode}"


def _crosscheck_cases(target: Target, plan: Plan) -> Iterator[Case]:
    """Yield the stored corpus first, then freshly generated inputs.

    Generated inputs are included so the check still exercises something when the corpus
    for a target is empty. A determinism gate that silently has nothing to run is the
    failure mode §8.3 names: indistinguishable, from the terminal, from one that passed.
    """
    yield from iter_corpus(plan.corpus_dir, target)
    yield from _case_stream(target, plan.cases, plan.seed)


def _crosscheck_case(
    target: Target,
    index: int,
    case: Case,
    workspace: Path,
    campaign: Campaign,
) -> None:
    """Evaluate determinism of one case across hash seeds."""
    path = _write(workspace, f"cross-{target.name}-{index}", case.text, SUFFIXES[case.corpus])
    campaign.executed += 1
    answers = _confirmed_answers(target.name, path)
    if answers is not None:
        campaign.failures.append(_divergence(target, case, answers))


def crosscheck(targets: Sequence[Target], workspace: Path, plan: Plan) -> Campaign:
    """Report any instrument whose answer depends on the interpreter's hash seed."""
    campaign = Campaign()
    for target in targets:
        for index, case in enumerate(_crosscheck_cases(target, plan)):
            _crosscheck_case(target, index, case, workspace, campaign)
    return campaign


def _confirmed_answers(target: str, path: Path) -> dict[str, str] | None:
    """Return the seed answers when they disagree twice running, else None.

    A determinism verdict taken from one sample per seed is itself a one-sample
    measurement. Real hash-order dependence is deterministic for a fixed seed and so
    survives the second pass unchanged; a sampling artefact does not, and without this
    the check reported two findings per run that no serial re-run could reproduce. The
    second pass costs three subprocesses and runs only on disagreement.
    """
    answers = {seed: _seed_fingerprint(target, path, seed) for seed in HASH_SEEDS}
    if len(set(answers.values())) == 1:
        return None
    again = {seed: _seed_fingerprint(target, path, seed) for seed in HASH_SEEDS}
    return again if len(set(again.values())) > 1 else None


def _divergence(target: Target, case: Case, answers: dict[str, str]) -> Failure:
    """Describe one instrument that answered differently under two hash seeds."""
    rendered = ", ".join(f"PYTHONHASHSEED={seed}->{answer}" for seed, answer in sorted(answers.items()))
    return Failure(
        target.name, "nondeterministic", case.corpus, case.seed, case.origin, rendered, case.digest()
    )


# --- CLI ------------------------------------------------------------------------------


def _selected(names: Sequence[str]) -> list[Target]:
    """Resolve target names to targets, preserving a stable order."""
    return [TARGETS[name] for name in sorted(names)]


def _print_campaign(campaign: Campaign, label: str) -> None:
    """Render a campaign the way a terminal reader needs it: worst first, then counts."""
    for failure in sorted(campaign.failures, key=lambda f: (f.severity != "error", f.target)):
        mark = "❌" if failure.severity == "error" else "⚠️ "
        print(f"{mark} {failure.target}/{failure.prop}  [{failure.origin}] {failure.digest}")
        print(f"     {failure.detail}")
    errors = len(gating_failures(campaign))
    print(f"{label}: {campaign.executed} case(s), {errors} error(s), {len(campaign.failures)} finding(s).")


def _write_report(campaign: Campaign, path: Path | None) -> None:
    """Persist the campaign as JSON for CI and the SARIF adapter."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(campaign.to_json(), indent=2) + "\n", encoding="utf-8")


def _handle_run(args: argparse.Namespace) -> int:
    """Explore for new failures, keeping each minimized input in the corpus."""
    with tempfile.TemporaryDirectory(prefix="vibes-fuzz-") as scratch:
        campaign = explore(_selected(args.targets), Path(scratch), Plan.from_args(args))
    _write_report(campaign, args.report)
    _print_campaign(campaign, "Explored")
    for saved in campaign.saved:
        print(f"   kept {saved}")
    return 1 if gating_failures(campaign) else 0


def _handle_replay(args: argparse.Namespace) -> int:
    """Re-run the committed corpus, which is what CI and the pre-push hook execute."""
    with tempfile.TemporaryDirectory(prefix="vibes-fuzz-") as scratch:
        campaign = replay(_selected(args.targets), Path(scratch), Plan.from_args(args))
    _write_report(campaign, args.report)
    _print_campaign(campaign, "Replayed")
    return 1 if gating_failures(campaign) else 0


def _handle_targets(args: argparse.Namespace) -> int:
    """List the instruments under test and what each one is allowed to raise."""
    for target in _selected(args.targets):
        tolerated = ", ".join(exc.__name__ for exc in target.tolerated) or "nothing"
        stored = sum(1 for _ in iter_corpus(args.corpus_dir, target))
        print(f"{target.name:<14} {target.corpus:<13} corpus={stored:<4} tolerates={tolerated}")
        print(f"               {target.summary}")
    return 0


def _handle_crosscheck(args: argparse.Namespace) -> int:
    """Re-run every input under several hash seeds and report any that disagree."""
    with tempfile.TemporaryDirectory(prefix="vibes-fuzz-") as scratch:
        campaign = crosscheck(_selected(args.targets), Path(scratch), Plan.from_args(args))
    _write_report(campaign, args.report)
    _print_campaign(campaign, "Cross-checked")
    return 1 if gating_failures(campaign) else 0


def _handle_fingerprint(args: argparse.Namespace) -> int:
    """Print one instrument's fingerprint for one file. This is the crosscheck worker."""
    if args.input is None:
        print("fingerprint requires --input", file=sys.stderr)
        return 2
    target = TARGETS[sorted(args.targets)[0]]
    text = args.input.read_text(encoding="utf-8", errors="surrogateescape")
    with tempfile.TemporaryDirectory(prefix="vibes-fuzz-") as scratch:
        observed = observe(target, text, Path(scratch))
    print(observed.fingerprint or f"error:{observed.error}")
    return 0


_HANDLERS: Final[dict[str, Callable[[argparse.Namespace], int]]] = {
    "crosscheck": _handle_crosscheck,
    "fingerprint": _handle_fingerprint,
    "replay": _handle_replay,
    "run": _handle_run,
    "targets": _handle_targets,
}


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser for the instrument fuzzer."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=sorted(_HANDLERS))
    parser.add_argument("--targets", nargs="*", default=sorted(TARGETS), choices=sorted(TARGETS))
    parser.add_argument("--cases", type=int, default=DEFAULT_CASES, help="Inputs per target")
    parser.add_argument("--seed", type=int, default=0, help="Base seed; the campaign is a function of it")
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--report", type=Path, default=None, help="Write the campaign as JSON")
    parser.add_argument("--no-save", action="store_true", help="Explore without growing the corpus")
    parser.add_argument("--input", type=Path, default=None, help="Single input, for `fingerprint`")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the instrument fuzzer."""
    args = build_arg_parser().parse_args(argv)
    return _HANDLERS[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
