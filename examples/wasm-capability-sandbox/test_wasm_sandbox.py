"""Unit and integration test suite for WASI 0.2 Capability-Based Sandbox Gateway.

Tests object-capability isolation, WIT interface validation, gas-metered execution,
linear memory ceiling containment, SARIF 2.1.0 export, and CLI commands.

Certified compliant with AST Invariant Sentinel (M <= 4, depth <= 2).
"""

from __future__ import annotations

from pathlib import Path

from wasm_sandbox import (
    WASM_MAGIC,
    WASM_VERSION,
    CapabilityKind,
    DiagnosticRule,
    SandboxEnvironment,
    TrapKind,
    check_filesystem_access,
    check_network_dial,
    create_capability,
    export_sarif,
    format_markdown_report,
    grow_memory,
    main,
    parse_wit_contract,
    run_metered_workload,
    validate_wasm_header,
    verify_wit_contract,
)


def _build_valid_wasm_bytes(sections: bytes = b"") -> bytes:
    """Construct minimal valid Wasm binary payload."""
    return WASM_MAGIC + WASM_VERSION + sections


def test_validate_wasm_header_valid() -> None:
    """Validate that well-formed Wasm binaries are correctly accepted."""
    data = _build_valid_wasm_bytes(b"\x01\x04\x01\x60\x00\x00")
    info = validate_wasm_header(data)
    assert (info.is_valid, info.version, info.sections) == (True, 1, (1,))


def test_validate_wasm_header_invalid() -> None:
    """Validate that corrupt or truncated headers are rejected."""
    bad_magic = validate_wasm_header(b"NOT_WASM_MAGIC")
    short_data = validate_wasm_header(b"\x00as")
    assert (bad_magic.is_valid, short_data.is_valid) == (False, False)


def test_parse_wit_contract() -> None:
    """Verify parsing of declarative WIT interface definitions."""
    wit_src = (
        "package example:runner;\n"
        "interface tool-exec {\n"
        "    import wasi:filesystem/preopens@0.2.0;\n"
        "    import wasi:clocks/monotonic-clock@0.2.0;\n"
        "    export run: func(input: string) -> string;\n"
        "}\n"
    )
    contract = parse_wit_contract(wit_src)
    assert (
        contract.package,
        contract.interface_name,
        len(contract.imports),
        len(contract.exports),
    ) == ("example:runner", "tool-exec", 2, 1)


def test_verify_wit_contract_mismatch() -> None:
    """Verify that ungranted WIT imports produce CAP005 diagnostic findings."""
    wit_src = "package example:test;\ninterface demo {\nimport wasi:net@0.2.0;\n}\n"
    contract = parse_wit_contract(wit_src)
    findings = verify_wit_contract(contract, granted=())
    assert (len(findings), findings[0].rule) == (1, DiagnosticRule.CAP005)


def test_filesystem_capability_boundaries(tmp_path: Path) -> None:
    """Verify scoped capability authorization and ambient filesystem denial (CAP001)."""
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    allowed_file = sandbox_dir / "data.txt"
    outside_file = tmp_path / "secret.env"
    cap = create_capability(CapabilityKind.FILESYSTEM_READ, str(sandbox_dir))
    allowed_res = check_filesystem_access(str(allowed_file), (cap,))
    denied_res = check_filesystem_access(str(outside_file), (cap,))
    assert (allowed_res is None, denied_res is not None, denied_res.rule if denied_res else None) == (
        True,
        True,
        DiagnosticRule.CAP001,
    )


def test_network_dial_capability(tmp_path: Path) -> None:
    """Verify network egress capability scoping and ambient denial (CAP002)."""
    cap_exact = create_capability(CapabilityKind.NETWORK_DIAL, "example.com:443")
    ok_res = check_network_dial("example.com:443", (cap_exact,))
    blocked_res = check_network_dial("example.com:8080", (cap_exact,))
    assert (ok_res is None, blocked_res is not None, blocked_res.rule if blocked_res else None) == (
        True,
        True,
        DiagnosticRule.CAP002,
    )


def test_gas_metering_termination() -> None:
    """Verify instruction gas exhaustion triggers CAP003 trap."""
    env = SandboxEnvironment(gas_budget=50)
    instructions = [("op_add", 10), ("op_mul", 20), ("op_loop", 30)]
    result = run_metered_workload(env, instructions)
    assert (
        result.trap,
        result.gas_consumed,
        result.gas_remaining,
        len(result.findings),
        result.findings[0].rule if result.findings else None,
    ) == (TrapKind.GAS_EXHAUSTED, 50, 0, 1, DiagnosticRule.CAP003)


def test_linear_memory_ceiling_trap() -> None:
    """Verify linear memory allocation ceiling enforcement (CAP004)."""
    env = SandboxEnvironment(max_pages=4, allocated_pages=2)
    ok_trap = grow_memory(env, 2)
    bad_trap = grow_memory(env, 1)
    assert (ok_trap, bad_trap, env.allocated_pages) == (
        TrapKind.NONE,
        TrapKind.MEMORY_CEILING_BREACH,
        4,
    )


def test_export_sarif_and_markdown() -> None:
    """Verify SARIF 2.1.0 and Markdown reporting formatting."""
    env = SandboxEnvironment(gas_budget=10)
    result = run_metered_workload(env, [("op_heavy", 20)])
    sarif_str = export_sarif(result.findings)
    md_str = format_markdown_report(result)
    assert (
        "sarif-2.1.0.json" in sarif_str,
        "CAP003" in sarif_str,
        "Trap Status" in md_str,
        "CAP003" in md_str,
    ) == (True, True, True, True)


def test_cli_subcommands(tmp_path: Path) -> None:
    """Verify CLI argument parsing and handler execution."""
    wasm_file = tmp_path / "test.wasm"
    wasm_file.write_bytes(_build_valid_wasm_bytes())
    rc_help = main([])
    rc_val = main(["validate", str(wasm_file)])
    rc_aud = main(
        [
            "audit",
            "--grant-fs",
            str(tmp_path),
            "--check-path",
            str(tmp_path / "file.txt"),
        ]
    )
    assert (rc_help, rc_val, rc_aud) == (0, 0, 0)
