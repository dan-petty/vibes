#!/usr/bin/env python3
"""Chaos Invariant Injector & Agent Resilience Benchmark.

Injects controlled, reversible mechanical mutations (complexity spikes,
nesting invasions, tuple assertion desync, and mock egress leaks) into
codebases to benchmark autonomous agent self-healing convergence.
"""

# sentinel: allow[ZeroTrustSanitization] — chaos mutation testing egress sentinel detection

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

SCHEMA_SARIF_VERSION: Final[str] = "2.1.0"
SCHEMA_SARIF_URI: Final[str] = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
)

RULE_CODES: Final[dict[str, str]] = {
    "COMPLEXITY_SPIKE": "CHAOS001",
    "NESTING_SPIKE": "CHAOS002",
    "ASSERTION_DESYNC": "CHAOS003",
    "EGRESS_POISON": "CHAOS004",
    "TRANSIENT_FAULT": "CHAOS005",
}


@dataclass(frozen=True)
class ChaosMutation:
    """Record of an active chaos mutation injected into a file."""

    mutation_id: str
    mutation_type: str
    target_path: str
    original_content: str
    mutated_content: str
    injected_at: float


@dataclass
class ChaosBenchmarkReport:
    """Consolidated report evaluating agentic self-healing resilience."""

    total_injected: int = 0
    total_detected: int = 0
    total_repaired: int = 0
    repair_ratio: float = 0.0
    mutations: list[ChaosMutation] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)


def inject_complexity_spike(source: str) -> str:
    """Inject branching ladder forcing cyclomatic complexity M > 10."""
    ladder = (
        "\n    # Chaos Mutation: Complexity Spike\n"
        "    if x == 1: pass\n"
        "    elif x == 2: pass\n"
        "    elif x == 3: pass\n"
        "    elif x == 4: pass\n"
        "    elif x == 5: pass\n"
        "    elif x == 6: pass\n"
        "    elif x == 7: pass\n"
        "    elif x == 8: pass\n"
        "    elif x == 9: pass\n"
        "    elif x == 10: pass\n"
    )
    if "def " in source:
        lines = source.splitlines(keepends=True)
        for idx, line in enumerate(lines):
            if line.strip().startswith("def "):
                lines.insert(idx + 1, ladder)
                return "".join(lines)
    return source + "\ndef _chaos_spike(x: int) -> None:" + ladder


def inject_nesting_spike(source: str) -> str:
    """Inject deep block indentation exceeding depth > 5."""
    nested = (
        "\n    # Chaos Mutation: Nesting Depth Spike\n"
        "    if True:\n"
        "        if True:\n"
        "            if True:\n"
        "                if True:\n"
        "                    if True:\n"
        "                        if True:\n"
        "                            pass\n"
    )
    return source + "\ndef _chaos_nesting() -> None:" + nested


def inject_assertion_desync(source: str) -> str:
    """Splinter consolidated tuple assertions into sprawling linear asserts."""
    sprawl = (
        "\n    # Chaos Mutation: Assertion Desynchronization\n"
        "    assert val_a == 1\n"
        "    assert val_b == 2\n"
        "    assert val_c == 3\n"
        "    assert val_d == 4\n"
        "    assert val_e == 5\n"
        "    assert val_f == 6\n"
    )
    return source + sprawl


def inject_egress_poison(source: str) -> str:
    """Inject simulated RFC 1918 private network host string to test egress sentinels."""
    leak = '\n# Chaos Mutation: Egress Leak\nMOCK_INTERNAL_GATEWAY = "http://192.168.1.1:8080/api"\n'
    return source + leak


def inject_transient_fault() -> dict[str, Any]:
    """Simulate HTTP 429 rate-limit response envelope."""
    return {
        "status_code": 429,
        "headers": {"Retry-After": "2.0"},
        "error": "Rate limit exceeded. Please back off.",
    }


def apply_chaos_mutation(
    target_file: Path,
    mutation_type: str,
) -> ChaosMutation:
    """Apply a selected mutation to target file and return record for rollback."""
    original = target_file.read_text(encoding="utf-8") if target_file.exists() else ""
    mutators = {
        "COMPLEXITY_SPIKE": inject_complexity_spike,
        "NESTING_SPIKE": inject_nesting_spike,
        "ASSERTION_DESYNC": inject_assertion_desync,
        "EGRESS_POISON": inject_egress_poison,
    }
    mutator_fn = mutators.get(mutation_type, inject_complexity_spike)
    mutated = mutator_fn(original)

    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(mutated, encoding="utf-8")

    return ChaosMutation(
        mutation_id=f"mut-{mutation_type.lower()}-{int(time.time())}",
        mutation_type=mutation_type,
        target_path=str(target_file.resolve()),
        original_content=original,
        mutated_content=mutated,
        injected_at=time.time(),
    )


