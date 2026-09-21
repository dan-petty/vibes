"""Unit tests for the Code Smell Quantifier."""

import ast
from pathlib import Path

import pytest
from smell_quantifier import (
    LOW_MAINTAINABILITY_INDEX,
    ADVISORY_SMELLS,
    Smell,
    analyze,
    compute_halstead,
    detect_duplicated_blocks,
    detect_god_classes,
    detect_import_cycles,
    detect_long_functions,
    detect_long_parameter_lists,
    detect_low_cohesion,
    detect_unreferenced_symbols,
    main,
    maintainability_index,
)


def _tree(source: str) -> ast.AST:
    return ast.parse(source)


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def test_halstead_volume_grows_with_vocabulary() -> None:
    """Volume is N log2(n); a richer vocabulary must cost more bits."""
    simple = compute_halstead(_tree("x = 1\n"))
    complex_ = compute_halstead(_tree("a = b + c * d - e / f\ng = h(a, b, c)\n"))
    assert complex_.volume > simple.volume >= 0.0


def test_maintainability_index_is_normalized_and_size_dominated() -> None:
    """Radon's normalized scale is 0-100, and module length dominates the formula."""
    halstead = compute_halstead(_tree("def f(x):\n    return x + 1\n"))
    small = maintainability_index(halstead, complexity=1, loc=10)
    large = maintainability_index(halstead, complexity=1, loc=2000)
    assert 0.0 <= large < small <= 100.0


def test_long_parameter_list_ignores_the_bound_receiver() -> None:
    """`self` is not a parameter the caller supplies."""
    source = "class C:\n    def m(self, a, b, c, d, e):\n        return a\n"
    assert detect_long_parameter_lists(_tree(source), Path("m.py")) == []

    source = "class C:\n    def m(self, a, b, c, d, e, f):\n        return a\n"
    findings = detect_long_parameter_lists(_tree(source), Path("m.py"))
    assert [(f.smell, f.measured) for f in findings] == [(Smell.LONG_PARAMETER_LIST, 6)]


def test_long_function_is_independent_of_branching() -> None:
    """A long linear function has complexity 1 and passes every branching gate."""
    body = "\n".join(f"    x{i} = {i}" for i in range(60))
    findings = detect_long_functions(_tree(f"def f():\n{body}\n"), Path("m.py"))
    assert [f.smell for f in findings] == [Smell.LONG_FUNCTION]


def test_data_carriers_are_exempt_from_the_attribute_ceiling() -> None:
    """A dataclass with nine fields is a record, not a god object."""
    fields = "\n".join(f"    f{i}: int = {i}" for i in range(9))
    carrier = f"from dataclasses import dataclass\n\n@dataclass\nclass R:\n{fields}\n"
    assert detect_god_classes(_tree(carrier), Path("m.py")) == []

    assigns = "\n".join(f"        self.f{i} = {i}" for i in range(9))
    god = f"class G:\n    def __init__(self):\n{assigns}\n"
    assert [f.smell for f in detect_god_classes(_tree(god), Path("m.py"))] == [Smell.GOD_CLASS]


def test_cohesion_counts_disjoint_method_clusters() -> None:
    """LCOM4 is the number of method groups sharing no state; 1 is cohesive."""
    cohesive = (
        "class C:\n"
        "    def a(self):\n        self.x = 1\n"
        "    def b(self):\n        return self.x\n"
        "    def c(self):\n        return self.x + 1\n"
    )
    assert detect_low_cohesion(_tree(cohesive), Path("m.py")) == []

    split = (
        "class C:\n"
        "    def a(self):\n        self.x = 1\n"
        "    def b(self):\n        return self.x\n"
        "    def c(self):\n        self.y = 2\n"
        "    def d(self):\n        return self.y\n"
    )
    findings = detect_low_cohesion(_tree(split), Path("m.py"))
    assert [(f.smell, f.measured) for f in findings] == [(Smell.LOW_COHESION, 2)]


