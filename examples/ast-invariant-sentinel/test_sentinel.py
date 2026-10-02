"""Unit tests for AST Invariant Sentinel."""

# sentinel: allow[ZeroTrustSanitization] — negative fixtures asserting this sentinel detects private IPs and subdomains

import ast
import io
import tempfile
from pathlib import Path

import pytest
import radon.complexity as cc
from sentinel import (
    ALL_INVARIANT_RULES,
    CANONICAL_MOCK_DOMAIN,
    PRESETS,
    SAFE_DOC_REPLACEMENT_IP,
    ComplexityVisitor,
    ConfigOverrides,
    SentinelConfig,
    _read_lsp_message,
    _write_lsp_message,
    audit_file,
    audit_source,
    audit_targets,
    auto_fix_file,
    auto_fix_source,
    auto_fix_targets,
    load_toml_config,
    main,
    normalize_rule_name,
    parse_cli_args,
    parse_cli_run_options,
    render_presets_table,
    resolve_config,
    run_lsp_server,
)


def test_clean_code_passes_all_invariants():
    code = """
def simple_calculator(a: int, b: int, op: str) -> int:
    operations = {
        "+": lambda x, y: x + y,
        "-": lambda x, y: x - y,
        "*": lambda x, y: x * y,
    }
    handler = operations.get(op)
    if not handler:
        raise ValueError("Invalid operator")
    return handler(a, b)
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = Path(tmp.name)

    try:
        violations = audit_file(tmp_path)
        assert len(violations) == 0
    finally:
        tmp_path.unlink()


def test_high_complexity_is_flagged():
    # Construct a function with cyclomatic complexity > 10
    code = """
def complex_spaghetti(x: int) -> int:
    if x == 1:
        return 1
    elif x == 2:
        return 2
    elif x == 3:
        return 3
    elif x == 4:
        return 4
    elif x == 5:
        return 5
    elif x == 6:
        return 6
    elif x == 7:
        return 7
    elif x == 8:
        return 8
    elif x == 9:
        return 9
    elif x == 10:
        return 10
    elif x == 11:
        return 11
    return 0
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = Path(tmp.name)

    try:
        violations = audit_file(tmp_path, max_complexity=10, max_depth=20)
        assert len(violations) == 1
        assert violations[0].invariant == "CyclomaticComplexity"
        assert violations[0].metric_value == 12
    finally:
        tmp_path.unlink()


