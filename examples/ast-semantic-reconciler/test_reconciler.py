"""Unit and integration test suite for Autonomous 3-Way AST Semantic Reconciler.

Validates commutative disjoint symbol reconciliation, import sets unification,
class member arbitration, docstrings, the compile check, and semantic collision
detection under SARIF 2.1.0.

Passes the AST Invariant Sentinel `strict` preset (M <= 6, depth <= 3).
"""

from __future__ import annotations

import ast
from pathlib import Path

import ast_reconciler
import pytest
from ast_reconciler import (
    ReconciliationStatus,
    SymbolKind,
    classify_statement_symbol,
    extract_docstring_node,
    hash_ast_node,
    main,
    parse_ast_safely,
    reconcile_3way,
    reconcile_import_sets,
    to_markdown,
    to_sarif,
)


def test_hash_ast_node_and_safe_parse() -> None:
    """Verify deterministic structural hashing and safe AST parsing."""
    code_a = "def foo(x: int) -> int:\n    return x + 1\n"
    code_b = "def foo(x: int) -> int:\n    return x + 1  # comment\n"
    tree_a = parse_ast_safely(code_a)
    tree_b = parse_ast_safely(code_b)
    assert (
        hash_ast_node(tree_a.body[0]),
        hash_ast_node(tree_b.body[0]),
    ) == (
        hash_ast_node(tree_b.body[0]),
        hash_ast_node(tree_a.body[0]),
    )


def test_extract_docstring_and_classification() -> None:
    """Verify docstring separation and symbol classification."""
    code = '"""Module docstring."""\n\nFLAG: bool = True\ndef task(): pass\n'
    tree = parse_ast_safely(code)
    doc_node, rest = extract_docstring_node(tree)
    name_c, kind_c = classify_statement_symbol(rest[0], 0)
    name_f, kind_f = classify_statement_symbol(rest[1], 1)
    assert (
        doc_node is not None,
        len(rest),
        name_c,
        kind_c,
        name_f,
        kind_f,
    ) == (
        True,
        2,
        "FLAG",
        SymbolKind.CONSTANT,
        "task",
        SymbolKind.FUNCTION,
    )


def test_reconcile_import_sets_deduplication() -> None:
    """Verify 3-way import union and false conflict counting."""
    base_imp = {"import os", "from sys import version"}
    ours_imp = {"import os", "from sys import version", "import json"}
    theirs_imp = {"import os", "from sys import version", "import time"}
    merged, false_conflicts = reconcile_import_sets(base_imp, ours_imp, theirs_imp)
    expected = ["from sys import version", "import json", "import os", "import time"]
    assert (merged, false_conflicts) == (expected, 1)


def test_reconcile_trivial_clean_merge() -> None:
    """Verify clean 3-way merge when only one branch modifies a function."""
    base = "def worker():\n    return 0\n"
    ours = "def worker():\n    return 42\n"
    theirs = "def worker():\n    return 0\n"
    res = reconcile_3way(base, ours, theirs)
    assert (
        res.status,
        "return 42" in res.merged_code,
        len(res.collisions),
    ) == (
        ReconciliationStatus.CLEAN_MERGE,
        True,
        0,
    )


def test_reconcile_commutative_disjoint_functions() -> None:
    """Verify commutative merge of disjoint functions added to the same position."""
    base = "def common():\n    return 1\n"
    ours = "def common():\n    return 1\n\n\ndef helper_alpha():\n    return 'alpha'\n"
    theirs = "def common():\n    return 1\n\n\ndef helper_beta():\n    return 'beta'\n"
    res = reconcile_3way(base, ours, theirs)
    parsed = ast.parse(res.merged_code)
    func_names = [stmt.name for stmt in parsed.body if isinstance(stmt, ast.FunctionDef)]
    assert (
        res.status,
        res.resolved_false_conflicts >= 1,
        "helper_alpha" in func_names,
        "helper_beta" in func_names,
    ) == (
        ReconciliationStatus.COMMUTATIVE_MERGE,
        True,
        True,
        True,
    )


