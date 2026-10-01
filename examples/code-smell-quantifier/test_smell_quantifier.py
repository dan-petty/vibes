"""Unit tests for the Code Smell Quantifier."""

import ast
import itertools
from pathlib import Path

import pytest
from smell_quantifier import (
    ADVISORY_SMELLS,
    LOW_MAINTAINABILITY_INDEX,
    RevisionScore,
    Smell,
    TrendReport,
    _compute_rev_deltas,
    _count_disjoint_clusters,
    _normalize_polyglot_line,
    analyze,
    compute_trend,
    detect_cyclomatic_complexity,
    detect_duplicated_blocks,
    detect_god_classes,
    detect_import_cycles,
    detect_long_functions,
    detect_long_parameter_lists,
    detect_low_cohesion,
    detect_polyglot_clones,
    detect_unreferenced_symbols,
    main,
    module_metrics,
    polyglot_module_metrics,
    render_trend,
)


def _tree(source: str) -> ast.AST:
    return ast.parse(source)


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def test_module_metrics_come_from_radon_not_a_paraphrase() -> None:
    """Report the reference implementation's number, never a restatement of its formula."""
    from radon.metrics import mi_visit

    source = "def f(x):\n    return x + 1\n"
    maintainability, volume, complexity = module_metrics(source)
    assert maintainability == pytest.approx(round(mi_visit(source, multi=True), 1))
    assert volume > 0 and complexity >= 1


def test_maintainability_is_on_radons_normalized_scale() -> None:
    """A large tangled module must score below a small clean one, both within 0-100."""
    small, _, _ = module_metrics("def f(x):\n    return x + 1\n")
    sprawl = "\n".join(f"def f{i}(a, b):\n    return a if a > b else b" for i in range(200))
    large, _, _ = module_metrics(sprawl)
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


def test_unreferenced_symbols_carry_vultures_confidence(tmp_path: Path) -> None:
    """Confidence is the number a reader needs to decide whether deletion is safe."""
    _write(tmp_path, "lib.py", "import os\n\n\ndef used():\n    return 1\n")
    _write(tmp_path, "main.py", "from lib import used\n\nprint(used())\n")

    findings = detect_unreferenced_symbols([tmp_path], min_confidence=90)
    assert [f.subject for f in findings] == ["import os"]
    assert findings[0].measured == 90


def test_unreferenced_symbol_confidence_floor_filters(tmp_path: Path) -> None:
    """A floor above every reported confidence must yield nothing."""
    _write(tmp_path, "lib.py", "import os\n\n\ndef used():\n    return 1\n")
    assert detect_unreferenced_symbols([tmp_path], min_confidence=100) == []


def test_advisory_smells_never_gate(tmp_path: Path) -> None:
    """Measurement soundness and action confidence are different properties."""
    _write(
        tmp_path,
        "m.py",
        "class C:\n    def a(self):\n        self.x = 1\n"
        "    def b(self):\n        return self.x\n"
        "    def c(self):\n        self.y = 2\n    def d(self):\n        return self.y\n",
    )
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
    assert pytest.approx(20.0) == LOW_MAINTAINABILITY_INDEX


def test_method_calls_and_constants_are_not_instance_attributes() -> None:
    """`self.run()` is a call and `self.LIMIT` is a constant; neither is state.

    Counting every `self.X` reference as an attribute put nine classes over the ceiling
    when their real counts were at or below it, and each would have been a refactor of
    correct code.
    """
    source = (
        "class C:\n"
        "    LIMIT = 10\n"
        "    def __init__(self):\n"
        "        self.a = 1\n        self.b = 2\n"
        "    def run(self):\n"
        "        return self.helper() + self.LIMIT + self.a\n"
        "    def helper(self):\n        return self.b\n"
    )
    node = next(n for n in ast.walk(_tree(source)) if isinstance(n, ast.ClassDef))
    from smell_quantifier import _class_attribute_names, _class_attribute_references

    assert _class_attribute_names(node) == {"a", "b"}
    assert {"helper", "LIMIT"} <= _class_attribute_references(node)