def test_deep_nesting_is_flagged():
    # Construct a function with 6 levels of nesting
    code = """
def deeply_nested(items):
    for a in items:
        if a > 0:
            for b in items:
                if b > 0:
                    for c in items:
                        if c > 0:
                            print(a, b, c)
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = Path(tmp.name)

    try:
        violations = audit_file(tmp_path, max_depth=5)
        assert len(violations) == 1
        assert violations[0].invariant == "NestingDepth"
        assert violations[0].metric_value == 7
    finally:
        tmp_path.unlink()


def test_private_ip_leakage_is_flagged():
    code = 'DATABASE_URL = "http://192.168.1.105:5432/db"\n'
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = Path(tmp.name)

    try:
        violations = audit_file(tmp_path)
        assert len(violations) == 1
        assert violations[0].invariant == "ZeroTrustSanitization"
        assert "192.168.1.105" in violations[0].message
    finally:
        tmp_path.unlink()


def test_rfc5737_documentation_ip_is_permitted():
    code = 'MOCK_API = "http://198.51.100.25:8080/v1"\n'
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = Path(tmp.name)

    try:
        violations = audit_file(tmp_path)
        assert len(violations) == 0
    finally:
        tmp_path.unlink()


def test_subdomain_mock_domain_is_flagged():
    code = 'ENDPOINT = "https://api.example.com/health"\n'
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = Path(tmp.name)

    try:
        violations = audit_file(tmp_path)
        assert len(violations) == 1
        assert violations[0].invariant == "ZeroTrustSanitization"
        assert "api.example.com" in violations[0].message
    finally:
        tmp_path.unlink()


def _write_module(directory: Path, name: str, code: str) -> Path:
    path = directory / name
    path.write_text(code)
    return path


def test_audit_targets_scans_every_supplied_path(tmp_path):
    """Pre-commit passes N filenames; every one must be audited, not just the first."""
    clean = _write_module(tmp_path, "clean.py", "def ok() -> int:\n    return 1\n")
    dirty = _write_module(tmp_path, "dirty.py", 'HOST = "http://192.168.1.10:8080"\n')

    report = audit_targets([clean, dirty])
    assert (report.files_checked, len(report.violations)) == (2, 1)
    assert report.violations[0].invariant == "ZeroTrustSanitization"


def test_main_audits_all_cli_paths_and_fails(tmp_path, capsys):
    """A violation in any trailing argument must fail the run, never be silently dropped."""
    clean = _write_module(tmp_path, "clean.py", "def ok() -> int:\n    return 1\n")
    dirty = _write_module(tmp_path, "dirty.py", 'HOST = "http://10.1.2.3"\n')

    exit_code = main(["sentinel.py", str(clean), str(dirty)])
    assert exit_code == 1
    assert "Checked 2 Python files." in capsys.readouterr().out


def test_audit_targets_deduplicates_overlapping_paths(tmp_path):
    """A directory plus one of its own files must not double-count that file."""
    module = _write_module(tmp_path, "solo.py", "def ok() -> int:\n    return 1\n")

    report = audit_targets([tmp_path, module])
    assert (report.files_checked, report.is_clean) == (1, True)


def test_main_defaults_to_current_directory_without_paths(tmp_path, monkeypatch):
    """Bare invocation must retain its recursive whole-repository default."""
    _write_module(tmp_path, "clean.py", "def ok() -> int:\n    return 1\n")
    monkeypatch.chdir(tmp_path)

    assert main(["sentinel.py"]) == 0


def test_justified_waiver_suppresses_sanitization_violation(tmp_path):
    """A detector's own negative fixtures may waive sanitization with a written reason."""
    module = _write_module(
        tmp_path,
        "fixtures.py",
        '"""Fixtures."""\n'
        "# sentinel: allow[ZeroTrustSanitization] — negative fixture for the detector\n"
        'HOST = "http://192.168.1.10"\n',
    )
    assert audit_file(module) == []


def test_waiver_without_justification_is_itself_a_violation(tmp_path):
    """An unexplained waiver must fail loudly instead of quietly disabling the gate."""
    module = _write_module(
        tmp_path,
        "lazy.py",
        '"""Lazy."""\n# sentinel: allow[ZeroTrustSanitization]\nHOST = "http://192.168.1.10"\n',
    )
    violations = audit_file(module)
    assert [v.invariant for v in violations] == ["WaiverIntegrity", "ZeroTrustSanitization"]


def test_structural_invariants_are_never_waivable(tmp_path):
    """Complexity and nesting caps must not be opt-out; only content detectors are."""
    body = "\n".join(f"    if x == {n}:\n        return {n}" for n in range(12))
    module = _write_module(
        tmp_path,
        "sprawl.py",
        f'"""Sprawl."""\n# sentinel: allow[CyclomaticComplexity] — please let this one through\ndef f(x: int) -> int:\n{body}\n    return 0\n',
    )
    invariants = {v.invariant for v in audit_file(module)}
    assert {"WaiverIntegrity", "CyclomaticComplexity"} <= invariants


def test_waiver_inside_string_literal_is_not_honored(tmp_path):
    """Waivers are parsed as comment tokens, so text inside a literal cannot disarm the gate."""
    module = _write_module(
        tmp_path,
        "sneaky.py",
        '"""Doc."""\nSAMPLE = "# sentinel: allow[ZeroTrustSanitization] — smuggled in a literal"\n'
        'HOST = "http://192.168.1.10"\n',
    )
    assert [v.invariant for v in audit_file(module)] == ["ZeroTrustSanitization"]