def test_reconcile_class_disjoint_methods() -> None:
    """Verify method-level reconciliation when branches add disjoint methods."""
    base = "class Service:\n    def start(self):\n        pass\n"
    ours = "class Service:\n    def start(self):\n        pass\n    def pause(self):\n        pass\n"
    theirs = "class Service:\n    def start(self):\n        pass\n    def resume(self):\n        pass\n"
    res = reconcile_3way(base, ours, theirs)
    assert (
        res.status,
        len(res.collisions),
        "def pause" in res.merged_code,
        "def resume" in res.merged_code,
    ) == (
        ReconciliationStatus.COMMUTATIVE_MERGE,
        0,
        True,
        True,
    )


def test_reconcile_semantic_collision_reporting() -> None:
    """Verify diagnostic detection when both branches make incompatible edits."""
    base = "def calculate(x: int) -> int:\n    return x\n"
    ours = "def calculate(x: int) -> int:\n    return x * 2\n"
    theirs = "def calculate(x: int) -> int:\n    return x * 3\n"
    res = reconcile_3way(base, ours, theirs)
    col = res.collisions[0] if res.collisions else None
    assert (
        res.status,
        len(res.collisions),
        col.symbol_name if col else "",
        col.symbol_kind if col else None,
    ) == (
        ReconciliationStatus.SEMANTIC_COLLISION,
        1,
        "calculate",
        SymbolKind.FUNCTION,
    )


def test_sarif_and_markdown_exporters() -> None:
    """Verify SARIF 2.1.0 schema contract and Markdown summary formatting."""
    base = "def f(): pass\n"
    ours = "def f(): return 1\n"
    theirs = "def f(): return 2\n"
    res = reconcile_3way(base, ours, theirs)
    sarif = to_sarif(res)
    md = to_markdown(res)
    assert (
        sarif["version"],
        len(sarif["runs"][0]["results"]),
        "SEMANTIC_COLLISION" in md,
        "❌ COLLISION" in md,
    ) == (
        "2.1.0",
        1,
        True,
        True,
    )


def _write_source_files(
    tmp_path: Path,
    base_src: str,
    ours_src: str,
    theirs_src: str,
) -> tuple[Path, Path, Path]:
    """Helper to write 3-way test sources to temporary paths."""
    base_file = tmp_path / "base.py"
    ours_file = tmp_path / "ours.py"
    theirs_file = tmp_path / "theirs.py"
    base_file.write_text(base_src, encoding="utf-8")
    ours_file.write_text(ours_src, encoding="utf-8")
    theirs_file.write_text(theirs_src, encoding="utf-8")
    return base_file, ours_file, theirs_file


def test_main_cli_execution_clean(tmp_path: Path) -> None:
    """Verify main CLI command execution writes merged output to file."""
    b, o, t = _write_source_files(tmp_path, "x = 1\n", "x = 1\ndef fa(): pass\n", "x = 1\ndef fb(): pass\n")
    out_file = tmp_path / "out.py"
    code = main(["--base", str(b), "--ours", str(o), "--theirs", str(t), "--output", str(out_file)])
    assert (code, out_file.exists(), "def fa" in out_file.read_text(encoding="utf-8")) == (0, True, True)


def test_main_cli_execution_collision(tmp_path: Path) -> None:
    """Verify main CLI command returns exit code 1 on collision."""
    b, o, t = _write_source_files(
        tmp_path, "def run(): return 0\n", "def run(): return 1\n", "def run(): return 2\n"
    )
    code = main(["--base", str(b), "--ours", str(o), "--theirs", str(t), "--format", "markdown"])
    assert code == 1


_COUNTER = (
    '"""Module docs."""\n\n'
    "class Counter:\n"
    '    """Counter state."""\n'
    "    count = 0\n\n"
    "    def start(self):\n"
    "        return 1\n"
)


def _class_shape(code: str, name: str) -> tuple[str | None, list[str]]:
    """Return the docstring and the first line of each other member of a class."""
    classes = {stmt.name: stmt for stmt in ast.parse(code).body if isinstance(stmt, ast.ClassDef)}
    cls = classes[name]
    doc = ast.get_docstring(cls)
    members = cls.body[1:] if doc is not None else cls.body
    return doc, [ast.unparse(member).splitlines()[0] for member in members]


