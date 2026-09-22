#!/usr/bin/env python3
"""Deterministic generation, mutation and shrinking of inputs for the instrument fuzzer.

Every instrument in this repository is a parser. `audit_file` parses Python, the
documentation validator parses CommonMark, the supply chain audit parses requirement
specifiers and workflow YAML — and each one is pointed at whatever happens to be in the
tree. The test suites feed them inputs an author thought of. Nothing has ever fed them an
input an author did not think of, which is the only kind that finds an unhandled branch.

This module supplies those inputs. Three properties matter more than volume:

* **Validity most of the time.** A generator that emits noise only ever reaches the first
  `except` in a parser. The generators here emit well-formed modules, documents and
  manifests, and the mutators then damage them, so the accepting path and the rejecting
  path are both reached.
* **Reproducibility.** A case carries the seed and the mutation that produced it, so a
  report names something that can be regenerated. The bytes are kept as well, because a
  seed only reproduces against the generator that was current when it ran, and the
  generator will change.
* **Shrinking.** An unshrunk failure is a four-hundred-line file nobody reads. Delta
  debugging reduces it to the lines that still fail, which is the difference between a
  corpus entry that serves as a regression test and one that serves as a souvenir.

The addresses in the generated corpus are deliberate. RFC 5737 documentation ranges
appear as literals; the private ranges §2 forbids are assembled from octets at runtime,
so the generator can drive the sanitization detector's positive path without this file
carrying the literal it exists to test for.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

CORPORA: Final[tuple[str, ...]] = ("python", "markdown", "roadmap", "requirements", "workflow")

# What a case is written to disk as. The suffix is load-bearing: several instruments
# select their parser from it, and `iter_source_files` filters on it.
SUFFIXES: Final[dict[str, str]] = {
    "python": ".py",
    "markdown": ".md",
    "roadmap": ".md",
    "requirements": ".toml",
    "workflow": ".yml",
}


@dataclass(frozen=True)
class Case:
    """One input, with the provenance needed to regenerate or replay it."""

    corpus: str
    text: str
    seed: int
    origin: str = "generated"

    def digest(self) -> str:
        """Return a content address, so the same input is never stored twice."""
        return hashlib.sha256(self.text.encode("utf-8", "surrogateescape")).hexdigest()[:16]

    def filename(self) -> str:
        """Return the corpus filename for this input.

        Deliberately not the corpus suffix. A stored case is a document that broke an
        instrument, so a corpus of `.md` files would be swept up by the documentation
        validator's whole-repository pass and a corpus of `.py` files by the sentinel,
        ruff and mypy — every fixture reported as a defect in the repository that keeps
        it. The suffix is restored when the case is written to scratch, where the parser
        actually needs it; on disk the corpus is inert.
        """
        return f"{self.digest()}.case"


# --- Python generation ----------------------------------------------------------------
#
# The templates below are formatted with `{n}`, never with a literal address. `192.0.2.x`
# is RFC 5737 documentation space and is allowed to appear here; the prohibited ranges are
# built by `_private_address` from octets so that no forbidden literal exists in this file.

_PY_LEAVES: Final[tuple[str, ...]] = (
    "value{n} = {n}",
    "total += {n}",
    "pass",
    "items{n} = [{n}, {n}]",
    'label{n} = "endpoint-{n}"',
    'host{n} = "192.0.2.{n}"',
    'note{n} = f"{{value{n}}} at example.com"',
    "del value{n}",
    "return {n}",
    "yield {n}",
)

_PY_BLOCK_HEADS: Final[tuple[str, ...]] = (
    "if value{n} > {n}:",
    "for index{n} in range({n}):",
    "while value{n} < 0:",
    "with open(\"file-{n}.txt\") as handle{n}:",
    "if not value{n}:",
)

_PY_SIGNATURES: Final[tuple[str, ...]] = (
    "def handler_{n}(value{n}: int = {n}) -> int:",
    "async def fetch_{n}(value{n}: int) -> int:",
    "def _private_{n}(*args: int, **kwargs: int) -> int:",
)


def _private_address(rng: random.Random) -> str:
    """Return an RFC 1918 address assembled at runtime rather than written as a literal.

    The sanitization detector's positive path is worth driving, and a generator that
    contained the literal would be flagged by the very instrument it is feeding.
    """
    first = rng.choice((10, 172, 192))
    second = {10: 0, 172: 16, 192: 168}[first]
    return f"{first}.{second}.{rng.randrange(256)}.{rng.randrange(1, 255)}"


def _py_block(rng: random.Random, depth: int, budget: int) -> list[str]:
    """Emit one indented block body, nesting at most `budget` levels further."""
    pad = "    " * depth
    lines = [pad + rng.choice(_PY_LEAVES).format(n=rng.randrange(100))]
    if budget > 0 and rng.random() < 0.6:
        lines.append(pad + rng.choice(_PY_BLOCK_HEADS).format(n=rng.randrange(100)))
        lines.extend(_py_block(rng, depth + 1, budget - 1))
    return lines


def _py_function(rng: random.Random, index: int) -> list[str]:
    """Emit one function definition, occasionally deep enough to breach the nesting cap."""
    signature = rng.choice(_PY_SIGNATURES).format(n=index)
    body = ['    """Generated."""', f"    total = {index}"]
    body.extend(_py_block(rng, 1, rng.randrange(0, 8)))
    body.append("    return total")
    return [signature, *body, ""]


# The functions above emit small, shapeless modules. Measured against the instruments they
# feed, that was not enough: 30 generated modules produced zero code-smell findings and
# left the refactorer's candidate detector empty in 24 of 30 cases. A corpus that never
# reaches an instrument's real work is a campaign that proves nothing, so the shapes below
# generate the structures those detectors exist to find.


def _shape_equality_ladder(rng: random.Random, index: int) -> list[str]:
    """Emit an `elif` ladder comparing one name against constants — the dispatch candidate."""
    lines = [f"def route_{index}(key: str) -> int:", '    """Generated ladder."""']
    keyword = "if"
    for branch in range(rng.randrange(4, 14)):
        lines.append(f'    {keyword} key == "case{branch}":')
        lines.append(f"        return {branch}")
        keyword = "elif"
    lines.append("    return -1")
    return [*lines, ""]


def _shape_boolean_storm(rng: random.Random, index: int) -> list[str]:
    """Emit one compound condition, which is complexity without nesting."""
    terms = " and ".join(f"arg{i} > {i}" for i in range(rng.randrange(3, 9)))
    guard = " or ".join((terms, "not arg0", f"arg1 != {index}"))
    params = ", ".join(f"arg{i}: int = {i}" for i in range(9))
    return [
        f"def gate_{index}({params}) -> bool:",
        '    """Generated compound predicate."""',
        f"    return bool({guard})",
        "",
    ]


def _shape_assert_chain(rng: random.Random, index: int) -> list[str]:
    """Emit the linear-assertion sprawl §10 tells agents to consolidate."""
    body = [f"    assert value{i} == {i}" for i in range(rng.randrange(4, 16))]
    setup = [f"    value{i} = {i}" for i in range(16)]
    return [f"def test_generated_{index}() -> None:", '    """Generated test."""', *setup, *body, ""]


def _shape_low_cohesion_class(rng: random.Random, index: int) -> list[str]:
    """Emit a class whose methods touch disjoint attributes, which is what LCOM4 counts."""
    groups = rng.randrange(2, 5)
    lines = [f"class Component{index}:", '    """Generated component."""', "", "    def __init__(self) -> None:"]
    lines.extend(f"        self.field{g} = {g}" for g in range(groups))
    for group in range(groups):
        lines.extend(
            [
                "",
                f"    def use_{group}(self) -> int:",
                '        """Generated accessor."""',
                f"        return self.field{group} + {group}",
            ]
        )
    return [*lines, ""]


def _shape_clone_pair(rng: random.Random, index: int) -> list[str]:
    """Emit two functions with identical bodies, which is what clone detection counts."""
    body = [f"    accumulator = {rng.randrange(100)}", "    for step in range(12):", "        accumulator += step * 3", "        accumulator -= step // 2", "    return accumulator"]
    first = [f"def compute_{index}_a() -> int:", '    """Generated clone."""', *body, ""]
    second = [f"def compute_{index}_b() -> int:", '    """Generated clone."""', *body, ""]
    return first + second


_PY_SHAPES: Final[tuple[Callable[[random.Random, int], list[str]], ...]] = (
    _shape_equality_ladder,
    _shape_boolean_storm,
    _shape_assert_chain,
    _shape_low_cohesion_class,
    _shape_clone_pair,
)


def generate_python(rng: random.Random) -> str:
    """Generate a parseable Python module, sometimes breaching the invariants on purpose."""
    lines = ["#!/usr/bin/env python3", '"""Generated module."""', ""]
    if rng.random() < 0.3:
        lines.append(f'GATEWAY = "{_private_address(rng)}"')
    for index in range(rng.randrange(1, 5)):
        lines.extend(_py_function(rng, index))
    for index in range(rng.randrange(1, 4)):
        lines.extend(rng.choice(_PY_SHAPES)(rng, index))
    return "\n".join(lines) + "\n"


# --- Markdown generation --------------------------------------------------------------

_MD_LINKS: Final[tuple[str, ...]] = (
    "[relative](./generated-{n}.md)",
    "[anchor](#section-{n})",
    "[absolute](https://example.com/{n})",
    "[spaced] (./generated-{n}.md)",
    "[empty]()",
)

_MD_FENCES: Final[tuple[tuple[str, str], ...]] = (
    ("python", "value = {n}\n"),
    ("python", "def broken(:\n"),
    ("json", '{{"key": {n}}}\n'),
    ("json", '{{"key": {n},}}\n'),
    ("yaml", "key: {n}\n"),
    ("yaml", "key: {n}\n  bad indent\n"),
    ("mermaid", "flowchart LR\n    A[{n}] --> B[done]\n"),
    ("mermaid", "flowchart LR\n    A[{n}] -> B\n"),
)


def _md_table(rng: random.Random) -> list[str]:
    """Emit a table whose rows sometimes disagree with the header about column count."""
    width = rng.randrange(2, 5)
    header = "| " + " | ".join(f"col{i}" for i in range(width)) + " |"
    divider = "|" + "---|" * width
    cells = width + rng.choice((-1, 0, 0, 1))
    row = "| " + " | ".join(str(rng.randrange(100)) for _ in range(max(1, cells))) + " |"
    return [header, divider, row, ""]


def _md_fence(rng: random.Random) -> list[str]:
    """Emit a fenced block, occasionally leaving it unterminated."""
    language, body = rng.choice(_MD_FENCES)
    block = [f"```{language}", body.format(n=rng.randrange(100)).rstrip()]
    if rng.random() < 0.85:
        block.append("```")
    block.append("")
    return block


def _md_section(rng: random.Random, index: int) -> list[str]:
    """Emit one heading and a randomly chosen body element beneath it."""
    lines = [f"{'#' * rng.randrange(1, 5)} Section {index}", ""]
    builders = (_md_fence, _md_table)
    lines.extend(rng.choice(builders)(rng))
    lines.append(rng.choice(_MD_LINKS).format(n=index))
    lines.append("")
    return lines


def generate_markdown(rng: random.Random) -> str:
    """Generate a Markdown document exercising fences, tables, links and diagrams."""
    lines = [f"# Generated Document {rng.randrange(1000)}", ""]
    for index in range(rng.randrange(1, 5)):
        lines.extend(_md_section(rng, index))
    if rng.random() < 0.2:
        lines.append(f"Reachable at {_private_address(rng)} internally.")
    return "\n".join(lines) + "\n"


# --- Roadmap generation ---------------------------------------------------------------
#
# A separate corpus from `markdown` because the roadmap parser reads a grammar, not a
# document: milestone headings carrying a version, checkbox items carrying an optional
# priority and an optional blocking reason, indented context bullets, and a value/effort
# table it sizes items from. Fed ordinary Markdown it returned zero items for 30 of 30
# cases, which exercised its first regex and nothing after it.

_ROADMAP_HEADINGS: Final[tuple[str, ...]] = (
    "### Milestone {n}: Generated Track (v0.{n}.0)",
    "### Milestone {n}: Generated Track (vNEXT)",
    "### Milestone {n}: Generated Track (v{n})",
    "## Rejected Alternatives",
    "### Anti-Patterns Considered (v0.{n}.0)",
    "#### Generated Subsection {n}",
)

_ROADMAP_ITEMS: Final[tuple[str, ...]] = (
    "- [ ] **Deliverable {n}**:",
    "- [ ] **Deliverable {n} (P{p} - High)**:",
    "- [x] **Deliverable {n} (P{p} - Low)**:",
    "- [ ] **Deliverable {n}** (blocked: upstream {n} is unreleased):",
    "- [ ] **Deliverable {n}** (blocked):",
    "- [ ] **Deliverable {n} (P9 - Imaginary)**:",
    "- [ ] ****:",
    "- [ ] **`Deliverable {n}` with *markup***:",
)

_ROADMAP_CONTEXT: Final[tuple[str, ...]] = (
    "  - *Gap*: generated description {n}",
    "  - *Evidence*: generated citation {n}",
    "  - *Survey*: generated marker {n}",
    "  - *: malformed label {n}",
    "  - plain bullet {n}",
)


def _roadmap_matrix(rng: random.Random) -> list[str]:
    """Emit the value/effort table the prioritizer sizes items from."""
    sizes = ("High", "Medium", "Low", "Unknown")
    rows = [
        f"| track | Deliverable {n} | Python | {rng.choice(sizes)} | {rng.choice(sizes)} | v0.{n}.0 |"
        for n in range(rng.randrange(1, 5))
    ]
    return ["| Category | Feature | Tech | Value | Effort | Target |", "|---|---|---|---|---|---|", *rows, ""]


def generate_roadmap(rng: random.Random) -> str:
    """Generate a roadmap document: milestones, checkbox items, context and a sizing table."""
    lines = ["# Generated Roadmap", ""]
    lines.extend(_roadmap_matrix(rng))
    for index in range(rng.randrange(1, 4)):
        lines.append(_ROADMAP_HEADINGS[rng.randrange(len(_ROADMAP_HEADINGS))].format(n=index))
        lines.extend(_roadmap_entry(rng, index))
    return "\n".join(lines) + "\n"


def _roadmap_entry(rng: random.Random, index: int) -> list[str]:
    """Emit the items and context bullets beneath one milestone heading."""
    lines: list[str] = []
    for item in range(rng.randrange(1, 5)):
        lines.append(rng.choice(_ROADMAP_ITEMS).format(n=index * 10 + item, p=rng.randrange(4)))
        lines.extend(
            rng.choice(_ROADMAP_CONTEXT).format(n=item) for _ in range(rng.randrange(0, 3))
        )
    lines.append("")
    return lines


# --- Manifest generation --------------------------------------------------------------

_REQ_SPECS: Final[tuple[str, ...]] = (
    '"package-{n}>={n}.0"',
    '"package-{n}=={n}.0.1"',
    '"package-{n}"',
    '"package-{n}~={n}.4"',
    '"package-{n}[extra]>={n}.0,<{n}9"',
    '"package-{n} >= {n}.0  # trailing"',
    '"Package_{n}.Name>={n}"',
)


def generate_requirements(rng: random.Random) -> str:
    """Generate a pyproject fragment declaring randomly specified dependencies."""
    specs = [rng.choice(_REQ_SPECS).format(n=rng.randrange(1, 40)) for _ in range(rng.randrange(1, 7))]
    return "\n".join(["[project]", 'name = "generated"', "dependencies = [", *(f"    {s}," for s in specs), "]", ""])


_ACTION_REFS: Final[tuple[str, ...]] = (
    "actions/checkout@v{n}",
    "actions/setup-python@main",
    "owner-{n}/action-{n}@" + "a" * 40,
    "owner-{n}/action-{n}@v{n}.1.2",
    "./.github/actions/local-{n}",
    "docker://ghcr.io/owner/image:{n}",
)


def generate_workflow(rng: random.Random) -> str:
    """Generate a workflow manifest whose action references vary in how they are pinned."""
    steps = [
        f"      - uses: {rng.choice(_ACTION_REFS).format(n=rng.randrange(1, 9))}"
        for _ in range(rng.randrange(1, 5))
    ]
    return "\n".join(["name: generated", "on: [push]", "jobs:", "  build:", "    steps:", *steps, ""])


GENERATORS: Final[dict[str, Callable[[random.Random], str]]] = {
    "python": generate_python,
    "markdown": generate_markdown,
    "roadmap": generate_roadmap,
    "requirements": generate_requirements,
    "workflow": generate_workflow,
}


def generate(corpus: str, seed: int) -> Case:
    """Generate one well-formed case for a corpus, reproducible from its seed alone."""
    return Case(corpus=corpus, text=GENERATORS[corpus](random.Random(seed)), seed=seed)


# --- Mutation -------------------------------------------------------------------------
#
# Every operator takes (text, rng, donor) whether or not it uses the donor. A uniform
# signature keeps the dispatch a dictionary lookup instead of a branch per operator, which
# is the same reason §10 gives for table-driven dispatch everywhere else.

# Bytes chosen because each one reaches a different failure path: NUL is rejected by
# `ast.parse` with ValueError rather than SyntaxError, a lone CR changes line counting,
# a BOM mid-file is not a BOM, and the escapes are legal in UTF-8 but not in source.
_CONTROL_BYTES: Final[tuple[str, ...]] = ("\x00", "\r", "\x0b", "\x0c", "﻿", "\x1b[0m", "\t")


def _lines(text: str) -> list[str]:
    """Split keeping terminators, so a mutation cannot silently normalize line endings."""
    return text.splitlines(keepends=True)


def _m_delete_line(text: str, rng: random.Random, donor: str) -> str:
    """Remove one line."""
    del donor
    lines = _lines(text)
    if not lines:
        return text
    index = rng.randrange(len(lines))
    return "".join(lines[:index] + lines[index + 1 :])


def _m_duplicate_line(text: str, rng: random.Random, donor: str) -> str:
    """Repeat one line, which doubles an indent or re-declares a name."""
    del donor
    lines = _lines(text)
    if not lines:
        return text
    index = rng.randrange(len(lines))
    return "".join([*lines[: index + 1], lines[index], *lines[index + 1 :]])


def _m_truncate(text: str, rng: random.Random, donor: str) -> str:
    """Cut the input at an arbitrary offset, simulating a partial write."""
    del donor
    return text[: rng.randrange(len(text) + 1)]


def _m_swap_adjacent(text: str, rng: random.Random, donor: str) -> str:
    """Transpose two neighbouring characters."""
    del donor
    if len(text) < 2:
        return text
    index = rng.randrange(len(text) - 1)
    return text[:index] + text[index + 1] + text[index] + text[index + 2 :]


def _m_insert_control(text: str, rng: random.Random, donor: str) -> str:
    """Splice in a control or format character at an arbitrary offset."""
    del donor
    index = rng.randrange(len(text) + 1)
    return text[:index] + rng.choice(_CONTROL_BYTES) + text[index:]


def _m_repeat_token(text: str, rng: random.Random, donor: str) -> str:
    """Repeat one line many times, to reach a cap no hand-written fixture reaches."""
    del donor
    lines = _lines(text)
    if not lines:
        return text
    index = rng.randrange(len(lines))
    return "".join(lines[:index] + [lines[index]] * rng.randrange(50, 400) + lines[index:])


def _m_indent_storm(text: str, rng: random.Random, donor: str) -> str:
    """Indent the whole input, which turns a valid module into a nesting question."""
    del donor
    pad = " " * rng.randrange(1, 64)
    return "".join(pad + line for line in _lines(text))


def _m_widen(text: str, rng: random.Random, donor: str) -> str:
    """Append one very long line, probing any per-line buffer or regex cost."""
    del donor
    return text + "#" + "x" * rng.randrange(4096, 65536) + "\n"


def _m_splice(text: str, rng: random.Random, donor: str) -> str:
    """Graft the tail of another case onto the head of this one."""
    cut = rng.randrange(len(text) + 1)
    return text[:cut] + donor[rng.randrange(len(donor) + 1) :]


def _m_blank(text: str, rng: random.Random, donor: str) -> str:
    """Reduce the input to nothing, the case every parser claims to handle."""
    del text, rng, donor
    return ""


MUTATORS: Final[dict[str, Callable[[str, random.Random, str], str]]] = {
    "blank": _m_blank,
    "delete_line": _m_delete_line,
    "duplicate_line": _m_duplicate_line,
    "indent_storm": _m_indent_storm,
    "insert_control": _m_insert_control,
    "repeat_token": _m_repeat_token,
    "splice": _m_splice,
    "swap_adjacent": _m_swap_adjacent,
    "truncate": _m_truncate,
    "widen": _m_widen,
}

# `blank` and `widen` are uninformative once they have been tried once each: the first is
# a single input and the second says nothing a shorter case does not. They stay in the
# table so `replay` can name them, and out of the default rotation so a budget of 200
# cases is not spent re-proving that the empty string parses.
EXPLORATORY_MUTATORS: Final[tuple[str, ...]] = tuple(
    name for name in sorted(MUTATORS) if name not in ("blank", "widen")
)


def mutate(case: Case, rng: random.Random, donor: str | None = None) -> Case:
    """Apply one mutation operator, recording which one in the returned case's origin."""
    name = rng.choice(EXPLORATORY_MUTATORS)
    text = MUTATORS[name](case.text, rng, donor if donor is not None else case.text)
    return Case(corpus=case.corpus, text=text, seed=case.seed, origin=f"{case.origin}+{name}")