def test_waiver_below_module_header_is_not_honored(tmp_path):
    """Waivers must be declared in the module header where reviewers will see them.

    The waiver is still not honoured — that is the rule. What changed is that the file now
    also reports *why*: a pragma one line too low used to be ignored in silence, so the
    author read a message about the code and none about the waiver. That cost an hour once,
    during the change that consolidated the address policy.
    """
    padding = "\n".join(f"CONST_{n} = {n}" for n in range(20))
    module = _write_module(
        tmp_path,
        "buried.py",
        f'"""Doc."""\n{padding}\n# sentinel: allow[ZeroTrustSanitization] — buried far below the header\n'
        'HOST = "http://192.168.1.10"\n',
    )
    invariants = [v.invariant for v in audit_file(module)]
    assert sorted(invariants) == ["WaiverIntegrity", "ZeroTrustSanitization"]


def test_directory_sweep_audits_test_files(tmp_path):
    """Test modules are audited like any other source; §10.2 caps their complexity too."""
    _write_module(tmp_path, "test_thing.py", 'HOST = "http://192.168.1.10"\n')

    report = audit_targets([tmp_path])
    assert (report.files_checked, len(report.violations)) == (1, 1)


def test_missing_target_fails_instead_of_certifying_nothing(tmp_path):
    """A typo'd or deleted path must fail rather than report a clean audit of zero files."""
    report = audit_targets([tmp_path / "does_not_exist.py"])
    assert (report.files_checked, report.is_clean) == (0, False)
    assert report.violations[0].invariant == "TargetIntegrity"


def test_cloud_metadata_address_is_nameable(tmp_path):
    """A rule that forbids naming the metadata endpoint forbids defending against it."""
    module = _write_module(tmp_path, "guard.py", 'BLOCKED = "169.254.169.254"\n')
    assert audit_file(module) == []


def test_surrounding_link_local_range_is_still_flagged(tmp_path):
    """Only the well-known constant is exempt, not the autoconfigured network around it."""
    module = _write_module(tmp_path, "leak.py", 'HOST = "169.254.12.34"\n')
    assert [v.invariant for v in audit_file(module)] == ["ZeroTrustSanitization"]


def test_a_subdomain_after_a_compliant_url_is_still_caught(tmp_path: Path) -> None:
    """The detector read only the first URL in a literal.

    A string holding a compliant endpoint followed by a subdomain was reported clean, and a
    docstring or fixture naming two endpoints is the ordinary case. The scan that certified
    this repository therefore certified strings it had only partly read.
    """
    source = tmp_path / "mod.py"
    source.write_text(
        'DOCS = "primary https://example.com/a, mirror https://mirror.example.com/b"\n',
        encoding="utf-8",
    )
    report = audit_targets([source])
    assert [v.invariant for v in report.violations] == ["ZeroTrustSanitization"]
    assert "mirror.example.com" in report.violations[0].message


# --- Agreement with the reference implementation -------------------------------------------

RADON_CORPUS: dict[str, str] = {
    "plain": "def p(a):\n    return a\n",
    "if_else": "def w(a):\n    if a:\n        pass\n    else:\n        pass\n",
    "elif_chain": "def w(a):\n    if a == 1: return 1\n    elif a == 2: return 2\n    else: return 3\n",
    "boolop": "def w(a, b, c):\n    return a and b and c\n",
    "listcomp": "def f(rows):\n    return [r for r in rows if r.a if r.b]\n",
    "genexp": "def k(xs):\n    return sum(x for x in xs if x)\n",
    "dictcomp": "def m(xs):\n    return {k: v for k, v in xs if k if v}\n",
    "nested_comp": "def n(xs):\n    return [y for x in xs for y in x if y]\n",
    "while_else": "def w(a):\n    while a:\n        a -= 1\n    else:\n        pass\n",
    "for_else": "def w(a):\n    for i in a:\n        pass\n    else:\n        pass\n",
    "try_else": "def w(a):\n    try:\n        pass\n    except ValueError:\n        pass\n    else:\n        pass\n",
    "try_two_handlers": "def w(a):\n    try:\n        pass\n    except ValueError:\n        pass\n    except KeyError:\n        pass\n",
    "match_plain": 'def d(c):\n    match c:\n        case "a": return 1\n        case "b": return 2\n        case _: return 0\n',
    "match_guard": "def g(x):\n    match x:\n        case int() if x > 0: return 1\n        case _: return 0\n",
    "match_no_wildcard": "def g(x):\n    match x:\n        case 1: return 1\n        case 2: return 2\n",
    "match_eleven": (
        "def d(c):\n    match c:\n"
        + "".join(f'        case "{chr(97 + i)}": return {i}\n' for i in range(11))
        + "        case _: return -1\n"
    ),
    "ternary": "def w(a):\n    return 1 if a else 2\n",
    "assert_stmt": "def w(a):\n    assert a\n",
    "with_stmt": "def w(a):\n    with open(a) as f:\n        return f.read()\n",
}