def test_god_class_ceiling_counts_only_assigned_state() -> None:
    """A class with many collaborators is not a class with many attributes."""
    calls = "\n".join(f"        self.step{i}()" for i in range(12))
    defs = "\n".join(f"    def step{i}(self):\n        return {i}" for i in range(12))
    source = (
        f"class C:\n    def __init__(self):\n        self.only = 1\n    def run(self):\n{calls}\n{defs}\n"
    )
    assert detect_god_classes(_tree(source), Path("m.py")) == []


MUTUAL_CALL_CLASS = """
class Service:
    def __init__(self) -> None:
        self.store = {}

    def alpha(self) -> None:
        self.beta()

    def beta(self) -> None:
        self.store["k"] = 1

    def gamma(self) -> None:
        self.store["j"] = 2
"""


def test_a_calling_method_joins_the_cluster_of_the_method_it_calls() -> None:
    """`alpha` touches no attribute of its own; it is cohesive only through `beta`.

    The link was tested in one direction only, so whether these three methods formed one
    cluster or two depended on which one the traversal reached first.
    """
    tree = ast.parse(MUTUAL_CALL_CLASS)
    assert detect_low_cohesion(tree, Path("service.py")) == []


def test_cohesion_is_the_same_whatever_order_the_methods_arrive_in() -> None:
    """The same class must score the same LCOM4 however the traversal reaches it.

    `set.pop()` returns an arbitrary element and string hashing is randomised per process,
    so an order-dependent traversal reported a different number on each run. Permuting the
    input directly proves the property over every ordering, which four sampled hash seeds
    could only sample — and does it without spawning a subprocess per seed.
    """
    graph = {"a": {"x"}, "b": {"a"}, "c": {"x"}, "d": {"z"}, "e": {"d"}}
    results = {
        _count_disjoint_clusters({key: graph[key] for key in order})
        for order in itertools.permutations(graph)
    }
    assert results == {2}


# A module `ast.parse` accepts and radon's raw tokenizer rejects. The form feed inside a
# string literal is legal Python and stops radon's line iterator. Found by
# `tools/fuzz_harness.py` and kept at `artifacts/fuzz-corpus/smells/f54cb450570a8f93.case`.
RADON_HOSTILE = 'async def fetch_1(value1: int) -> int:\n    if value58 > 58:\n            return 81\n    "\x0c""Generated."'


def test_a_module_radon_cannot_read_does_not_abort_the_scan(tmp_path: Path) -> None:
    """One unscorable module previously took down the whole repository scan."""
    hostile = _write(tmp_path, "hostile.py", RADON_HOSTILE)
    healthy = _write(tmp_path, "healthy.py", "def f(x):\n    return x + 1\n")
    report = analyze([hostile, healthy], include_advisory=True)
    assert [Path(m.path).name for m in report.modules] == ["healthy.py"]


