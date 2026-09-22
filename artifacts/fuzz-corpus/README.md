# Fuzzing Regression Corpus

Every file under this directory is an input that once broke one of this repository's
instruments. Together they are the gate: [`tools/fuzz_harness.py`](../../tools/fuzz_harness.py)
`replay` re-runs all of them on every push and every pull request, and the set only grows.

## Why the corpus is the gate and the search is not

Exploration is a random search. A campaign that must pass fails on the run that happened
to find something and passes on the run that happened not to, and neither outcome is about
the commit under test. So the two are split:

| Command | When | Deterministic | Gates a release |
|---|---|---|---|
| `replay` | every push, every pull request, pre-push hook | yes | yes |
| `run` | [`fuzz.yml`](../../.github/workflows/fuzz.yml), daily | no | no |
| `crosscheck` | `fuzz.yml`, daily | yes, per hash seed | yes, when seeds disagree |

A scheduled `run` that finds something uploads the minimized input as a workflow artifact.
It does not commit. A new corpus entry is a claim that an instrument is broken, and that
claim belongs in a pull request somebody reads.

## Layout

```text
artifacts/fuzz-corpus/
├── README.md
└── <target>/                 # one directory per instrument under test
    └── <content-hash>.case   # one input, named by its own digest
```

The `.case` suffix is deliberate. A stored case is a document that broke a parser, so a
corpus of `.md` files would be swept up by the documentation validator's whole-repository
pass and a corpus of `.py` files by the sentinel, ruff and mypy — every fixture reported
as a defect in the repository that keeps it. The real suffix is restored when the case is
written to a scratch directory, which is where the parser actually needs it.

Naming by content hash means the same failing input is never stored twice, however many
mutations rediscover it.

## Adding an entry

```bash
python3 tools/fuzz_harness.py run --cases 2000 --seed "$RANDOM"   # writes what it finds
python3 tools/fuzz_harness.py replay                              # must now fail
# fix the instrument
python3 tools/fuzz_harness.py replay                              # must now pass
```

Commit the fix and the case in the same pull request. An entry that never failed the build
is not a regression test, so verify the failure before the fix as well as the pass after
it — reverting the fix and watching `replay` exit non-zero takes ten seconds and is the
only thing that distinguishes the two.

## What is here

| Target | Case | The defect it pins |
|---|---|---|
| `smells` | `f54cb450570a8f93.case` | `ast.parse` accepts a form feed inside a string literal and radon's raw tokenizer rejects it, so the corpus filter and the scorer disagreed about what Python is. One such module raised `SyntaxError` out of `analyze` and aborted the whole repository scan. |
| `docs_fix` | `2c70a7f1ccbda495.case` | `auto_fix_content` dropped one trailing newline per call while reporting zero repairs: `splitlines()` discards the final terminator, and the rejoin restored it only when the joined text did not already end in one — which a document ending in two newlines always does. |