def _radon_complexity(path: Path) -> int:
    """Return the complexity radon reports for the single function in a file."""
    blocks = cc.cc_visit(path.read_text(encoding="utf-8"))
    return blocks[0].complexity


@pytest.mark.parametrize("name", sorted(RADON_CORPUS))
def test_complexity_agrees_with_radon(name: str, tmp_path: Path) -> None:
    """The gate is measured against the tool it cites, not against a restatement of the rules.

    This is the durable half of the fix. The previous implementation carried a hand-written
    list of branching constructs that stopped at `If`/`While`/`For`/`ExceptHandler`/
    `Assert`/`IfExp`/`BoolOp`, and the language moved on without it: comprehensions and
    `match` both scored zero, so four functions in this repository passed the M<=10 gate
    while radon scored them 11 to 13, and an eleven-case dispatcher measured 1. A list of
    constructs falls behind; an agreement test does not.
    """
    source = tmp_path / f"{name}.py"
    source.write_text(RADON_CORPUS[name], encoding="utf-8")
    visitor = ComplexityVisitor(str(source))
    mine = visitor._calculate_complexity(ast.parse(RADON_CORPUS[name]).body[0])
    assert mine == _radon_complexity(source)


def test_the_gate_is_never_laxer_than_its_reference(tmp_path: Path) -> None:
    """One deliberate divergence, stated rather than discovered.

    radon scores `except*` as zero because it predates PEP 654. A handler is a branch, so
    it is counted here — and a gate stricter than its reference is safe, where a laxer one
    is the defect this was written to fix.
    """
    source = tmp_path / "starred.py"
    body = "def w(a):\n    try:\n        pass\n    except* ValueError:\n        pass\n"
    source.write_text(body, encoding="utf-8")
    mine = ComplexityVisitor(str(source))._calculate_complexity(ast.parse(body).body[0])
    assert (mine, _radon_complexity(source)) == (2, 1)


def test_render_presets_table_contains_all_presets() -> None:
    """The preset table lists every available preset name and description."""
    table = render_presets_table()
    present = tuple(
        name in table
        for name in ("standard", "strict", "relaxed", "pedantic", "security_only", "structural_only")
    )
    assert (len(PRESETS), all(present)) == (6, True)


