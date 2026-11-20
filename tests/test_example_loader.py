"""Tests for the single definition of how a tool imports a module from `examples/`.

Four tools held their own copy of this loader, and the copies already differed in their
signatures. These tests pin the shared behavior and that the tools use the one copy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import cegis_engine
import example_loader
import fuzz_harness
import living_standard_certifier
import prompt_injection_scanner
import resource_iteration_workbench
import sarif_report
from example_loader import load_example


@pytest.fixture
def isolated_imports(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Restore `sys.path` after the test, so no temporary directory stays importable."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    return monkeypatch


def _write_example(root: Path, files: dict[str, str]) -> Path:
    """Write the modules of a demonstration example under root and return its directory."""
    directory = root / "examples" / "demo"
    directory.mkdir(parents=True)
    for name, text in files.items():
        (directory / f"{name}.py").write_text(text, encoding="utf-8")
    return directory.resolve()


def test_loads_the_named_module_from_the_examples_directory_of_the_root(
    tmp_path: Path, isolated_imports: pytest.MonkeyPatch
) -> None:
    """The loader imports `<root>/examples/<example>/<module>.py` and returns the module."""
    isolated_imports.delitem(sys.modules, "loader_demo_mark", raising=False)
    _write_example(tmp_path, {"loader_demo_mark": "MARK = 7\n"})

    module = load_example(tmp_path, "demo", "loader_demo_mark")

    assert module.MARK == 7


def test_a_loaded_example_imports_its_sibling_by_bare_name(
    tmp_path: Path, isolated_imports: pytest.MonkeyPatch
) -> None:
    """An example that imports a sibling module works as it does when it runs alone."""
    for name in ("loader_demo_main", "loader_demo_sibling"):
        isolated_imports.delitem(sys.modules, name, raising=False)
    _write_example(
        tmp_path,
        {
            "loader_demo_main": "import loader_demo_sibling\n\nVALUE = loader_demo_sibling.VALUE\n",
            "loader_demo_sibling": "VALUE = 'from the sibling'\n",
        },
    )

    module = load_example(tmp_path, "demo", "loader_demo_main")

    assert module.VALUE == "from the sibling"


def test_two_loads_put_the_example_directory_on_sys_path_once(
    tmp_path: Path, isolated_imports: pytest.MonkeyPatch
) -> None:
    """A tool that loads the same example again does not grow `sys.path`."""
    isolated_imports.delitem(sys.modules, "loader_demo_twice", raising=False)
    directory = _write_example(tmp_path, {"loader_demo_twice": "MARK = 1\n"})

    first = load_example(tmp_path, "demo", "loader_demo_twice")
    second = load_example(tmp_path, "demo", "loader_demo_twice")

    assert (second is first, sys.path.count(str(directory))) == (True, 1)


def test_a_missing_example_module_raises_an_error_that_names_its_path(
    tmp_path: Path, isolated_imports: pytest.MonkeyPatch
) -> None:
    """A wrong name fails with the path that was expected, and `sys.path` stays as it was."""
    directory = _write_example(tmp_path, {})
    expected = directory / "loader_demo_absent.py"
    path_before = list(sys.path)

    with pytest.raises(ModuleNotFoundError) as caught:
        load_example(tmp_path, "demo", "loader_demo_absent")

    assert (str(expected) in str(caught.value), caught.value.name, sys.path) == (
        True,
        "loader_demo_absent",
        path_before,
    )


def test_the_tools_share_one_loader() -> None:
    """Each tool that runs an example uses the shared loader, not a copy of its own."""
    tools = (
        sarif_report,
        fuzz_harness,
        living_standard_certifier,
        prompt_injection_scanner,
        cegis_engine,
        resource_iteration_workbench,
    )

    assert [tool.load_example for tool in tools] == [example_loader.load_example] * len(tools)
