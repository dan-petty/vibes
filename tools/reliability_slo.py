#!/usr/bin/env python3
"""Reliability SLO Engine: error budgets for a self-improving codebase.

The feedback loop in `AGENTS.md` §11 inverts on a binary condition — health is 100.0
or it is not. Binary conditions make bad policy for two reasons. A single flaky
iteration flips the whole loop into reactive mode and freezes proactive work that was
not actually blocked. And a loop that has never once failed cannot tell whether it is
reliable or merely unambitious, because both look identical from inside a green run.

SRE solves this with the same instrument for both: an error budget. An objective below
100% makes a measured, affordable amount of failure explicit; spending inside that
budget is normal operation, not an emergency, and *never* spending it is itself a
finding — the objective is too loose to be steering anything.

Design patterns, applied deliberately:

- **Registry / Strategy** (`SLI_REGISTRY`): each indicator is a pure function from an
  iteration report to (good events, valid events). Adding one is a dict entry, never a
  branch in a dispatcher.
- **Value objects**: `SliMeasurement`, `BudgetState` and `PolicyDecision` are frozen;
  every calculation returns a new one, so a decision can be logged and replayed.
- **Policy object** (`decide_phase`): the budget state maps to a phase through one
  table, so the rule an agent must obey is readable in a single place.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

# SRE convention: an objective is a target on good events over valid events, never an
# average. Averages hide the tail that callers actually experience.
DEFAULT_HISTORY_PATH: Final[Path] = Path(".data/slo_history.json")
MAX_HISTORY_ITERATIONS: Final[int] = 100
FAST_FEEDBACK_CEILING_SECONDS: Final[float] = 2.0
# Burn faster than this and the window will be exhausted before it closes.
CRITICAL_BURN_RATE: Final[float] = 2.0
# Burn rate divides by elapsed window, so the first iterations always look catastrophic.
# Requiring a minimum sample is what stops one unlucky run from freezing the whole loop —
# the noise this engine exists to remove. A zero-budget objective still trips immediately.
MIN_BURN_RATE_ITERATIONS: Final[int] = 3
# An objective that never consumes budget across a full window is not steering anything.
SLACK_BUDGET_THRESHOLD: Final[float] = 0.0


class LoopPhase(StrEnum):
    """The phase the self-improvement loop should occupy, per AGENTS.md §11."""

    REACTIVE_REMEDIATION = "PHASE_1_REACTIVE_REMEDIATION"
    PROACTIVE_ELEVATION = "PHASE_2_PROACTIVE_ELEVATION"
    # Deliberately unnumbered: this is not AGENTS.md §11 Phase 3, which is self-hardening.
    OBJECTIVE_REVIEW = "OBJECTIVE_REVIEW"


class BudgetStatus(StrEnum):
    """Error budget health, in the vocabulary an on-call engineer would use."""

    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    EXHAUSTED = "EXHAUSTED"
    BURNING = "BURNING"
    HEALTHY = "HEALTHY"
    UNSPENT = "UNSPENT"


@dataclass(frozen=True)
class SliMeasurement:
    """One indicator measured over one iteration: good events out of valid events."""

    name: str
    good_events: int
    valid_events: int

    @property
    def ratio(self) -> float:
        """Return the success ratio, defined as 1.0 when nothing was measurable."""
        return 1.0 if self.valid_events == 0 else self.good_events / self.valid_events


@dataclass(frozen=True)
class ServiceLevelObjective:
    """A target ratio for an indicator, evaluated over a rolling iteration window."""

    sli: str
    target: float
    window_iterations: int
    rationale: str
    # A ratio over a handful of events is noise, and acting on noise is the twitchiness
    # this engine exists to remove. Zero-budget objectives override this to 1.
    min_valid_events: int = 10
    # Gating objectives describe whether the artifact is fit to ship, and failing one
    # fails the build. A steering objective describes how the loop should spend effort;
    # it moves the phase but must never block a release.
    #
    # The test is whether a breach means "this must not ship" or "we should work on this
    # next". Only defects gate: a violated invariant, or a resource failing its own gate.
    # Slow tests, near-ceiling complexity and an automatable backlog are all real signals
    # about where effort should go, and none of them makes the artifact unfit.
    gating: bool = True


@dataclass(frozen=True)
class BudgetState:
    """Error budget arithmetic for one objective over its window."""

    objective: ServiceLevelObjective
    observed: float
    iterations: int
    consumed: float
    burn_rate: float
    status: BudgetStatus

    @property
    def remaining(self) -> float:
        """Return the unconsumed fraction of the error budget."""
        return max(0.0, 1.0 - self.consumed)


@dataclass(frozen=True)
class PolicyDecision:
    """The phase the loop should occupy and the objective that decided it."""

    phase: LoopPhase
    driver: str
    rationale: str


def _resources(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the evaluated resources of an iteration report."""
    return report.get("resources_evaluated", [])