def test_reconcile_keeps_class_members_and_one_module_docstring() -> None:
    """Verify that a merge keeps class members and writes the module docstring once."""
    base = (
        '"""Mod doc."""\n\nclass A:\n    """Class doc."""\n    X = 1\n\n    def f(self):\n        return 1\n'
    )
    ours = base + "\ndef g():\n    return 2\n"
    theirs = base + "\ndef h():\n    return 3\n"
    res = reconcile_3way(base, ours, theirs)
    assert (
        res.status,
        res.merged_code.count("Mod doc."),
        ast.get_docstring(ast.parse(res.merged_code)),
        _class_shape(res.merged_code, "A"),
        "def g" in res.merged_code and "def h" in res.merged_code,
    ) == (
        ReconciliationStatus.COMMUTATIVE_MERGE,
        1,
        "Mod doc.",
        ("Class doc.", ["X = 1", "def f(self):"]),
        True,
    )


def test_reconcile_unchanged_class_keeps_members() -> None:
    """Verify that a class no branch changed keeps its attribute and docstring."""
    res = reconcile_3way(_COUNTER, _COUNTER, _COUNTER)
    assert (
        res.status,
        res.merged_code.count("Module docs."),
        _class_shape(res.merged_code, "Counter"),
    ) == (
        ReconciliationStatus.CLEAN_MERGE,
        1,
        ("Counter state.", ["count = 0", "def start(self):"]),
    )


def test_reconcile_changed_class_keeps_members() -> None:
    """Verify that a class one branch extends keeps its attribute and docstring."""
    ours = _COUNTER + "\n    def reset(self):\n        return 0\n"
    res = reconcile_3way(_COUNTER, ours, _COUNTER)
    assert _class_shape(res.merged_code, "Counter") == (
        "Counter state.",
        ["count = 0", "def start(self):", "def reset(self):"],
    )


def test_reconcile_disjoint_class_members() -> None:
    """Verify that an attribute from ours and a method from theirs both reach the class."""
    ours = _COUNTER.replace("    count = 0\n", "    count = 0\n    limit = 10\n")
    theirs = _COUNTER + "\n    def stop(self):\n        return 2\n"
    res = reconcile_3way(_COUNTER, ours, theirs)
    assert (res.status, _class_shape(res.merged_code, "Counter")) == (
        ReconciliationStatus.COMMUTATIVE_MERGE,
        ("Counter state.", ["count = 0", "limit = 10", "def start(self):", "def stop(self):"]),
    )


def test_reconcile_keeps_module_docstring_added_by_theirs() -> None:
    """Verify that a module docstring that only theirs adds appears once."""
    base = "def f():\n    return 1\n"
    res = reconcile_3way(base, base, '"""Module docs."""\n' + base)
    assert (
        res.merged_code.count("Module docs."),
        ast.get_docstring(ast.parse(res.merged_code)),
    ) == (1, "Module docs.")


def test_reconcile_keeps_property_getter_and_setter() -> None:
    """Verify that two class members with the same name both survive a member merge."""
    base = (
        "class Box:\n"
        "    @property\n"
        "    def value(self):\n"
        "        return self._v\n\n"
        "    @value.setter\n"
        "    def value(self, v):\n"
        "        self._v = v\n"
    )
    ours = base + "\n    def a(self):\n        return 1\n"
    theirs = base + "\n    def b(self):\n        return 2\n"
    res = reconcile_3way(base, ours, theirs)
    assert (res.merged_code.count("def value"), _class_shape(res.merged_code, "Box")[1]) == (
        2,
        ["@property", "@value.setter", "def a(self):", "def b(self):"],
    )


def test_reconcile_keeps_class_header_change_of_theirs() -> None:
    """Verify that a new base class from theirs survives a method added by ours."""
    ours = _COUNTER + "\n    def reset(self):\n        return 0\n"
    theirs = _COUNTER.replace("class Counter:", "class Counter(Base):")
    res = reconcile_3way(_COUNTER, ours, theirs)
    assert ("class Counter(Base):" in res.merged_code, "def reset" in res.merged_code) == (True, True)


def test_reconcile_class_header_collision_names_the_class() -> None:
    """Verify that two different new base classes are a collision on the class itself."""
    ours = (
        _COUNTER.replace("class Counter:", "class Counter(Left):") + "\n    def a(self):\n        return 1\n"
    )
    theirs = _COUNTER.replace("class Counter:", "class Counter(Right):")
    res = reconcile_3way(_COUNTER, ours, theirs)
    names = [(c.symbol_name, c.symbol_kind) for c in res.collisions]
    assert (res.status, names) == (ReconciliationStatus.SEMANTIC_COLLISION, [("Counter", SymbolKind.CLASS)])