def test_main_list_presets(capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI entrypoint with --list-presets renders the table and exits cleanly."""
    exit_code = main(["sentinel.py", "--list-presets"])
    captured = capsys.readouterr().out
    assert (exit_code, "Available AST Invariant Sentinel Presets:" in captured) == (0, True)


def test_normalize_rule_names_and_aliases() -> None:
    """Rule aliases and code identifiers normalize to canonical invariant names."""
    cases = (
        ("cc001", "CyclomaticComplexity"),
        ("complexity", "CyclomaticComplexity"),
        ("c901", "CyclomaticComplexity"),
        ("nd001", "NestingDepth"),
        ("nesting", "NestingDepth"),
        ("zt001", "ZeroTrustSanitization"),
        ("security", "ZeroTrustSanitization"),
        ("sanitization", "ZeroTrustSanitization"),
        ("si001", "SyntaxIntegrity"),
        ("ti001", "TargetIntegrity"),
        ("wi001", "WaiverIntegrity"),
    )
    results = tuple(normalize_rule_name(alias) for alias, _ in cases)
    expected = tuple(canonical for _, canonical in cases)
    assert results == expected


def test_sentinel_config_defaults() -> None:
    """SentinelConfig provides standard defaults and rule activation lookups."""
    cfg = SentinelConfig()
    assert (
        cfg.preset_name,
        cfg.max_complexity,
        cfg.max_depth,
        cfg.active_rules == ALL_INVARIANT_RULES,
        cfg.is_rule_active("CC001"),
        cfg.is_rule_active("unknown"),
    ) == ("standard", 10, 5, True, True, False)


def test_resolve_config_defaults_and_overrides() -> None:
    """Config resolution honors presets and applies granular overrides."""
    std = resolve_config()
    strict = resolve_config(ConfigOverrides(preset_name="strict"))
    custom = resolve_config(
        ConfigOverrides(
            preset_name="strict",
            max_complexity=8,
            max_depth=4,
            select=["complexity"],
            extend_select=["sanitization"],
            ignore=["waivers"],
        )
    )
    assert (
        (std.preset_name, std.max_complexity, std.max_depth),
        (strict.preset_name, strict.max_complexity, strict.max_depth),
        (custom.preset_name, custom.max_complexity, custom.max_depth),
        custom.active_rules == frozenset({"CyclomaticComplexity", "ZeroTrustSanitization"}),
    ) == (
        ("standard", 10, 5),
        ("strict", 6, 3),
        ("strict", 8, 4),
        True,
    )


def test_strict_preset_flags_mid_complexity(tmp_path: Path) -> None:
    """The strict preset flags functions with M=7 that pass the standard M<=10 ceiling."""
    source = tmp_path / "mid_complexity.py"
    code = (
        "def mid_func(x: int) -> int:\n"
        "    if x == 1: return 1\n"
        "    if x == 2: return 2\n"
        "    if x == 3: return 3\n"
        "    if x == 4: return 4\n"
        "    if x == 5: return 5\n"
        "    if x == 6: return 6\n"
        "    return 0\n"
    )
    source.write_text(code, encoding="utf-8")
    std_violations = audit_file(source, config=resolve_config(ConfigOverrides(preset_name="standard")))
    strict_violations = audit_file(source, config=resolve_config(ConfigOverrides(preset_name="strict")))
    assert (
        len(std_violations),
        len(strict_violations),
        strict_violations[0].invariant if strict_violations else "",
    ) == (0, 1, "CyclomaticComplexity")


def test_pedantic_preset_flags_low_complexity(tmp_path: Path) -> None:
    """The pedantic preset enforces M<=4 and flags functions with M=5."""
    source = tmp_path / "low_complexity.py"
    code = (
        "def func(x: int) -> int:\n"
        "    if x == 1: return 1\n"
        "    if x == 2: return 2\n"
        "    if x == 3: return 3\n"
        "    if x == 4: return 4\n"
        "    return 0\n"
    )
    source.write_text(code, encoding="utf-8")
    pedantic_cfg = resolve_config(ConfigOverrides(preset_name="pedantic"))
    strict_cfg = resolve_config(ConfigOverrides(preset_name="strict"))
    pedantic_v = audit_file(source, config=pedantic_cfg)
    strict_v = audit_file(source, config=strict_cfg)
    assert (len(strict_v), len(pedantic_v), pedantic_v[0].threshold if pedantic_v else 0) == (0, 1, 4)


def test_relaxed_preset_allows_higher_complexity(tmp_path: Path) -> None:
    """The relaxed preset permits legacy functions with M=12 that breach standard M<=10."""
    source = tmp_path / "legacy.py"
    code = (
        "def legacy(x: int) -> int:\n"
        + "".join(f"    if x == {i}: return {i}\n" for i in range(1, 12))
        + "    return 0\n"
    )
    source.write_text(code, encoding="utf-8")
    std_v = audit_file(source, config=resolve_config(ConfigOverrides(preset_name="standard")))
    relaxed_v = audit_file(source, config=resolve_config(ConfigOverrides(preset_name="relaxed")))
    assert (len(std_v), len(relaxed_v)) == (1, 0)


def test_security_only_preset_ignores_complexity(tmp_path: Path) -> None:
    """The security_only preset ignores complex control flow but flags private host IPs."""
    source = tmp_path / "sec_check.py"
    code = (
        "def complex_with_ip(x: int) -> str:\n"
        + "".join(f"    if x == {i}: return 'step'\n" for i in range(1, 12))
        + "    return 'https://api.example.com/resource'\n"
    )
    source.write_text(code, encoding="utf-8")
    sec_cfg = resolve_config(ConfigOverrides(preset_name="security_only"))
    violations = audit_file(source, config=sec_cfg)
    assert (len(violations), violations[0].invariant) == (1, "ZeroTrustSanitization")


def test_structural_only_preset_ignores_sanitization(tmp_path: Path) -> None:
    """The structural_only preset ignores IP leaks but flags complexity/nesting violations."""
    source = tmp_path / "struct_check.py"
    code = "def simple_with_ip() -> str:\n    return 'https://api.example.com/resource'\n"
    source.write_text(code, encoding="utf-8")
    struct_cfg = resolve_config(ConfigOverrides(preset_name="structural_only"))
    violations = audit_file(source, config=struct_cfg)
    assert len(violations) == 0


def test_cli_parsing_presets_and_rule_selection(tmp_path: Path) -> None:
    """CLI flag parsing wires presets, explicit selection, and ignore lists correctly."""
    targets, config, is_list = parse_cli_args(
        [
            "sentinel.py",
            str(tmp_path),
            "--preset",
            "strict",
            "--select",
            "CC001,ZT001",
            "--ignore",
            "sanitization",
            "--max-complexity",
            "5",
            "--max-depth",
            "2",
        ]
    )
    assert (
        is_list,
        targets[0],
        config.preset_name,
        config.max_complexity,
        config.max_depth,
        config.active_rules,
    ) == (
        False,
        tmp_path,
        "strict",
        5,
        2,
        frozenset({"CyclomaticComplexity"}),
    )


def test_toml_config_loading_and_override(tmp_path: Path) -> None:
    """TOML configuration in pyproject.toml is loaded and overridden by CLI flags."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.sentinel]\n"
        'preset = "strict"\n'
        "max_complexity = 7\n"
        "max_depth = 3\n"
        'select = ["complexity", "nesting"]\n',
        encoding="utf-8",
    )
    loaded = load_toml_config(pyproject)
    cfg = resolve_config(ConfigOverrides(config_file=pyproject))
    override_cfg = resolve_config(ConfigOverrides(config_file=pyproject, max_complexity=9))
    assert (
        (loaded.get("preset"), loaded.get("max_complexity")),
        (cfg.preset_name, cfg.max_complexity, cfg.max_depth),
        cfg.active_rules,
        override_cfg.max_complexity,
    ) == (
        ("strict", 7),
        ("strict", 7, 3),
        frozenset({"CyclomaticComplexity", "NestingDepth"}),
        9,
    )