def rollback_chaos_mutation(mutation: ChaosMutation) -> bool:
    """Revert mutated file back to its original pre-chaos state."""
    target = Path(mutation.target_path)
    try:
        if mutation.original_content:
            target.write_text(mutation.original_content, encoding="utf-8")
        elif target.exists():
            target.unlink()
        return True
    except OSError:
        return False


def evaluate_resilience(
    mutations: list[ChaosMutation],
    detected_count: int,
    repaired_count: int,
) -> ChaosBenchmarkReport:
    """Calculate resilience telemetry and repair ratio."""
    total = len(mutations)
    ratio = round(repaired_count / max(1, total), 4)
    findings = [
        {
            "rule_id": RULE_CODES.get(m.mutation_type, "CHAOS001"),
            "mutation_type": m.mutation_type,
            "target": m.target_path,
        }
        for m in mutations
    ]
    return ChaosBenchmarkReport(
        total_injected=total,
        total_detected=detected_count,
        total_repaired=repaired_count,
        repair_ratio=ratio,
        mutations=mutations,
        findings=findings,
    )


def to_sarif(report: ChaosBenchmarkReport) -> dict[str, Any]:
    """Export chaos findings to OASIS SARIF 2.1.0."""
    rules = [
        {
            "id": code,
            "name": name,
            "shortDescription": {"text": f"Chaos invariant injected: {name}"},
        }
        for name, code in RULE_CODES.items()
    ]
    results = [
        {
            "ruleId": f["rule_id"],
            "level": "warning",
            "message": {"text": f"Active chaos mutation '{f['mutation_type']}' on {f['target']}"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f["target"]},
                        "region": {"startLine": 1, "startColumn": 1},
                    }
                }
            ],
        }
        for f in report.findings
    ]
    return {
        "$schema": SCHEMA_SARIF_URI,
        "version": SCHEMA_SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "chaos-invariant-monkey",
                        "version": "1.0.0",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def to_markdown(report: ChaosBenchmarkReport) -> str:
    """Format benchmark report as Markdown summary."""
    lines = [
        "# Chaos Invariant Resilience Benchmark Report",
        "",
        f"- **Total Injected**: {report.total_injected}",
        f"- **Total Detected**: {report.total_detected}",
        f"- **Total Repaired**: {report.total_repaired}",
        f"- **Repair Ratio ($R_{{repair}}$)**: {report.repair_ratio * 100:.1f}%",
        "",
        "## Injected Mutations",
        "",
        "| Mutation ID | Type | Target |",
        "|---|---|---|",
    ]
    for m in report.mutations:
        lines.append(f"| {m.mutation_id} | {m.mutation_type} | {Path(m.target_path).name} |")
    return "\n".join(lines) + "\n"


def parse_args(args: list[str]) -> argparse.Namespace:
    """Parse CLI options for chaos monkey."""
    parser = argparse.ArgumentParser(
        description="Chaos Invariant Injector & Agent Resilience Benchmark",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inj = sub.add_parser("inject", help="Inject chaos mutation into a file")
    inj.add_argument("target", type=Path, help="Target file path")
    inj.add_argument(
        "--type",
        choices=["COMPLEXITY_SPIKE", "NESTING_SPIKE", "ASSERTION_DESYNC", "EGRESS_POISON"],
        default="COMPLEXITY_SPIKE",
        help="Type of invariant mutation",
    )

    bench = sub.add_parser("benchmark", help="Run simulated chaos benchmark")
    bench.add_argument("--format", choices=["text", "json", "markdown", "sarif"], default="text")

    return parser.parse_args(args)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for standalone execution."""
    opts = parse_args(argv or sys.argv[1:])
    if opts.command == "inject":
        mut = apply_chaos_mutation(opts.target, opts.type)
        print(f"Injected {mut.mutation_type} -> {opts.target} (ID: {mut.mutation_id})")
        return 0

    dummy_mut = ChaosMutation("mut-demo", "COMPLEXITY_SPIKE", "demo.py", "x=1", "x=1\nif x==1:pass", time.time())
    report = evaluate_resilience([dummy_mut], 1, 1)

    if opts.format == "json":
        print(json.dumps(asdict(report), indent=2))
    elif opts.format == "sarif":
        print(json.dumps(to_sarif(report), indent=2))
    elif opts.format == "markdown":
        print(to_markdown(report))
    else:
        print(f"Chaos Benchmark: Repair Ratio = {report.repair_ratio * 100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