def test_an_unscorable_module_is_announced_rather_than_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A module silently missing from the score table looks exactly like one that scored well."""
    hostile = _write(tmp_path, "hostile.py", RADON_HOSTILE)
    analyze([hostile], include_advisory=True)
    reported = capsys.readouterr().err
    assert ("hostile.py" in reported, "not scored" in reported) == (True, True)


def test_ast_and_radon_disagree_which_is_why_the_guard_exists(tmp_path: Path) -> None:
    """The guard is only needed because the corpus filter and the scorer use two parsers."""
    ast.parse(RADON_HOSTILE)  # the corpus filter accepts it
    with pytest.raises(SyntaxError):
        module_metrics(RADON_HOSTILE)  # the scorer does not


# --- Agreement with pylint, whose thresholds these are --------------------------------------


def _gating(source: str, tmp_path: Path) -> list[str]:
    """Return the gating smells the quantifier reports for one module."""
    module = tmp_path / "sample.py"
    module.write_text(source, encoding="utf-8")
    report = analyze([module], include_advisory=False)
    return [f"{f.smell.name}:{f.measured}" for f in report.findings]


def test_a_private_helper_is_not_a_public_method(tmp_path: Path) -> None:
    """R0904 is `max-public-methods`, and pylint counts only names not starting with `_`.

    Counting private helpers against a public-method ceiling punishes the decomposition the
    ceiling exists to encourage: this facade scored 21 here and nothing under real pylint.
    """
    source = "class Facade:\n" + "".join(f"    def pub{i:02d}(self): pass\n" for i in range(1, 11))
    source += "".join(f"    def _h{i:02d}(self): pass\n" for i in range(1, 12))
    assert _gating(source, tmp_path) == []


def test_star_args_are_not_counted_as_parameters(tmp_path: Path) -> None:
    """R0913 excludes `*args` and `**kwargs`; every forwarding wrapper was flagged for them."""
    assert _gating("def wide(a, b, c, d, e, *args, **kwargs):\n    return a\n", tmp_path) == []


def test_the_def_and_its_docstring_are_not_statements(tmp_path: Path) -> None:
    """R0915 counts neither, so a 49-statement function measured 51 against a ceiling of 50."""
    source = 'def f():\n    """Doc."""\n' + "".join(f"    x{i} = {i}\n" for i in range(48)) + "    return 1\n"
    assert _gating(source, tmp_path) == []


def test_one_statement_over_the_ceiling_is_still_reported(tmp_path: Path) -> None:
    """Aligning with pylint must not turn the detector off."""
    source = 'def f():\n    """Doc."""\n' + "".join(f"    x{i} = {i}\n" for i in range(51)) + "    return 1\n"
    assert _gating(source, tmp_path) == ["LONG_FUNCTION:52"]


def test_a_package_relative_submodule_import_draws_an_edge(tmp_path: Path) -> None:
    """The commonest circular import in a package drew no edge at all.

    `from pkg import b` was read as an import of `pkg`, never of `b`, so `pkg/a.py` and
    `pkg/b.py` importing each other produced an empty graph. CPython refuses that program
    at runtime; this detector called it clean.
    """
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "a.py").write_text("from pkg import b\n\n\ndef fa():\n    return b\n", encoding="utf-8")
    (package / "b.py").write_text("from pkg import a\n\n\ndef fb():\n    return a\n", encoding="utf-8")
    cycles = [
        f for f in analyze([package], include_advisory=False).findings if f.smell.name == "IMPORT_CYCLE"
    ]
    assert len(cycles) == 1


def test_detect_cyclomatic_complexity_clean_function() -> None:
    """A low-complexity function produces zero findings."""
    source = "def simple(a, b):\n    return a + b\n"
    findings = detect_cyclomatic_complexity(_tree(source), Path("simple.py"))
    assert (len(findings), findings) == (0, [])


def test_detect_cyclomatic_complexity_flags_high_complexity() -> None:
    """Functions exceeding MAX_CYCLOMATIC_COMPLEXITY are flagged with rank and measured value."""
    lines = ["def complex_fn(x):"]
    for i in range(11):
        lines.append(f"    if x == {i}: return {i}")
    lines.append("    return -1\n")
    source = "\n".join(lines)
    findings = detect_cyclomatic_complexity(_tree(source), Path("complex.py"), max_complexity=10)
    assert (
        len(findings),
        findings[0].smell,
        findings[0].subject,
        findings[0].measured,
        findings[0].threshold,
        "C" in findings[0].detail,
    ) == (1, Smell.HIGH_CYCLOMATIC_COMPLEXITY, "complex_fn", 12.0, 10.0, True)


def test_cyclomatic_complexity_in_gating_analysis(tmp_path: Path) -> None:
    """High cyclomatic complexity is a gating smell reported by analyze()."""
    lines = ["def complex_fn(x):"]
    for i in range(11):
        lines.append(f"    if x == {i}: return {i}")
    lines.append("    return -1\n")
    source = "\n".join(lines)
    assert _gating(source, tmp_path) == ["HIGH_CYCLOMATIC_COMPLEXITY:12.0"]


def test_polyglot_line_normalization_erases_identifiers_and_literals() -> None:
    """Polyglot line normalization strips names/values while preserving structural tokens."""
    js_line = "function calculateBonus(userSalary, multiplier) {"
    ts_line = "const total: number = baseAmount * 1.05;"
    go_line = 'if err != nil { return fmt.Errorf("failed: %v", err) }'
    comment = "// this is a comment"

    assert (
        _normalize_polyglot_line(js_line, "javascript"),
        _normalize_polyglot_line(ts_line, "typescript"),
        _normalize_polyglot_line(go_line, "go"),
        _normalize_polyglot_line(comment, "javascript"),
    ) == (
        "function <ID> ( <ID> , <ID> ) {",
        "const <ID> : <ID> = <ID> * <LIT> ;",
        "if <ID> != <ID> { return <ID> . <ID> ( <LIT> , <ID> ) }",
        "",
    )


def test_polyglot_clone_detection_finds_type2_duplicates_across_ts_files(tmp_path: Path) -> None:
    """Type-2 clone detection matches structural duplicates across non-Python files."""
    code_a = (
        "function handleFirst(a, b) {\n"
        "    if (a > 0) {\n"
        "        const val = a * b;\n"
        "        return val + 10;\n"
        "    }\n"
        "    return 0;\n"
        "}\n"
    )
    code_b = (
        "function handleSecond(x, y) {\n"
        "    if (x > 0) {\n"
        "        const res = x * y;\n"
        "        return res + 10;\n"
        "    }\n"
        "    return 0;\n"
        "}\n"
    )
    file_a = _write(tmp_path, "first.ts", code_a)
    file_b = _write(tmp_path, "second.ts", code_b)

    findings = detect_polyglot_clones({file_a: code_a, file_b: code_b}, size=6)
    assert (
        len(findings),
        findings[0].smell,
        findings[0].measured,
        findings[0].threshold,
    ) == (1, Smell.DUPLICATED_BLOCK, 2, 1)


def test_polyglot_clone_detection_ignores_distinct_code(tmp_path: Path) -> None:
    """Distinct polyglot files do not report spurious clone findings."""
    code_a = "func A() int {\n    x := 1\n    y := 2\n    return x + y\n}\n"
    code_b = "func B(msg string) bool {\n    println(msg)\n    return true\n}\n"
    file_a = _write(tmp_path, "a.go", code_a)
    file_b = _write(tmp_path, "b.go", code_b)

    findings = detect_polyglot_clones({file_a: code_a, file_b: code_b}, size=4)
    assert (len(findings), findings) == (0, [])


def test_polyglot_module_metrics_normalized_scale() -> None:
    """Polyglot metrics calculate LOC, Halstead volume, and normalized MI on 0-100 scale."""
    clean_ts = "function add(a: number, b: number): number {\n    return a + b;\n}\n"
    tangled_ts = "\n".join(
        f"function fn{i}(x: number): number {{ if (x > {i}) {{ return x * 2; }} else {{ return 0; }} }}"
        for i in range(50)
    )

    clean_mi, clean_vol, clean_cc = polyglot_module_metrics(clean_ts, "typescript")
    tangled_mi, tangled_vol, tangled_cc = polyglot_module_metrics(tangled_ts, "typescript")

    assert (
        0.0 <= tangled_mi < clean_mi <= 100.0,
        clean_vol > 0.0,
        tangled_vol > clean_vol,
        clean_cc,
        tangled_cc > clean_cc,
    ) == (True, True, True, 1, True)


def test_analyze_scores_polyglot_files_in_advisory_mode(tmp_path: Path) -> None:
    """analyze() discovers and scores polyglot modules alongside Python."""
    _write(tmp_path, "worker.py", "def work():\n    return 42\n")
    _write(
        tmp_path,
        "handler.ts",
        "export function run(req: any): any {\n    return req.body;\n}\n",
    )

    report = analyze([tmp_path], include_advisory=True, languages=["all"])
    scored_names = {Path(m.path).name for m in report.modules}

    assert (
        "worker.py" in scored_names,
        "handler.ts" in scored_names,
        len(report.modules),
        0.0 <= report.mean_maintainability <= 100.0,
    ) == (True, True, 2, True)


def test_analyze_gates_on_excessive_polyglot_complexity(tmp_path: Path) -> None:
    """Excessive cyclomatic complexity in polyglot files is reported in report.gating."""
    branches = "\n".join(f"    if (x == {i}) {{ return {i}; }}" for i in range(12))
    complex_ts = f"function evalNum(x: number): number {{\n{branches}\n    return -1;\n}}\n"
    _write(tmp_path, "complex.ts", complex_ts)

    report = analyze([tmp_path], include_advisory=False, languages=["all"])
    gating_smells = [f.smell for f in report.gating]

    assert (
        Smell.HIGH_CYCLOMATIC_COMPLEXITY in gating_smells,
        len(report.gating),
    ) == (True, 1)


def test_polyglot_languages_filter_selectively_scans(tmp_path: Path) -> None:
    """Specifying languages filter restricts scanning to the requested extensions."""
    _write(tmp_path, "app.ts", "const x = 1;\n")
    _write(tmp_path, "lib.rs", "fn run() -> i32 { 1 }\n")

    report_ts = analyze([tmp_path], languages=["ts"])
    report_rs = analyze([tmp_path], languages=["rs"])

    scored_ts = [Path(m.path).name for m in report_ts.modules]
    scored_rs = [Path(m.path).name for m in report_rs.modules]

    assert (
        scored_ts,
        scored_rs,
    ) == (["app.ts"], ["lib.rs"])


def test_cli_languages_argument_parsing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI --languages argument properly filters target analysis."""
    _write(tmp_path, "service.go", "package main\n\nfunc main() {}\n")
    exit_code = main(["quantify", "--languages", "go", "--json", str(tmp_path)])
    out = capsys.readouterr().out

    assert (
        exit_code,
        "service.go" in out,
        "mean_maintainability" in out,
    ) == (0, True, True)