# --- Shrinking ------------------------------------------------------------------------


def shrink(text: str, fails: Callable[[str], bool], max_rounds: int = 400) -> str:
    """Reduce a failing input to a smaller one that fails the same way.

    Delta debugging over lines, then over the trailing characters. `max_rounds` bounds the
    number of candidate evaluations rather than the wall clock, because the predicate runs
    a real instrument and its cost varies by two orders of magnitude between hosts — the
    same reason §11 refuses to gate on `feedback_latency`.
    """
    return _shrink_tail(_shrink_lines(text, fails, max_rounds), fails)


def _shrink_lines(text: str, fails: Callable[[str], bool], max_rounds: int) -> str:
    """Remove line windows of halving width while the input still fails."""
    lines = _lines(text)
    chunk = max(1, len(lines) // 2)
    rounds = 0
    while chunk >= 1 and rounds < max_rounds:
        lines, removed, rounds = _shrink_pass(lines, fails, chunk, rounds, max_rounds)
        if not removed:
            chunk //= 2
    return "".join(lines)


def _shrink_pass(
    lines: Sequence[str], fails: Callable[[str], bool], chunk: int, rounds: int, max_rounds: int
) -> tuple[list[str], bool, int]:
    """Try removing each window of `chunk` lines once, keeping every removal that holds.

    The empty input is reachable. It is a legitimate minimum: an instrument that fails on
    an empty document should say so with an empty corpus entry, not with the smallest
    non-empty one the reducer happened to stop at.
    """
    kept = list(lines)
    index = 0
    removed = False
    while index < len(kept) and rounds < max_rounds:
        candidate = kept[:index] + kept[index + chunk :]
        rounds += 1
        if fails("".join(candidate)):
            kept, removed = candidate, True
        else:
            index += chunk
    return kept, removed, rounds


def _shrink_tail(text: str, fails: Callable[[str], bool]) -> str:
    """Trim trailing characters in halving steps while the input still fails."""
    step = max(1, len(text) // 2)
    while step >= 1:
        while len(text) >= step and fails(text[: len(text) - step]):
            text = text[: len(text) - step]
        step //= 2
    return text
