#!/usr/bin/env python3
"""Accept a codebase's existing findings so a gate can be adopted before it passes.

Selected from [`docs/landscape/SURVEY.md`](../docs/landscape/SURVEY.md): `per_file_baseline`
and `baseline_diff` are each missing from two of this repository's capabilities and held by
four surveyed projects — `terryyin/lizard` (96.7, Mature) with `-W`, `jendrikseipp/vulture`
(93.3) with whitelist files, `tonybaloney/wily` (92.6) with `wily diff`, and
`promptfoo/promptfoo` (95.1). It is the most-cited gap in the survey.

The problem it solves is adoption, not tolerance. A gate pointed at a codebase that does
not pass yet reports hundreds of findings at once, which is indistinguishable from reporting
nothing: no one reads it and no one can tell which finding arrived with the change in front
of them. A baseline records what was already there so the gate reports only what is new.

Three decisions worth knowing:

* **The baseline lives at the normalization layer, not inside one oracle.** Every oracle's
  findings already become `sarif_report.Result` objects with a stable fingerprint, so
  baselining them once gives the behaviour to all five at no further cost — and gives one
  format to review rather than a whitelist syntax per tool.
* **Fingerprints exclude the line number, deliberately.** An import added above a defect
  does not make it a new defect. This is the same identity SARIF uses to decide whether to
  re-alert, so a baseline entry and a code scanning alert agree about what "the same
  finding" means.
* **A baseline that never shrinks is an amnesty.** `status` reports entries whose finding
  is no longer produced, and `prune` removes them. Without that step a whitelist keeps
  suppressing a defect that was fixed and later reintroduced — which is the failure mode
  that makes long-lived suppression files worse than no gate at all.

This repository's own baseline is empty and `tests/test_finding_baseline.py` asserts that it
stays that way. The architectural invariants in §10 are non-negotiable; the machinery exists
so that *other* codebases can adopt these gates incrementally, never so that this one can
defer a violation.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

DEFAULT_BASELINE: Final[Path] = Path("artifacts/finding-baseline.json")
BASELINE_VERSION: Final[int] = 1


class Finding(Protocol):
    """The shape a baseline needs, satisfied by `sarif_report.Result`.

    Declared structurally so this module imports nothing from the reporter, which imports
    this one. The alternative — a shared types module — would have been a third file whose
    only content is one dataclass.
    """

    # Declared read-only. Plain annotations would demand *settable* attributes, which no
    # frozen dataclass provides — and every finding type here is frozen, precisely so an
    # identity cannot be mutated after it has been compared against a baseline.
    @property
    def rule_id(self) -> str:
        """Return the rule that produced the finding."""

    @property
    def file_path(self) -> str:
        """Return the file the finding is reported against."""

    @property
    def subject(self) -> str:
        """Return the finding's subject, which participates in its identity."""

    def fingerprint(self, root: Path | None = None) -> str:
        """Return the finding's stable identity, normalised against a repository root.

        `root` is what keeps the identity independent of where the checkout lives. Without
        it a baseline recorded from an absolute path suppressed nothing when CI scanned the
        same tree as `.`.
        """


