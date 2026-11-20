#!/usr/bin/env python3
"""Single definition of how a tool imports a module from `examples/`.

The examples are standalone programs that a reader copies and runs. They are not packages
and sit on no import path. A tool that runs one puts the example's own directory on
`sys.path` and imports the module by its bare name, so the example's imports of its
sibling modules resolve as they do when the example runs alone. The examples stay as
they are; they are not restructured to suit one consumer.

Four tools held their own copy of this loader, and the copies differed in their
signatures. The loader lives here and nowhere else.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType


def load_example(root: Path, example: str, module: str) -> ModuleType:
    """Import `<root>/examples/<example>/<module>.py` and return the module.

    The example directory goes on `sys.path` once. A module file that does not exist
    raises `ModuleNotFoundError` with its path, and `sys.path` does not change.
    """
    directory = (root / "examples" / example).resolve()
    source = directory / f"{module}.py"
    if not source.is_file():
        raise ModuleNotFoundError(f"No example module at {source}", name=module, path=str(source))
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return importlib.import_module(module)
