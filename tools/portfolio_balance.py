#!/usr/bin/env python3
"""Measure what this repository builds against what it merely judges.

Every work generator here answers a question about existing work. [Observation 14](../observations/systems/14-defect-shaped-loops-and-the-feature-blind-spot.md)
named that and roadmap ingestion was the fix; the landscape survey was then added to look
outward. But the survey's manifest listed four capabilities and all four were analysis
tools, so every gap it could emit was a linter feature and twelve of sixteen sample
applications were invisible to it. **The correction for the blind spot inherited the blind
spot**, and being citation-backed made it read as rigour.

That is not visible from inside any single change. It is only visible as a ratio, so this
module reports two:

* **Portfolio** — what the repository *has*, from the `kind` each capability declares in
  [`docs/landscape/capabilities.yaml`](../docs/landscape/capabilities.yaml).
* **Investment** — what recent work *touched*, counted from git over a window of commits.

Both are mechanical. `kind` is declared rather than inferred for the same reason the
self-hardening audit stopped grepping prose for five keywords: a classifier that guesses
reports whatever its word list happens to catch, and a portfolio measurement that is wrong
is worse than none because it reads as evidence.

This steers and never gates. A repository legitimately spends a whole release on tooling;
what it must not do is spend one without noticing. §11 keeps defect indicators gating and
everything else steering, and a balance ratio is as steering as a signal gets.
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

DEFAULT_MANIFEST: Final[Path] = Path("docs/landscape/capabilities.yaml")
DEFAULT_WINDOW: Final[int] = 20

# Where a touched file counts. Anything outside these prefixes is narrative or
# configuration and is reported separately rather than folded into either side.
CAPABILITY_PREFIXES: Final[tuple[str, ...]] = ("examples/",)
QUALITY_PREFIXES: Final[tuple[str, ...]] = ("tools/",)


@dataclass
class Balance:
    """One measurement of what the repository holds or where it spent its effort."""

    label: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Return every counted unit."""
        return sum(self.counts.values())

    def share(self, kind: str) -> float:
        """Return one kind's share, or 0.0 when nothing was counted."""
        return round(self.counts.get(kind, 0) / self.total, 3) if self.total else 0.0

    def to_json(self) -> dict[str, Any]:
        """Render for the loop and for CI."""
        return {
            "label": self.label,
            "counts": dict(sorted(self.counts.items())),
            "total": self.total,
            "capability_share": self.share("capability"),
        }


def portfolio(manifest_path: Path) -> Balance:
    """Count declared capabilities by kind.

    An undeclared `kind` is counted as `undeclared` rather than guessed. A capability
    nobody classified is a real state worth seeing, and inventing a class for it would
    make the ratio agree with whatever the guess was.
    """
    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    kinds = [entry.get("kind", "undeclared") for entry in document.get("capabilities", [])]
    return Balance("portfolio", dict(collections.Counter(kinds)))


def _added_lines(window: int, root: Path) -> list[tuple[int, str]]:
    """Return (added lines, path) for every file the last `window` commits changed."""
    # Fixed argv, no shell, and the repository is the one being measured.
    proc = subprocess.run(
        ["git", "log", f"-{window}", "--numstat", "--format="],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return [parsed for line in proc.stdout.splitlines() if (parsed := _numstat_row(line))]


def _numstat_row(line: str) -> tuple[int, str] | None:
    """Parse one `--numstat` row, skipping binaries, which report `-` instead of a count."""
    parts = line.split("\t")
    if len(parts) != 3 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[2]


def declared_kinds(manifest_path: Path) -> dict[str, str]:
    """Map each declared capability's path to its kind, longest path first.

    Sorted by length so the most specific declaration wins: `examples/` alone would
    classify the sentinel and the crawler identically, and they are opposite kinds.
    """
    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    declared = {
        str(entry["path"]).rstrip("/"): str(entry.get("kind", "undeclared"))
        for entry in document.get("capabilities", [])
        if entry.get("path")
    }
    return dict(sorted(declared.items(), key=lambda item: -len(item[0])))


def _classify_path(path: str, declared: dict[str, str] | None = None) -> str:
    """Return which side of the ledger a touched file falls on.

    The manifest decides where it can. Location alone gets this wrong in both directions:
    `tools/app_factory.py` builds applications and `examples/ast-invariant-sentinel/` judges
    them, so the first version reported building the factory as *quality* investment and
    moved the ratio the wrong way — an instrument disagreeing with the thing it measures
    on the very change that was made to correct it.

    Location remains the fallback, because most files are not a declared capability and
    dropping them would leave the ratio measuring a handful of paths.
    """
    for prefix, kind in (declared or {}).items():
        if path == prefix or path.startswith(prefix + "/"):
            return kind
    if path.startswith(CAPABILITY_PREFIXES):
        return "capability"
    if path.startswith(QUALITY_PREFIXES):
        return "quality"
    return "other"


def investment(window: int, root: Path, manifest_path: Path | None = None) -> Balance:
    """Count lines added per area over a window of commits.

    Counts added lines, not touched files. The first version of this function counted
    touches and reported a comfortable 52/48 for a window in which no application
    capability was developed at all: one repository-wide lint sweep touched 29 files under
    `examples/`, and a measure that counts locations cannot tell a sweep from a build.
    Lines can — the same window is 350 added under `examples/` against 4,584 under
    `tools/`, which is 7% against 93%.

    It remains a proxy. Lines are not value, and a careful refactor can shrink a file it
    improves. What it is good for is the question that prompted it: whether a stretch of
    work touched one side of the repository and not the other.
    """
    declared = declared_kinds(manifest_path) if manifest_path else {}
    tally: dict[str, int] = collections.Counter()
    for added, path in _added_lines(window, root):
        tally[_classify_path(path, declared)] += added
    counted = {kind: total for kind, total in tally.items() if kind != "other"}
    return Balance(f"investment/{window} (lines added)", counted)


def render(measurements: list[Balance]) -> list[str]:
    """Render the measurements for a terminal reader."""
    lines: list[str] = []
    for measure in measurements:
        lines.append(f"{measure.label}: {measure.total} unit(s)")
        for kind, count in sorted(measure.counts.items()):
            bar = "█" * round(20 * count / measure.total) if measure.total else ""
            lines.append(f"  {kind:<12} {count:>4}  {measure.share(kind):>5.0%} {bar}")
    return lines


def _handle_report(args: argparse.Namespace) -> int:
    """Report both ratios, and say when recent work touched no capability at all."""
    measurements = [portfolio(args.manifest), investment(args.window, args.root, args.manifest)]
    if args.json:
        print(json.dumps([m.to_json() for m in measurements], indent=2))
        return 0
    for line in render(measurements):
        print(line)
    spent = measurements[1]
    if spent.total and spent.counts.get("capability", 0) == 0:
        print(
            f"\n⚠️  No capability work in the last {args.window} commit(s). "
            "A release spent entirely on tooling is a choice; spending one without "
            "noticing is the drift this measurement exists to surface."
        )
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser for the balance report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    parser.add_argument("--json", action="store_true", help="Emit the measurements as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the portfolio balance report."""
    return _handle_report(build_arg_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