@dataclass(frozen=True)
class BaselineEntry:
    """One accepted finding, with enough context to review it in a diff."""

    fingerprint: str
    rule_id: str
    file_path: str
    subject: str
    first_seen: str

    def to_json(self) -> dict[str, Any]:
        """Render for the committed baseline file."""
        return {
            "fingerprint": self.fingerprint,
            "rule": self.rule_id,
            "file": self.file_path,
            "subject": self.subject,
            "first_seen": self.first_seen,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> BaselineEntry:
        """Parse one entry, tolerating a missing subject on a hand-edited file."""
        return cls(
            fingerprint=str(payload["fingerprint"]),
            rule_id=str(payload.get("rule", "")),
            file_path=str(payload.get("file", "")),
            subject=str(payload.get("subject", "")),
            first_seen=str(payload.get("first_seen", "")),
        )


@dataclass
class Partition:
    """Current findings split against a baseline."""

    new: list[Any]
    known: list[Any]
    resolved: list[BaselineEntry]

    def is_stale(self) -> bool:
        """Report whether the baseline suppresses findings nothing produces any more."""
        return bool(self.resolved)


def load_baseline(path: Path) -> dict[str, BaselineEntry]:
    """Read a baseline, returning empty when the file does not exist.

    A missing baseline is the normal state and means "accept nothing", so it is not an
    error. A malformed one is, because silently treating it as empty would turn every
    accepted finding back into a failure with no explanation.
    """
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = [BaselineEntry.from_json(item) for item in payload.get("findings", [])]
    return {entry.fingerprint: entry for entry in entries}


def save_baseline(
    path: Path, findings: Iterable[Finding], recorded_at: str | None = None,
    root: Path | None = None,
) -> int:
    """Write a baseline from the current findings, sorted so re-recording diffs cleanly.

    `root` normalises each fingerprint's path to the same repository-relative URI the
    SARIF result carries. Without it a baseline recorded from an absolute checkout path
    suppressed nothing when CI scanned the same tree as `.`, which is the one job it has.
    """
    stamp = recorded_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = [
        BaselineEntry(f.fingerprint(root), f.rule_id, f.file_path, f.subject, stamp)
        for f in findings
    ]
    unique = {entry.fingerprint: entry for entry in entries}
    ordered = sorted(unique.values(), key=lambda e: (e.rule_id, e.file_path, e.fingerprint))
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": BASELINE_VERSION,
        "recorded_at": stamp,
        "findings": [entry.to_json() for entry in ordered],
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return len(ordered)


def partition(
    findings: Sequence[Finding], baseline: dict[str, BaselineEntry], root: Path | None = None
) -> Partition:
    """Split current findings into new and already-accepted, and report what has been fixed."""
    seen: set[str] = set()
    new: list[Any] = []
    known: list[Any] = []
    for finding in findings:
        digest = finding.fingerprint(root)
        seen.add(digest)
        (known if digest in baseline else new).append(finding)
    resolved = [entry for digest, entry in sorted(baseline.items()) if digest not in seen]
    return Partition(new=new, known=known, resolved=resolved)


def prune(path: Path, resolved: Sequence[BaselineEntry]) -> int:
    """Drop entries whose finding is no longer produced, and report how many went.

    This is the step a suppression file usually lacks. An entry kept after its finding was
    fixed goes on suppressing the same finding if it is ever reintroduced, so the gate
    stops covering code it once covered without anything saying so.
    """
    if not resolved:
        return 0
    stale = {entry.fingerprint for entry in resolved}
    baseline = load_baseline(path)
    kept = [entry for digest, entry in baseline.items() if digest not in stale]
    document = {
        "version": BASELINE_VERSION,
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "findings": [
            entry.to_json()
            for entry in sorted(kept, key=lambda e: (e.rule_id, e.file_path, e.fingerprint))
        ],
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return len(stale)


# --- CLI ------------------------------------------------------------------------------
#
# The CLI reads a SARIF log rather than calling the oracles. Collecting findings here meant
# importing the reporter, which imports this module for its `--baseline` flag, and the smell
# quantifier flagged the cycle immediately. Reading the standard format instead removes the
# dependency in both directions and makes the baseline usable against any SARIF-emitting
# tool, not only the five in this repository.


@dataclass(frozen=True)
class SarifFinding:
    """One result read back out of a SARIF log, in the shape a baseline needs."""

    rule_id: str
    file_path: str
    subject: str
    digest: str

    def fingerprint(self, root: Path | None = None) -> str:
        """Return the identity the emitting tool already computed.

        `root` is accepted and ignored: this finding was read back out of a SARIF log, where
        the producer has already normalised the path and written the digest. Recomputing it
        here would be a second opinion about an identity that is not ours to assign.
        """
        return self.digest


def read_sarif(path: Path) -> list[SarifFinding]:
    """Parse a SARIF log into findings, keyed by the fingerprint its producer assigned.

    A result without a `partialFingerprints` entry is skipped rather than assigned a
    computed identity: a fingerprint this module invented would not match the one the
    producing tool uses, so the baseline would suppress nothing and say it had.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    found: list[SarifFinding] = []
    for run in payload.get("runs", []):
        found.extend(_findings_in_run(run))
    return found


def _findings_in_run(run: dict[str, Any]) -> list[SarifFinding]:
    """Extract every fingerprinted result from one SARIF run."""
    found: list[SarifFinding] = []
    for result in run.get("results", []):
        digests = list(result.get("partialFingerprints", {}).values())
        if not digests:
            continue
        found.append(
            SarifFinding(
                rule_id=str(result.get("ruleId", "")),
                file_path=_result_uri(result),
                subject=str(result.get("message", {}).get("text", ""))[:80],
                digest=str(digests[0]),
            )
        )
    return found


def _result_uri(result: dict[str, Any]) -> str:
    """Return a result's artifact URI, or empty when it carries no location."""
    for location in result.get("locations", []):
        artifact = location.get("physicalLocation", {}).get("artifactLocation", {})
        if "uri" in artifact:
            return str(artifact["uri"])
    return ""


def _handle_record(args: argparse.Namespace) -> int:
    """Accept every finding in the supplied log."""
    written = save_baseline(args.baseline, read_sarif(args.source))
    print(f"Recorded {written} finding(s) from {args.source} into {args.baseline}.")
    print("Review this file in the diff: every entry is a defect the gate will stop reporting.")
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    """Report how the baseline stands against the findings in the supplied log."""
    baseline = load_baseline(args.baseline)
    split = partition(read_sarif(args.source), baseline)
    print(f"Baseline {args.baseline}: {len(baseline)} accepted finding(s).")
    print(f"  new       {len(split.new):>4}  reported by the gate")
    print(f"  known     {len(split.known):>4}  suppressed by the baseline")
    print(f"  resolved  {len(split.resolved):>4}  fixed since recording; prune them")
    for entry in split.resolved[:10]:
        print(f"    stale: {entry.rule_id} {entry.file_path} {entry.subject[:50]}")
    # Exits non-zero only for a stale baseline, and only when asked. Whether new findings
    # should fail a build is the gate's decision, not the baseline's -- `sarif_report
    # --fail-on-error` owns that, and duplicating it here would give two commands that
    # disagree about the same question the moment one of them changes.
    return 1 if (args.strict and split.is_stale()) else 0


def _handle_prune(args: argparse.Namespace) -> int:
    """Remove entries whose finding is no longer produced."""
    split = partition(read_sarif(args.source), load_baseline(args.baseline))
    print(f"Pruned {prune(args.baseline, split.resolved)} resolved entry(ies) from {args.baseline}.")
    return 0


_HANDLERS: Final[dict[str, Any]] = {
    "record": _handle_record,
    "status": _handle_status,
    "prune": _handle_prune,
}


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser for baseline management."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=sorted(_HANDLERS))
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(".data/findings.sarif"),
        help="SARIF log to read findings from (produced by tools/sarif_report.py)",
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when the baseline holds entries nothing produces any more",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for baseline management."""
    args = build_arg_parser().parse_args(argv)
    return int(_HANDLERS[args.command](args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
