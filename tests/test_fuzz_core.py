"""Tests for the fuzzer's input generation, mutation and shrinking.

The generators decide what the whole campaign is capable of finding. A generator that
emits noise reaches one `except` and nothing behind it, and a generator that is not
reproducible turns every finding into an anecdote — so both are asserted here rather than
assumed.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import fuzz_core
from fuzz_core import CORPORA, GENERATORS, MUTATORS, SUFFIXES, Case, generate, mutate, shrink


def test_every_declared_corpus_has_a_generator_and_a_suffix() -> None:
    """A corpus a target can name but nothing can produce is a target that never runs."""
    assert (sorted(GENERATORS), sorted(SUFFIXES)) == (sorted(CORPORA), sorted(CORPORA))


def test_generation_is_a_function_of_the_seed_alone() -> None:
    """A finding that cannot be regenerated from its seed is an anecdote, not a report."""
    first = [generate(corpus, 41).text for corpus in CORPORA]
    second = [generate(corpus, 41).text for corpus in CORPORA]
    assert first == second


def test_different_seeds_produce_different_inputs() -> None:
    """A generator that ignores its seed turns a thousand-case campaign into one case."""
    texts = {generate("python", seed).text for seed in range(20)}
    assert len(texts) == 20


def test_generated_python_parses() -> None:
    """Valid input is what reaches the code behind a parser's first rejection."""
    failures = [seed for seed in range(120) if _syntax_error(generate("python", seed).text)]
    assert failures == []


def _syntax_error(source: str) -> bool:
    """Report whether a generated module failed to parse."""
    try:
        ast.parse(source)
    except SyntaxError:
        return True
    return False


def test_generated_roadmaps_carry_the_grammar_the_parser_reads() -> None:
    """Ordinary Markdown returned zero items for 30 of 30 cases; the grammar is the corpus.

    Asserted over the corpus rather than over every document, because a generator whose
    every document looks the same explores one shape repeatedly. What must hold is that
    each element of the grammar — milestone heading, open item, context bullet, sizing
    row — is reachable, and that most documents carry at least one open item.
    """
    documents = [generate("roadmap", seed).text for seed in range(30)]
    corpus = "".join(documents)
    grammar = ("### Milestone", "- [ ] **", "  - *Gap*", "| Category | Feature |")
    with_items = sum("- [ ] **" in document for document in documents)
    assert (all(token in corpus for token in grammar), with_items > 15) == (True, True)


def test_no_private_address_appears_as_a_literal_in_the_generator() -> None:
    """The generator feeds the egress detector, so it must not trip it."""
    source = Path(fuzz_core.__file__).read_text(encoding="utf-8")
    found = re.findall(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b", source)
    assert found == []


def test_the_generator_still_emits_private_addresses_at_runtime() -> None:
    """Assembling them from octets must not quietly disable the positive path."""
    corpus = "".join(generate("python", seed).text for seed in range(60))
    assert re.search(r"\b(?:10|192\.168|172\.16)\.\d{1,3}\.\d{1,3}\b", corpus) is not None


def test_mutation_records_which_operator_produced_the_input() -> None:
    """A failing case whose provenance is unknown cannot be narrowed to an operator."""
    import random

    mutated = mutate(generate("python", 3), random.Random(9))
    assert (mutated.origin.startswith("generated+"), mutated.seed) == (True, 3)


def test_no_mutator_raises_on_degenerate_input() -> None:
    """The mutators run inside the campaign loop; one that raises ends it early."""
    import random

    rng = random.Random(0)
    outputs = [MUTATORS[name]("", rng, "") for name in sorted(MUTATORS)]
    assert all(isinstance(text, str) for text in outputs)


def test_shrinking_reduces_an_input_while_preserving_the_failure() -> None:
    """An unshrunk failure is a file nobody reads; a shrunk one is a regression test."""
    text = "\n".join(f"line {index}" for index in range(200)) + "\nPOISON\n"
    reduced = shrink(text, lambda candidate: "POISON" in candidate)
    assert (reduced.strip(), len(reduced) < len(text)) == ("POISON", True)


def test_shrinking_terminates_when_every_candidate_still_fails() -> None:
    """A predicate that always holds must reduce all the way, not stop one character short."""
    assert shrink("a" * 500, lambda candidate: True) == ""


def test_shrinking_keeps_an_input_that_cannot_be_reduced() -> None:
    """A predicate that never holds for a smaller input must leave the original intact."""
    assert shrink("abcdef", lambda candidate: candidate == "abcdef") == "abcdef"


def test_a_case_is_addressed_by_its_content() -> None:
    """Storing the same failing input twice under two names would grow the corpus forever."""
    first = Case("python", "x = 1\n", 1, "generated")
    second = Case("python", "x = 1\n", 99, "generated+truncate")
    assert (first.digest(), first.filename()) == (second.digest(), second.digest() + ".case")


def test_a_stored_case_carries_no_suffix_an_instrument_scans() -> None:
    """A `.py` fixture would be audited by the sentinel that it is a fixture for."""
    suffixes = {generate(corpus, 1).filename().split(".", 1)[1] for corpus in CORPORA}
    assert suffixes == {"case"}