def test_clone_detection_survives_renaming(tmp_path: Path) -> None:
    """Type-2 detection: the first thing anyone changes after pasting is a name."""
    block = "\n".join(f"    step{i} = value + {i}" for i in range(6))
    renamed = block.replace("step", "phase").replace("value", "other")
    trees = {
        tmp_path / "a.py": _tree(f"def a(value):\n{block}\n    return step0\n"),
        tmp_path / "b.py": _tree(f"def b(other):\n{renamed}\n    return phase0\n"),
    }
    findings = detect_duplicated_blocks(trees)
    assert [f.smell for f in findings] == [Smell.DUPLICATED_BLOCK]


def test_distinct_logic_is_not_reported_as_a_clone(tmp_path: Path) -> None:
    """Normalization erases names, not structure."""
    trees = {
        tmp_path / "a.py": _tree("def a():\n" + "\n".join(f"    x{i} = {i}" for i in range(6))),
        tmp_path / "b.py": _tree("def b():\n" + "\n".join(f"    if x{i}:\n        pass" for i in range(6))),
    }
    assert detect_duplicated_blocks(trees) == []


def test_import_cycle_is_detected(tmp_path: Path) -> None:
    """Cyclic modules load together and resist extraction."""
    trees = {
        tmp_path / "a.py": _tree("import b\n"),
        tmp_path / "b.py": _tree("import a\n"),
    }
    assert [f.smell for f in detect_import_cycles(trees)] == [Smell.IMPORT_CYCLE]

    acyclic = {tmp_path / "a.py": _tree("import b\n"), tmp_path / "b.py": _tree("x = 1\n")}
    assert detect_import_cycles(acyclic) == []


def test_unreferenced_symbol_detection_spares_test_modules(tmp_path: Path) -> None:
    """Nothing references a pytest function by name; the collector finds it by prefix."""
    trees = {
        tmp_path / "lib.py": _tree("def used():\n    return 1\n\ndef unused():\n    return 2\n"),
        tmp_path / "test_lib.py": _tree("from lib import used\n\ndef test_used():\n    assert used()\n"),
    }
    findings = detect_unreferenced_symbols(trees)
    assert [f.subject for f in findings] == ["unused"]


def test_string_reference_counts_as_use(tmp_path: Path) -> None:
    """A name reachable only through getattr or a registry is still reachable."""
    trees = {
        tmp_path / "lib.py": _tree("def handler():\n    return 1\n"),
        tmp_path / "registry.py": _tree("ROUTES = {'handler': 'handler'}\n"),
    }
    assert detect_unreferenced_symbols(trees) == []


def test_advisory_smells_never_gate(tmp_path: Path) -> None:
    """Measurement soundness and action confidence are different properties."""
    _write(tmp_path, "m.py", "class C:\n    def a(self):\n        self.x = 1\n"
           "    def b(self):\n        return self.x\n"
           "    def c(self):\n        self.y = 2\n    def d(self):\n        return self.y\n")
    report = analyze([tmp_path])
    assert any(f.smell is Smell.LOW_COHESION for f in report.advisory)
    assert all(f.smell in ADVISORY_SMELLS for f in report.advisory)
    assert Smell.LOW_COHESION not in {f.smell for f in report.gating}


def test_report_quantifies_before_it_judges(tmp_path: Path) -> None:
    """Every module is scored whether or not it breaches anything."""
    _write(tmp_path, "clean.py", "def f(x: int) -> int:\n    return x + 1\n")
    report = analyze([tmp_path])
    assert len(report.modules) == 1
    assert 0.0 <= report.mean_maintainability <= 100.0
    assert report.modules[0].halstead_volume > 0


def test_main_fails_on_a_missing_target(tmp_path: Path) -> None:
    """A path that does not exist must not be certified as clean."""
    assert main(["quantify", str(tmp_path / "absent"), "--fail-on", "none"]) == 1


def test_main_fail_on_switch_controls_the_exit_code(tmp_path: Path) -> None:
    """Advisory findings inform; only gating findings decide the build."""
    _write(tmp_path, "m.py", "def f(a, b, c, d, e, f_):\n    return a\n")
    assert main(["quantify", str(tmp_path), "--fail-on", "none"]) == 0
    assert main(["quantify", str(tmp_path), "--fail-on", "gating"]) == 1


def test_maintainability_threshold_matches_the_radon_scale() -> None:
    """20 is radon's A/B boundary; 65 belongs to the unnormalized SEI scale."""
    assert LOW_MAINTAINABILITY_INDEX == pytest.approx(20.0)