def test_target_integrity_disabled_via_select(tmp_path: Path) -> None:
    """When TargetIntegrity is not selected, missing paths do not fail the audit."""
    missing = tmp_path / "nonexistent.py"
    normal_report = audit_targets([missing])
    cfg = resolve_config(ConfigOverrides(select=["complexity"]))
    filtered_report = audit_targets([missing], config=cfg)
    assert (
        normal_report.is_clean,
        len(normal_report.violations),
        normal_report.violations[0].invariant,
        filtered_report.is_clean,
        len(filtered_report.violations),
    ) == (
        False,
        1,
        "TargetIntegrity",
        True,
        0,
    )


def test_auto_fix_source_remediates_private_ip() -> None:
    """auto_fix_source rewrites private host IPs to safe RFC 5737 documentation addresses."""
    bad_ip = "192." + "168.1.10"
    source = f'ENDPOINT = "http://{bad_ip}:8080/api"\n'
    fixed, count = auto_fix_source(source)
    assert (
        count,
        SAFE_DOC_REPLACEMENT_IP in fixed,
        bad_ip not in fixed,
        len(audit_source(fixed)),
    ) == (1, True, True, 0)


def test_auto_fix_source_remediates_subdomain() -> None:
    """auto_fix_source rewrites non-canonical example.com subdomains to canonical example.com."""
    source = 'URL = "https://api.example.com/v1/resource"\n'
    fixed, count = auto_fix_source(source)
    assert (
        count,
        f"https://{CANONICAL_MOCK_DOMAIN}/v1/resource" in fixed,
        len(audit_source(fixed)),
    ) == (1, True, 0)


