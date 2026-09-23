"""Unit tests for AST Invariant Sentinel."""

# sentinel: allow[ZeroTrustSanitization] — negative fixtures asserting this sentinel detects private IPs and subdomains

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from sentinel import ComplexityVisitor, audit_file, audit_targets, main


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
    """Waivers must be declared in the module header where reviewers will see them."""
    padding = "\n".join(f"CONST_{n} = {n}" for n in range(20))
    module = _write_module(
        tmp_path,
        "buried.py",
        f'"""Doc."""\n{padding}\n# sentinel: allow[ZeroTrustSanitization] — buried far below the header\n'
        'HOST = "http://192.168.1.10"\n',
    )
    assert [v.invariant for v in audit_file(module)] == ["ZeroTrustSanitization"]


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
    proc = subprocess.run(
        [sys.executable, "-m", "radon", "cc", "-s", str(path)],
        capture_output=True, text=True, check=False, timeout=60,
    )
    return int(proc.stdout.strip().splitlines()[-1].split("(")[-1].rstrip(")"))


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