def _feedback(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the actionable feedback items, excluding positive reinforcement."""
    items = report.get("improvement_feedback", [])
    return [item for item in items if item.get("category") != "POSITIVE_REINFORCEMENT"]


def measure_gate_pass_rate(report: dict[str, Any]) -> SliMeasurement:
    """Availability analogue: resources certified healthy out of resources evaluated."""
    resources = _resources(report)
    healthy = sum(1 for item in resources if item.get("health_status") == "HEALTHY")
    return SliMeasurement("gate_pass_rate", healthy, len(resources))


def measure_invariant_compliance(report: dict[str, Any]) -> SliMeasurement:
    """Correctness analogue: resources free of complexity and sanitization violations."""
    resources = _resources(report)
    clean = sum(
        1
        for item in resources
        if not item["scan_metrics"]["complexity_violations"]
        and not item["scan_metrics"]["sanitization_violations"]
    )
    return SliMeasurement("invariant_compliance", clean, len(resources))


def _execution_seconds(run: dict[str, Any]) -> float:
    """Return test execution time, preferring the suite's self-report over wall-clock."""
    reported = run.get("execution_seconds")
    return run.get("duration_seconds", 0.0) if reported is None else reported


def measure_feedback_latency(report: dict[str, Any]) -> SliMeasurement:
    """Latency SLI: suites returning inside the fast-feedback ceiling.

    Counted as good events over valid events rather than as a mean, because one
    pathologically slow suite is exactly what an average is designed to hide.
    """
    runs = [item["run_result"] for item in _resources(report) if item.get("run_result")]
    fast = sum(1 for run in runs if _execution_seconds(run) <= FAST_FEEDBACK_CEILING_SECONDS)
    return SliMeasurement("feedback_latency", fast, len(runs))


def measure_headroom_saturation(report: dict[str, Any]) -> SliMeasurement:
    """Saturation golden signal: resources with no function near the complexity ceiling."""
    resources = _resources(report)
    spacious = sum(1 for item in resources if not item["scan_metrics"]["near_threshold_functions"])
    return SliMeasurement("headroom_saturation", spacious, len(resources))


# Feedback categories a tool in this repository can already remediate without judgement.
AUTOMATABLE_CATEGORIES: Final[frozenset[str]] = frozenset({"PROACTIVE_REFACTOR", "DOCUMENTATION"})


def measure_toil_containment(report: dict[str, Any]) -> SliMeasurement:
    """Toil SLI: backlog items that are not machine-fixable work left to a human.

    SRE counts toil as manual work a machine could do. An item in `AUTOMATABLE_CATEGORIES`
    sitting in the backlog is exactly that: `ast_refactorer.py` or `docs_validator --fix`
    could clear it, and an agent is spending judgement on it instead.
    """
    items = _feedback(report)
    non_toil = sum(1 for item in items if item.get("category") not in AUTOMATABLE_CATEGORIES)
    return SliMeasurement("toil_containment", non_toil, len(items))


SLI_REGISTRY: Final[dict[str, Callable[[dict[str, Any]], SliMeasurement]]] = {
    "gate_pass_rate": measure_gate_pass_rate,
    "invariant_compliance": measure_invariant_compliance,
    "feedback_latency": measure_feedback_latency,
    "headroom_saturation": measure_headroom_saturation,
    "toil_containment": measure_toil_containment,
}

DEFAULT_OBJECTIVES: Final[tuple[ServiceLevelObjective, ...]] = (
    ServiceLevelObjective(
        sli="invariant_compliance",
        target=1.0,
        window_iterations=20,
        rationale="Invariants are the one thing with no acceptable failure rate; a breach is never routine.",
        min_valid_events=1,
    ),
    ServiceLevelObjective(
        sli="gate_pass_rate",
        target=0.98,
        window_iterations=20,
        rationale="A gate failing on 2% of resources is a working gate, not an incident.",
    ),
    ServiceLevelObjective(
        sli="feedback_latency",
        target=0.95,
        window_iterations=20,
        rationale="Agent agility depends on the tail, so one slow suite in twenty is the affordable limit.",
        # Steering: a slow suite is a loop-agility problem, not an unfit artifact. It is also
        # host-sensitive — is_dir() measured 0.764ms on a bind mount against 0.001ms on tmpfs,
        # and load swings readings three to fivefold. Gating on it would block releases for
        # the speed of whatever machine happened to run them.
        gating=False,
    ),
    ServiceLevelObjective(
        sli="headroom_saturation",
        target=0.90,
        window_iterations=20,
        rationale="Functions may sit near the ceiling briefly; a tenth of the tree doing so permanently is drift.",
        # Steering: sitting near the ceiling is exactly the proactive elevation work of
        # Phase 2, not a defect. Crossing the ceiling is a defect, and invariant_compliance
        # already gates that at a zero-budget target.
        gating=False,
    ),
    ServiceLevelObjective(
        sli="toil_containment",
        target=0.50,
        window_iterations=20,
        rationale="SRE's toil ceiling: over half the backlog being machine-fixable means the machine should fix it.",
        gating=False,
    ),
)


def measure_report(report: dict[str, Any]) -> dict[str, SliMeasurement]:
    """Measure every registered indicator against one iteration report."""
    return {name: measure(report) for name, measure in SLI_REGISTRY.items()}


def _read_json(path: Path) -> Any:
    """Read JSON, treating any unreadable or malformed ledger as absent.

    The ledger is telemetry, not a source of truth. A corrupt file must degrade the
    window, never halt the loop that was about to record a fresh measurement into it.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _shard_entries(directory: Path) -> list[dict[str, Any]]:
    """Read every iteration shard in a directory, oldest first by filename."""
    entries = (_read_json(shard) for shard in sorted(directory.glob("*.json")))
    return [entry for entry in entries if isinstance(entry, dict)]


def load_history(path: Path) -> list[dict[str, Any]]:
    """Load the bounded iteration history from a single ledger or a shard directory."""
    if path.is_dir():
        return _shard_entries(path)[-MAX_HISTORY_ITERATIONS:]
    data = _read_json(path)
    return data[-MAX_HISTORY_ITERATIONS:] if isinstance(data, list) else []


def _shard_name(entry: dict[str, Any], existing: int) -> str:
    """Name a shard so filename order is chronological order."""
    stamp = str(entry.get("timestamp", "")).replace(":", "").replace("-", "") or f"{existing:05d}"
    return f"{stamp}-{existing:05d}.json"


def _write_shard(entry: dict[str, Any], directory: Path) -> None:
    """Write one iteration to its own file and prune beyond the retention window.

    One file per iteration rather than one shared ledger: concurrent branches each add a
    distinct path, so git merges them without conflict. A single appended array would
    conflict on every parallel iteration, which is the failure this repository already
    decomposed task tracking to avoid.
    """
    directory.mkdir(parents=True, exist_ok=True)
    shards = sorted(directory.glob("*.json"))
    (directory / _shard_name(entry, len(shards))).write_text(
        json.dumps(entry, indent=2), encoding="utf-8"
    )
    for stale in sorted(directory.glob("*.json"))[:-MAX_HISTORY_ITERATIONS]:
        stale.unlink(missing_ok=True)


def record_iteration(report: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    """Record one iteration's measurements, bounded to the retention window.

    A path that is a directory (or names no file extension) is treated as a shard
    directory; anything else is a single JSON ledger.
    """
    entry = {
        "timestamp": report.get("timestamp", ""),
        "measurements": {
            name: [m.good_events, m.valid_events] for name, m in measure_report(report).items()
        },
    }
    if path.is_dir() or not path.suffix:
        _write_shard(entry, path)
        return load_history(path)
    history = [*load_history(path), entry][-MAX_HISTORY_ITERATIONS:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return history


def _window_totals(history: Sequence[dict[str, Any]], sli: str, window: int) -> tuple[int, int]:
    """Sum good and valid events for one indicator across the trailing window.

    Aggregated over the window rather than averaged per iteration: an iteration that
    measured two resources should not weigh as heavily as one that measured two hundred.
    """
    recent = [entry for entry in history[-window:] if sli in entry.get("measurements", {})]
    pairs = [entry["measurements"][sli] for entry in recent]
    return sum(good for good, _ in pairs), sum(valid for _, valid in pairs)


def _consumed_fraction(observed: float, target: float) -> float:
    """Return the fraction of the error budget spent, treating a 1.0 target as zero-budget."""
    budget = 1.0 - target
    if budget <= 0.0:
        return 0.0 if observed >= 1.0 else 1.0
    return min((1.0 - observed) / budget, 1.0) if observed < 1.0 else 0.0


@dataclass(frozen=True)
class BudgetSample:
    """The evidence a status classification is drawn from."""

    consumed: float
    burn_rate: float
    iterations: int
    valid_events: int
    min_valid_events: int
    window_full: bool

    @property
    def is_undersampled(self) -> bool:
        """Too few events for the ratio to mean anything."""
        return self.valid_events < self.min_valid_events

    @property
    def is_exhausted(self) -> bool:
        """The whole budget has been spent."""
        return self.consumed >= 1.0

    @property
    def is_burning(self) -> bool:
        """Spending faster than the window elapses, on enough iterations to believe it."""
        return self.burn_rate > CRITICAL_BURN_RATE and self.iterations >= MIN_BURN_RATE_ITERATIONS

    @property
    def is_unspent(self) -> bool:
        """A full window closed without the budget being touched."""
        return self.window_full and self.consumed <= SLACK_BUDGET_THRESHOLD


# Ordered predicates: the first match wins, so precedence is the list order and nothing else.
_STATUS_RULES: Final[tuple[tuple[str, BudgetStatus], ...]] = (
    ("is_undersampled", BudgetStatus.INSUFFICIENT_DATA),
    ("is_exhausted", BudgetStatus.EXHAUSTED),
    ("is_burning", BudgetStatus.BURNING),
    ("is_unspent", BudgetStatus.UNSPENT),
)


def _budget_status(sample: BudgetSample) -> BudgetStatus:
    """Classify budget health from consumption, burn rate, sample size, and window maturity."""
    matches = (status for predicate, status in _STATUS_RULES if getattr(sample, predicate))
    return next(matches, BudgetStatus.HEALTHY)


def evaluate_objective(
    objective: ServiceLevelObjective, history: Sequence[dict[str, Any]]
) -> BudgetState:
    """Compute error budget state for one objective over its rolling window."""
    good, valid = _window_totals(history, objective.sli, objective.window_iterations)
    observed = 1.0 if valid == 0 else good / valid
    iterations = min(len(history), objective.window_iterations)
    consumed = _consumed_fraction(observed, objective.target)
    elapsed = max(iterations / objective.window_iterations, 1e-9)
    burn_rate = consumed / elapsed
    window_full = iterations >= objective.window_iterations
    return BudgetState(
        objective=objective,
        observed=observed,
        iterations=iterations,
        consumed=consumed,
        burn_rate=burn_rate,
        status=_budget_status(
            BudgetSample(
                consumed=consumed,
                burn_rate=burn_rate,
                iterations=iterations,
                valid_events=valid,
                min_valid_events=objective.min_valid_events,
                window_full=window_full,
            )
        ),
    )


def evaluate_all(
    history: Sequence[dict[str, Any]],
    objectives: Sequence[ServiceLevelObjective] = DEFAULT_OBJECTIVES,
) -> list[BudgetState]:
    """Evaluate every objective against the recorded history."""
    return [evaluate_objective(objective, history) for objective in objectives]


# The policy table: budget status maps to a phase, so the rule is readable in one place
# rather than inferred from a dispatcher. Ordered by precedence.
_PHASE_BY_STATUS: Final[dict[BudgetStatus, LoopPhase]] = {
    BudgetStatus.EXHAUSTED: LoopPhase.REACTIVE_REMEDIATION,
    BudgetStatus.BURNING: LoopPhase.REACTIVE_REMEDIATION,
    BudgetStatus.UNSPENT: LoopPhase.OBJECTIVE_REVIEW,
    BudgetStatus.HEALTHY: LoopPhase.PROACTIVE_ELEVATION,
    BudgetStatus.INSUFFICIENT_DATA: LoopPhase.PROACTIVE_ELEVATION,
}
_STATUS_PRECEDENCE: Final[tuple[BudgetStatus, ...]] = (
    BudgetStatus.EXHAUSTED,
    BudgetStatus.BURNING,
    BudgetStatus.HEALTHY,
    BudgetStatus.INSUFFICIENT_DATA,
    BudgetStatus.UNSPENT,
)


def decide_phase(states: Sequence[BudgetState]) -> PolicyDecision:
    """Map error budget states to the phase the loop should occupy.

    Replaces the binary `health == 100.0` inversion. Spending budget is normal
    operation; exhausting it or burning through it faster than the window elapses
    freezes proactive work. Never spending any of it across a full window is its own
    finding: the objectives are too loose to steer.
    """
    if not states:
        return PolicyDecision(
            phase=LoopPhase.PROACTIVE_ELEVATION,
            driver="none",
            rationale="No objectives recorded yet; proceed with proactive elevation.",
        )
    ranked = sorted(states, key=lambda s: (_STATUS_PRECEDENCE.index(s.status), -s.burn_rate))
    worst = ranked[0]
    if states and all(state.status is BudgetStatus.UNSPENT for state in states):
        return PolicyDecision(
            phase=LoopPhase.OBJECTIVE_REVIEW,
            driver="all objectives",
            rationale=(
                "Every budget closed a full window unspent. The objectives are not steering "
                "anything: tighten the targets, or take the risk they were reserving."
            ),
        )
    return PolicyDecision(
        phase=_PHASE_BY_STATUS[worst.status],
        driver=worst.objective.sli,
        rationale=(
            f"{worst.objective.sli} observed {worst.observed:.3f} against a {worst.objective.target:.3f} "
            f"target over {worst.iterations} iteration(s): {worst.consumed:.0%} of budget spent, "
            f"burn rate {worst.burn_rate:.2f}x. {worst.objective.rationale}"
        ),
    )


def gating_states(states: Sequence[BudgetState]) -> list[BudgetState]:
    """Return only the objectives that describe whether the artifact is fit to ship."""
    return [state for state in states if state.objective.gating]


def render_status(states: Sequence[BudgetState], decision: PolicyDecision) -> list[str]:
    """Render the error budget table and the resulting phase decision."""
    header = f"{'OBJECTIVE':<22} {'OBSERVED':>9} {'TARGET':>7} {'BUDGET USED':>12} {'BURN':>7}  STATUS"
    rows = [
        f"{s.objective.sli:<22} {s.observed:>9.3f} {s.objective.target:>7.3f} "
        f"{s.consumed:>11.0%} {s.burn_rate:>6.2f}x  {s.status.value}"
        + ("" if s.objective.gating else "  (steering)")
        for s in states
    ]
    return [
        "=" * 78,
        "🎯 RELIABILITY OBJECTIVES — ERROR BUDGET STATE",
        "=" * 78,
        header,
        "-" * 78,
        *rows,
        "-" * 78,
        f"Phase: {decision.phase.value}  (driver: {decision.driver})",
        f"Rationale: {decision.rationale}",
        "=" * 78,
    ]


def _load_report(path: Path) -> dict[str, Any]:
    """Read an iteration report emitted by `resource_iteration_workbench.py --json`."""
    return json.loads(path.read_text(encoding="utf-8"))


def _handle_record(args: argparse.Namespace) -> int:
    """Measure one iteration report and append it to the history ledger."""
    history = record_iteration(_load_report(args.report), args.history)
    measurements = measure_report(_load_report(args.report))
    for name, measurement in measurements.items():
        print(f"{name:<22} {measurement.good_events:>4}/{measurement.valid_events:<4} = {measurement.ratio:.3f}")
    print(f"Recorded iteration {len(history)} to {args.history}")
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    """Report error budget state, the phase the loop should occupy, and the release verdict.

    The exit code reflects the gating objectives only. A steering objective can put the
    loop into remediation without blocking a release, which is the whole point of
    separating "what should we work on" from "is this fit to ship".
    """
    states = evaluate_all(load_history(args.history))
    decision = decide_phase(states)
    release = decide_phase(gating_states(states))
    if args.json:
        print(json.dumps({
            "objectives": [{**asdict(s), "objective": asdict(s.objective), "status": s.status.value} for s in states],
            "decision": {**asdict(decision), "phase": decision.phase.value},
        }, indent=2, default=str))
    else:
        print("\n".join(render_status(states, decision)))
        print(f"Release gate: {'BLOCKED by ' + release.driver if release.phase is LoopPhase.REACTIVE_REMEDIATION else 'clear'}")
    return 0 if release.phase is not LoopPhase.REACTIVE_REMEDIATION else 1


def _handle_policy(args: argparse.Namespace) -> int:
    """Emit only the phase decision, for agents and CI to branch on."""
    states = evaluate_all(load_history(args.history))
    print(decide_phase(states).phase.value)
    return 0 if decide_phase(gating_states(states)).phase is not LoopPhase.REACTIVE_REMEDIATION else 1


_COMMANDS: Final[dict[str, Callable[[argparse.Namespace], int]]] = {
    "record": _handle_record,
    "status": _handle_status,
    "policy": _handle_policy,
}


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser for the reliability SLO engine."""
    parser = argparse.ArgumentParser(description="Reliability SLO & error budget engine")
    parser.add_argument("command", choices=sorted(_COMMANDS), help="Action to perform")
    parser.add_argument("report", nargs="?", type=Path, help="Iteration report JSON (for 'record')")
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY_PATH, help="History ledger path")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI runner for the reliability SLO engine."""
    parser = build_arg_parser()
    args = parser.parse_args(list(argv[1:]) if argv is not None else None)
    if args.command == "record" and args.report is None:
        parser.error("'record' requires a report path emitted by the workbench --json")
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
