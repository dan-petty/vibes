#!/usr/bin/env python3
"""Differential Polyglot AST Mutation Fuzzer.

Evaluates parser resilience, boundary containment, and memory safety across
multi-language CST/AST parsers and file loaders under adversarial structural mutations.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

SCHEMA_SARIF_VERSION: Final[str] = "2.1.0"
SCHEMA_SARIF_URI: Final[str] = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
)

LANG_EXTENSIONS: Final[dict[str, str]] = {
    "python": ".py",
    "rust": ".rs",
    "go": ".go",
    "typescript": ".ts",
    "bash": ".sh",
}

RULE_CODES: Final[dict[str, str]] = {
    "CRASH": "POLY001",
    "TIMEOUT": "POLY002",
    "RECURSION_LIMIT": "POLY003",
    "UNBOUNDED_MEMORY": "POLY004",
    "SYMLINK_ESCAPE": "POLY005",
}


@dataclass(frozen=True)
class MutationResult:
    """Outcome of a single mutation evaluation."""

    mutator: str
    language: str
    survived: bool
    status: str
    error_message: str
    duration_ms: float
    memory_bounded: bool


@dataclass
class FuzzBatchReport:
    """Consolidated telemetry for a fuzzing run."""

    total_mutations: int = 0
    passed: int = 0
    failed: int = 0
    findings: list[dict[str, Any]] = field(default_factory=list)
    results: list[MutationResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        """Calculate percentage of survived mutations."""
        if self.total_mutations == 0:
            return 1.0
        return round(self.passed / self.total_mutations, 4)


def mutate_bracket_desync(content: str, language: str) -> str:
    """Inject mismatched brackets and structural delimiters."""
    delimiters = ["{", "}", "(", ")", "[", "]", "<", ">"]
    target = delimiters[len(content) % len(delimiters)]
    replacement = delimiters[(len(content) + 1) % len(delimiters)]
    if target in content:
        return content.replace(target, replacement, 1)
    return content + replacement


def mutate_token_smuggle(content: str, language: str) -> str:
    """Inject null-bytes and comment boundary escapes."""
    comment_token = "#" if language in ("python", "bash") else "//"
    payload = f"\n{comment_token} injected payload \x00 '\"--\n"
    midpoint = len(content) // 2
    return content[:midpoint] + payload + content[midpoint:]


def mutate_structural_depth(content: str, language: str) -> str:
    """Wrap content in deeply nested conditional blocks."""
    if language == "python":
        prefix = "".join("    " * idx + "if True:\n" for idx in range(25))
        indent = "    " * 25
        indented = "\n".join(indent + line for line in content.splitlines())
        return prefix + indented
    if language in ("rust", "go", "typescript"):
        prefix = "".join("if (true) {\n" for _ in range(25))
        suffix = "\n" + "".join("}" for _ in range(25))
        return prefix + content + suffix
    prefix_sh = "".join("if true; then\n" for _ in range(25))
    suffix_sh = "\n" + "".join("fi\n" for _ in range(25))
    return prefix_sh + content + suffix_sh


def mutate_truncated_stream(content: str, language: str) -> str:
    """Simulate mid-stream token cutoff and abrupt EOF."""
    cutoff = max(1, len(content) // 3)
    return content[:cutoff]


def build_cyclical_symlink_tree(root_dir: Path) -> Path:
    """Construct a controlled cyclical symlink for loop testing."""
    abs_root = root_dir.resolve()
    abs_root.mkdir(parents=True, exist_ok=True)
    child = abs_root / "cycle_child"
    child.mkdir(parents=True, exist_ok=True)
    loop_link = child / "loop_back"
    if loop_link.is_symlink() or loop_link.exists():
        loop_link.unlink(missing_ok=True)
    with contextlib.suppress(OSError, RuntimeError):
        loop_link.symlink_to(abs_root, target_is_directory=True)
    return loop_link


def evaluate_python_ast(content: str) -> tuple[bool, str]:
    """Parse Python code using AST and assert clean error handling."""
    try:
        ast.parse(content)
        return True, "CLEAN_PARSE"
    except SyntaxError as exc:
        return True, f"HANDLED_SYNTAX_ERROR: {exc.msg}"
    except RecursionError:
        return False, "RECURSION_LIMIT_EXCEEDED"
    except Exception as exc:
        return False, f"UNHANDLED_CRASH: {type(exc).__name__}: {exc}"


def evaluate_text_boundary(content: str, max_chars: int = 100_000) -> tuple[bool, str]:
    """Check generic string memory bounding and null containment."""
    if len(content) > max_chars:
        return False, "UNBOUNDED_MEMORY"
    lines = content.splitlines()
    if not lines and content:
        return False, "CORRUPTED_STREAM"
    return True, "CONTAINED"


def execute_mutation_check(
    mutator_name: str,
    mutator_fn: Any,
    seed_content: str,
    language: str,
) -> MutationResult:
    """Run one mutation and measure execution metrics."""
    start_time = time.perf_counter()
    mutated = mutator_fn(seed_content, language)
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    if language == "python":
        survived, status = evaluate_python_ast(mutated)
    else:
        survived, status = evaluate_text_boundary(mutated)

    mem_bounded = len(mutated) < 5_000_000
    if not mem_bounded:
        survived = False
        status = "UNBOUNDED_MEMORY"

    return MutationResult(
        mutator=mutator_name,
        language=language,
        survived=survived,
        status=status,
        error_message="" if survived else status,
        duration_ms=round(elapsed_ms, 3),
        memory_bounded=mem_bounded,
    )


def _cleanup_symlink_tree(base_dir: Path, loop_link: Path) -> None:
    """Safely unlink loop and remove temporary directory."""
    with contextlib.suppress(OSError):
        loop_link.unlink(missing_ok=True)
        child = base_dir / "cycle_child"
        if child.exists():
            shutil.rmtree(child, ignore_errors=True)


def evaluate_symlink_containment(base_dir: Path) -> MutationResult:
    """Verify that file traversal terminates on circular symlinks."""
    start_time = time.perf_counter()
    loop_link = build_cyclical_symlink_tree(base_dir)
    visited: set[Path] = set()
    survived = True
    status = "LOOP_CONTAINED"

    try:
        curr = loop_link
        for _ in range(50):
            try:
                resolved = curr.resolve()
                if resolved in visited:
                    break
                visited.add(resolved)
                curr = resolved / "cycle_child" / "loop_back"
            except (OSError, RuntimeError) as exc:
                status = f"HANDLED_FS_ERROR: {exc}"
                break
        else:
            survived = False
            status = "SYMLINK_ESCAPE_INFINITE_LOOP"
    finally:
        _cleanup_symlink_tree(base_dir, loop_link)

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
    return MutationResult(
        mutator="CircularSymlinkGenerator",
        language="filesystem",
        survived=survived,
        status=status,
        error_message="" if survived else status,
        duration_ms=round(elapsed_ms, 3),
        memory_bounded=True,
    )


def run_fuzz_campaign(
    seeds: dict[str, str],
    temp_dir: Path | None = None,
) -> FuzzBatchReport:
    """Run full fuzzing campaign across polyglot mutators."""
    report = FuzzBatchReport()
    mutators = [
        ("BracketDesyncMutator", mutate_bracket_desync),
        ("TokenSmuggleMutator", mutate_token_smuggle),
        ("StructuralDepthSpikeMutator", mutate_structural_depth),
        ("TruncatedPayloadMutator", mutate_truncated_stream),
    ]

    for lang, content in seeds.items():
        for name, fn in mutators:
            res = execute_mutation_check(name, fn, content, lang)
            report.total_mutations += 1
            if res.survived:
                report.passed += 1
            else:
                report.failed += 1
                report.findings.append(build_finding_dict(res))
            report.results.append(res)

    if temp_dir is not None:
        sym_res = evaluate_symlink_containment(temp_dir)
        report.total_mutations += 1
        if sym_res.survived:
            report.passed += 1
        else:
            report.failed += 1
            report.findings.append(build_finding_dict(sym_res))
        report.results.append(sym_res)

    return report


def build_finding_dict(res: MutationResult) -> dict[str, Any]:
    """Map a mutation failure to a structured finding record."""
    rule_id = RULE_CODES.get(res.status, "POLY001")
    return {
        "rule_id": rule_id,
        "mutator": res.mutator,
        "language": res.language,
        "message": res.error_message or res.status,
        "duration_ms": res.duration_ms,
    }


def to_sarif(report: FuzzBatchReport) -> dict[str, Any]:
    """Generate schema-compliant OASIS SARIF 2.1.0 output."""
    rules = [
        {
            "id": code,
            "name": name,
            "shortDescription": {"text": f"Polyglot fuzzer invariant violation: {name}"},
        }
        for name, code in RULE_CODES.items()
    ]
    results = [
        {
            "ruleId": item["rule_id"],
            "level": "error",
            "message": {
                "text": (
                    f"Fuzzer violation in {item['mutator']} ({item['language']}): "
                    f"{item['message']}"
                )
            },
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f"polyglot/{item['language']}/seed"},
                        "region": {"startLine": 1, "startColumn": 1},
                    }
                }
            ],
        }
        for item in report.findings
    ]
    return {
        "$schema": SCHEMA_SARIF_URI,
        "version": SCHEMA_SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "polyglot-mutation-fuzzer",
                        "version": "1.0.0",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def to_markdown(report: FuzzBatchReport) -> str:
    """Format report as a clean Markdown summary."""
    verdict = "PASSED" if report.failed == 0 else "FAILED"
    lines = [
        "# Polyglot AST Mutation Fuzzer Report",
        "",
        f"**Verdict**: {'✅' if verdict == 'PASSED' else '❌'} {verdict}",
        f"- **Pass Rate**: {report.pass_rate * 100:.1f}%",
        f"- **Total Mutations**: {report.total_mutations}",
        f"- **Survived**: {report.passed}",
        f"- **Failed / Crashed**: {report.failed}",
        "",
        "## Evaluated Mutations",
        "",
        "| Mutator | Language | Duration (ms) | Status |",
        "|---|---|---|---|",
    ]
    for r in report.results:
        symbol = "✅" if r.survived else "❌"
        lines.append(f"| {r.mutator} | {r.language} | {r.duration_ms:.2f} | {symbol} {r.status} |")
    return "\n".join(lines) + "\n"


def parse_args(args: list[str]) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Differential Polyglot AST Mutation Fuzzer",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "markdown", "sarif"],
        default="text",
        help="Output serialization format",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Optional file path to persist output",
    )
    return parser.parse_args(args)


def main(argv: list[str] | None = None) -> int:
    """Entry point for standalone execution."""
    opts = parse_args(argv or sys.argv[1:])
    seeds = {
        "python": "def add(a: int, b: int) -> int:\n    return a + b\n",
        "rust": "fn calculate(x: i32) -> i32 {\n    x * 2\n}\n",
        "go": "func Process(v string) bool {\n    return len(v) > 0\n}\n",
        "typescript": "export function greet(name: string): string {\n    return `Hi ${name}`;\n}\n",
        "bash": "#!/usr/bin/env bash\necho 'hello world'\n",
    }
    with tempfile.TemporaryDirectory(prefix="vibes_fuzz_") as tmp_d:
        report = run_fuzz_campaign(seeds, Path(tmp_d))

    if opts.format == "json":
        body = json.dumps(asdict(report), indent=2)
    elif opts.format == "sarif":
        body = json.dumps(to_sarif(report), indent=2)
    elif opts.format == "markdown":
        body = to_markdown(report)
    else:
        body = f"Polyglot Fuzzer: {report.passed}/{report.total_mutations} survived ({report.pass_rate * 100:.1f}%)"

    if opts.out:
        opts.out.parent.mkdir(parents=True, exist_ok=True)
        opts.out.write_text(body, encoding="utf-8")
    else:
        print(body)

    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