def test_auto_fix_source_remediates_waiver_justification() -> None:
    """auto_fix_source appends compliant justification to bare or truncated waiver pragmas."""
    source = "# sentinel: allow[ZeroTrustSanitization]\n"
    fixed, count = auto_fix_source(source)
    assert (
        count,
        "sanitized mock reference" in fixed,
        fixed.startswith("# sentinel: allow[ZeroTrustSanitization] —"),
    ) == (1, True, True)


def test_auto_fix_source_clean_noop() -> None:
    """Clean source code produces zero fixes and returns unmodified string."""
    clean = 'SAFE = "https://example.com/data"\n'
    fixed, count = auto_fix_source(clean)
    assert (count, fixed) == (0, clean)


def test_auto_fix_file_and_targets(tmp_path: Path) -> None:
    """auto_fix_file and auto_fix_targets write remediated source to disk and resolve violations."""
    bad_ip = "10." + "0.0.5"
    py_file = tmp_path / "leaky.py"
    py_file.write_text(f'HOST = "{bad_ip}"\n', encoding="utf-8")

    initial_violations = audit_file(py_file)
    repaired_file_count = auto_fix_file(py_file)
    post_violations = audit_file(py_file)

    py_file2 = tmp_path / "leaky2.py"
    py_file2.write_text(f'HOST2 = "{bad_ip}"\n', encoding="utf-8")
    repaired_targets_count = auto_fix_targets([tmp_path])

    assert (
        len(initial_violations),
        repaired_file_count,
        len(post_violations),
        repaired_targets_count,
        SAFE_DOC_REPLACEMENT_IP in py_file.read_text(encoding="utf-8"),
        SAFE_DOC_REPLACEMENT_IP in py_file2.read_text(encoding="utf-8"),
    ) == (1, 1, 0, 1, True, True)


def test_lsp_framing_read_and_write() -> None:
    """LSP Content-Length header framing reads and writes JSON-RPC messages cleanly."""
    buf = io.BytesIO()
    payload = {"jsonrpc": "2.0", "id": 42, "method": "test"}
    _write_lsp_message(buf, payload)
    buf.seek(0)
    read_back = _read_lsp_message(buf)
    assert read_back == payload


def _collect_lsp_responses(out_buf: io.BytesIO) -> list[dict]:
    """Read all LSP message payloads from stream until EOF."""
    out_buf.seek(0)
    responses: list[dict] = []
    while (parsed := _read_lsp_message(out_buf)) is not None:
        responses.append(parsed)
    return responses


def _extract_response_fields(responses: list[dict]) -> tuple[list[str], list[int]]:
    """Extract present methods and response IDs from LSP responses."""
    methods = [r["method"] for r in responses if "method" in r]
    ids = [r["id"] for r in responses if "id" in r]
    return methods, ids


