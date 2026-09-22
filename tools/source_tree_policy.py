#!/usr/bin/env python3
"""Single definition of which paths are the repository's own source.

Four call sites answered this question independently and no two agreed: the docs
validator pruned hidden directories, `.venv`, `node_modules` and `__pycache__`; the
workbench's document scanner pruned hidden directories and `.venv` but not
`node_modules`; its Python scanner pruned `.venv` and `__pycache__` but neither hidden
directories nor `node_modules`. Installing a Node toolchain therefore made the workbench
report 330 documents where the validator reported 96, and open 45 backlog items against
vendored READMEs it does not own and cannot fix.

Instruments that disagree about what the corpus *is* cannot be reconciled about what is
wrong with it, so the corpus definition lives here and nowhere else.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final, Iterator, Sequence

# Directory names that hold code this repository did not write and does not maintain.
# Hidden names (`.venv`, `.git`, `.mypy_cache`) are covered by the leading-dot rule.
VENDORED_DIR_NAMES: Final[frozenset[str]] = frozenset({"node_modules", "__pycache__", "venv"})

# Suffixes of generated build directories, which reappear after any packaging step.
VENDORED_DIR_SUFFIXES: Final[tuple[str, ...]] = (".egg-info",)


def _is_hidden(part: str) -> bool:
    """Report whether a path component is hidden, excluding the traversal components."""
    return part.startswith(".") and part not in (".", "..")


def is_repository_source(path: Path) -> bool:
    """Report whether a path is this repository's own source.

    Excludes vendored dependencies, generated build directories, and anything hidden.
    A path is judged by its components, so the rule holds for absolute and relative
    paths alike and does not depend on the directory a scan started from.
    """
    return not any(
        _is_hidden(part) or part in VENDORED_DIR_NAMES or part.endswith(VENDORED_DIR_SUFFIXES)
        for part in path.parts
    )


def iter_source_files(root_dir: Path, suffixes: Sequence[str]) -> Iterator[Path]:
    """Yield the repository's own files with any of the given suffixes.

    Prunes excluded directories during the walk rather than filtering afterwards.
    `rglob` cannot prune, so it descends into every vendored package before discarding
    the result — on a tree carrying a Node toolchain that is most of the walk.
    """
    wanted = {suffix.lower() for suffix in suffixes}
    for parent, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if is_repository_source(Path(d))]
        yield from _matching_files(Path(parent), files, wanted)


def _matching_files(parent: Path, names: Sequence[str], wanted: set[str]) -> Iterator[Path]:
    """Yield the files in one already-pruned directory whose suffix was asked for."""
    for name in names:
        path = parent / name
        if path.suffix.lower() in wanted:
            yield path