def test_compute_rev_deltas() -> None:
    """_compute_rev_deltas calculates Maintainability Index, LOC, and finding count deltas."""
    prev = RevisionScore(
        commit="1111111",
        short_sha="1111111",
        subject="Initial commit",
        mean_maintainability=80.0,
        total_loc=100,
        module_count=2,
        gating_findings=1,
        advisory_findings=2,
    )
    delta_none = _compute_rev_deltas(85.0, 120, 5, prev=None)
    delta_prev = _compute_rev_deltas(85.0, 120, 5, prev=prev)

    assert (delta_none, delta_prev) == ((0.0, 0, 0), (5.0, 20, 2))


def test_compute_trend_returns_trajectory() -> None:
    """compute_trend calculates trajectory metrics across historical git revisions."""
    target = Path("examples/code-smell-quantifier/smell_quantifier.py")
    report = compute_trend([target], revisions_count=3)

    assert (
        len(report.revisions) > 0,
        all(isinstance(r.short_sha, str) and len(r.short_sha) > 0 for r in report.revisions),
        all(r.mean_maintainability > 0.0 for r in report.revisions),
        all(r.module_count >= 1 for r in report.revisions),
    ) == (True, True, True, True)


def test_render_trend_outputs_table_and_summary() -> None:
    """render_trend outputs formatted ASCII trajectory table and summary lines."""
    revs = [
        RevisionScore("1111111", "1111111", "First commit", 80.0, 100, 1, 0, 1, 0.0, 0, 0),
        RevisionScore("2222222", "2222222", "Second commit", 85.0, 120, 1, 0, 1, 5.0, 20, 0),
    ]
    lines = render_trend(TrendReport(revisions=revs))
    output = "\n".join(lines)

    assert (
        "CODE HEALTH TREND TRAJECTORY" in output,
        "1111111" in output,
        "2222222" in output,
        "Trajectory (2 revs):" in output,
    ) == (True, True, True, True)


def test_cli_trend_json_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI --trend with --json emits machine-readable trajectory payload."""
    exit_code = main(
        ["quantify", "--trend", "2", "--json", "examples/code-smell-quantifier/smell_quantifier.py"]
    )
    out = capsys.readouterr().out

    assert (
        exit_code,
        '"trend":' in out,
        '"mean_maintainability":' in out,
        '"delta_maintainability":' in out,
    ) == (0, True, True, True)