def test_lsp_server_lifecycle_and_diagnostics() -> None:
    """run_lsp_server completes full initialize/didOpen/codeAction/shutdown/exit lifecycle."""
    in_buf = io.BytesIO()
    out_buf = io.BytesIO()

    bad_ip = "172." + "16.0.1"
    doc_uri = "file:///workspace/test.py"
    doc_text = f'HOST = "{bad_ip}"\n'

    init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    open_notif = {
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": doc_uri, "text": doc_text}},
    }
    action_req = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "textDocument/codeAction",
        "params": {"textDocument": {"uri": doc_uri}},
    }
    shutdown_req = {"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {}}
    exit_notif = {"jsonrpc": "2.0", "method": "exit", "params": {}}

    for msg in (init_req, open_notif, action_req, shutdown_req, exit_notif):
        _write_lsp_message(in_buf, msg)

    in_buf.seek(0)
    exit_code = run_lsp_server(reader=in_buf, writer=out_buf)
    responses = _collect_lsp_responses(out_buf)
    methods, ids = _extract_response_fields(responses)

    assert (
        exit_code,
        ids,
        "textDocument/publishDiagnostics" in methods,
        len(responses),
    ) == (0, [1, 2, 3], True, 4)


def test_cli_run_options_with_fix_and_lsp() -> None:
    """CLI run options parser correctly extracts --fix and --lsp flags."""
    targets, _config, is_list, is_fix, is_lsp = parse_cli_run_options(
        [
            "sentinel.py",
            "some_path.py",
            "--fix",
            "--lsp",
        ]
    )
    assert (is_list, is_fix, is_lsp, str(targets[0])) == (False, True, True, "some_path.py")


def test_polyglot_audit_clean_go_and_rust() -> None:
    """audit_source validates clean Go and Rust code with zero violations."""
    go_code = (
        "package main\n\n"
        "func Add(a int, b int) int {\n"
        "    if a > 0 {\n"
        "        return a + b\n"
        "    }\n"
        "    return b\n"
        "}\n"
    )
    rs_code = (
        "pub fn compute(val: i32) -> i32 {\n"
        "    if val > 10 {\n"
        "        val * 2\n"
        "    } else {\n"
        "        val\n"
        "    }\n"
        "}\n"
    )
    v_go = audit_source(go_code, file_path="main.go")
    v_rs = audit_source(rs_code, file_path="lib.rs")

    assert (len(v_go), len(v_rs)) == (0, 0)


def test_polyglot_audit_flags_cyclomatic_complexity() -> None:
    """audit_source detects high cyclomatic complexity in polyglot functions."""
    branches = "\n".join(f"    if x == {i} {{ return {i} }}" for i in range(12))
    go_code = f"func Evaluate(x int) int {{\n{branches}\n    return -1\n}}\n"
    violations = audit_source(go_code, file_path="eval.go")

    assert (
        len(violations),
        violations[0].invariant,
        violations[0].metric_value,
        violations[0].threshold,
    ) == (1, "CyclomaticComplexity", 13, 10)


def test_polyglot_audit_flags_nesting_depth() -> None:
    """audit_source detects excessive brace nesting in TypeScript functions."""
    ts_code = (
        "function deep() {\n"
        "    if (true) {\n"
        "        if (true) {\n"
        "            if (true) {\n"
        "                if (true) {\n"
        "                    if (true) {\n"
        "                        if (true) {\n"
        "                            return 1;\n"
        "                        }\n"
        "                    }\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    violations = audit_source(ts_code, file_path="deep.ts")

    assert (
        len(violations),
        violations[0].invariant,
        violations[0].metric_value,
        violations[0].threshold,
    ) == (1, "NestingDepth", 6, 5)


def test_polyglot_audit_flags_ip_leakage_and_subdomain() -> None:
    """audit_source catches private IPs and example.com subdomains in polyglot files."""
    rs_code = 'pub const ADDR: &str = "192.168.1.50";\npub const URL: &str = "https://api.example.com/v1";\n'
    violations = audit_source(rs_code, file_path="config.rs")
    invariants = [v.invariant for v in violations]

    assert (
        len(violations),
        invariants,
    ) == (2, ["ZeroTrustSanitization", "ZeroTrustSanitization"])


def test_polyglot_waiver_in_header_suppresses_violation() -> None:
    """Header comments with sentinel waiver suppress ZeroTrustSanitization in polyglot files."""
    rs_code = (
        "// sentinel: allow[ZeroTrustSanitization] Test mock private address verification\n"
        'pub const TEST_IP: &str = "10.0.0.1";\n'
    )
    violations = audit_source(rs_code, file_path="network_test.rs")

    assert len(violations) == 0


def test_polyglot_targets_expansion_in_audit_targets(tmp_path: Path) -> None:
    """audit_targets discovers and audits polyglot files alongside Python files."""
    (tmp_path / "main.py").write_text("def ok(): return 1\n", encoding="utf-8")
    (tmp_path / "worker.go").write_text("func Work() int { return 1 }\n", encoding="utf-8")
    (tmp_path / "lib.rs").write_text("pub fn lib() -> i32 { 1 }\n", encoding="utf-8")

    report = audit_targets([tmp_path])

    assert (
        report.files_checked,
        report.py_files_checked,
        report.polyglot_files_checked,
        report.is_clean,
    ) == (3, 1, 2, True)