def test_reconcile_class_member_collision_names_the_member() -> None:
    """Verify that a collision inside a class names the class and the member."""
    ours = _COUNTER.replace("return 1", "return 2") + "\n    def a(self):\n        return 1\n"
    theirs = _COUNTER.replace("return 1", "return 3")
    res = reconcile_3way(_COUNTER, ours, theirs)
    names = [(c.symbol_name, c.symbol_kind) for c in res.collisions]
    assert (res.status, res.merged_code, names) == (
        ReconciliationStatus.SEMANTIC_COLLISION,
        "",
        [("Counter.start", SymbolKind.FUNCTION)],
    )


def test_reconcile_reports_merge_that_does_not_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that assembled code that does not parse becomes a collision, not output."""
    monkeypatch.setattr(ast_reconciler, "assemble_merged_module", lambda *_args: "def broken(:\n")
    res = reconcile_3way("x = 1\n", "x = 1\n", "x = 1\n")
    names = [(c.symbol_name, c.symbol_kind) for c in res.collisions]
    assert (res.status, res.merged_code, names) == (
        ReconciliationStatus.SEMANTIC_COLLISION,
        "",
        [("<module>", SymbolKind.MODULE)],
    )


_FUTURE_BASE = '"""Module docs."""\n\nfrom __future__ import annotations\n\nfrom . import a\n\n\ndef f() -> int:\n    return 1\n'


def test_reconcile_keeps_future_import_first() -> None:
    """Verify that a `from __future__` import stays first after the docstring, so the merge compiles."""
    ours = _FUTURE_BASE + "\n\ndef g() -> int:\n    return 2\n"
    theirs = _FUTURE_BASE.replace("from . import a", "from . import a\nimport os")
    res = reconcile_3way(_FUTURE_BASE, ours, theirs)
    compile(res.merged_code, "<merged>", "exec")
    first = ast.parse(res.merged_code).body[1]
    assert (res.collisions, ast.unparse(first)) == ([], "from __future__ import annotations")


def test_reconcile_reports_merge_that_does_not_compile() -> None:
    """Verify that a merge that parses but does not compile becomes a collision, not output."""
    base = "class C:\n    a = 1\n"
    ours = "class C:\n    a = 1\n    y = x\n"
    theirs = "class C:\n    global x\n    a = 1\n"
    res = reconcile_3way(base, ours, theirs)
    names = [(c.symbol_name, c.symbol_kind) for c in res.collisions]
    assert (res.status, res.merged_code, names) == (
        ReconciliationStatus.SEMANTIC_COLLISION,
        "",
        [("<module>", SymbolKind.MODULE)],
    )


_CLEAN_MERGES = [
    ("x = 1\n", "x = 1\ny = 2\n", "x = 1\nz = 3\n"),
    (_FUTURE_BASE, _FUTURE_BASE, _FUTURE_BASE),
    (_COUNTER, _COUNTER, _COUNTER),
    (_COUNTER, _COUNTER + "\n    def reset(self):\n        return 0\n", _COUNTER),
    (
        _COUNTER,
        _COUNTER.replace("count = 0", "count = 1"),
        _COUNTER + "\n    def stop(self):\n        pass\n",
    ),
    ("class E:\n    pass\n", "class E:\n    x = 1\n", "class E:\n    y = 2\n"),
    (
        "class N:\n    class M:\n        a = 1\n",
        "class N:\n    class M:\n        a = 1\n        b = 2\n",
        "class N:\n    class M:\n        a = 1\n        c = 3\n",
    ),
]


@pytest.mark.parametrize(("base", "ours", "theirs"), _CLEAN_MERGES)
def test_every_clean_merge_compiles(base: str, ours: str, theirs: str) -> None:
    """Verify that each clean merge in the table returns code that compiles."""
    res = reconcile_3way(base, ours, theirs)
    code = compile(res.merged_code, "<merged>", "exec")
    assert (res.collisions, code.co_name) == ([], "<module>")
