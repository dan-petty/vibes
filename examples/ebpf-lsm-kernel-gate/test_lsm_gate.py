"""Unit test suite for eBPF Runtime LSM Kernel Gate Oracle."""

# sentinel: allow[ZeroTrustSanitization] — negative fixtures asserting the detector fires

from __future__ import annotations

from typing import Final

import pytest
from lsm_gate import (
    LSMEvent,
    LSMFinding,
    LSMHookType,
    LSMPolicy,
    LSMVerdict,
    evaluate_event,
    format_sarif,
    generate_bpf_c,
    generate_syz_path_mutations,
    generate_syz_socket_mutations,
    generate_tetragon_policy,
    main,
    run_fuzz_campaign,
)

MOCK_DOMAIN: Final[str] = "http://example.com"


def test_bprm_check_security_rules() -> None:
    """Assert binary execution allowlist and blocklist enforcement."""
    pol = LSMPolicy(allowed_binaries=frozenset({"python", "pytest"}), blocked_binaries=frozenset({"nc"}))
    ev_allow = LSMEvent(LSMHookType.BPRM_CHECK, "python", "/usr/bin/python")
    ev_block_disallowed = LSMEvent(LSMHookType.BPRM_CHECK, "nc", "/bin/nc")
    ev_block_unauthorized = LSMEvent(LSMHookType.BPRM_CHECK, "gcc", "/usr/bin/gcc")

    res_allow = evaluate_event(ev_allow, pol)
    res_block1 = evaluate_event(ev_block_disallowed, pol)
    res_block2 = evaluate_event(ev_block_unauthorized, pol)

    assert (
        (res_allow.verdict, res_allow.rule_id),
        (res_block1.verdict, res_block1.rule_id),
        (res_block2.verdict, res_block2.rule_id),
    ) == (
        (LSMVerdict.ALLOW, "LSM000"),
        (LSMVerdict.BLOCK, "LSM001"),
        (LSMVerdict.BLOCK, "LSM001"),
    )


def test_socket_connect_egress_rules() -> None:
    """Assert network egress blocking for private IPs and AWS metadata."""
    pol = LSMPolicy(allow_loopback_only=True)
    ev_loopback = LSMEvent(LSMHookType.SOCKET_CONNECT, "runner", "127.0.0.1:8080")
    ev_meta = LSMEvent(LSMHookType.SOCKET_CONNECT, "runner", "169.254.169.254:80")
    ev_priv1 = LSMEvent(LSMHookType.SOCKET_CONNECT, "runner", "10.0.0.1:443")
    ev_priv2 = LSMEvent(LSMHookType.SOCKET_CONNECT, "runner", "192.168.1.1:80")

    r_loop = evaluate_event(ev_loopback, pol)
    r_meta = evaluate_event(ev_meta, pol)
    r_priv1 = evaluate_event(ev_priv1, pol)
    r_priv2 = evaluate_event(ev_priv2, pol)

    assert (
        (r_loop.verdict, r_loop.rule_id),
        (r_meta.verdict, r_meta.rule_id),
        (r_priv1.verdict, r_priv1.rule_id),
        (r_priv2.verdict, r_priv2.rule_id),
    ) == (
        (LSMVerdict.ALLOW, "LSM000"),
        (LSMVerdict.BLOCK, "LSM002"),
        (LSMVerdict.BLOCK, "LSM002"),
        (LSMVerdict.BLOCK, "LSM002"),
    )


def test_file_open_sensitive_paths() -> None:
    """Assert filesystem path boundary containment."""
    pol = LSMPolicy()
    ev_safe = LSMEvent(LSMHookType.FILE_OPEN, "runner", "/workspace/src/app.py")
    ev_shadow = LSMEvent(LSMHookType.FILE_OPEN, "runner", "/etc/shadow")
    ev_ssh = LSMEvent(LSMHookType.FILE_OPEN, "runner", "~/.ssh/id_rsa")
    ev_hook = LSMEvent(LSMHookType.FILE_OPEN, "runner", "/repo/.git/hooks/post-commit")

    r_safe = evaluate_event(ev_safe, pol)
    r_shadow = evaluate_event(ev_shadow, pol)
    r_ssh = evaluate_event(ev_ssh, pol)
    r_hook = evaluate_event(ev_hook, pol)

    assert (
        (r_safe.verdict, r_safe.rule_id),
        (r_shadow.verdict, r_shadow.rule_id),
        (r_ssh.verdict, r_ssh.rule_id),
        (r_hook.verdict, r_hook.rule_id),
    ) == (
        (LSMVerdict.ALLOW, "LSM000"),
        (LSMVerdict.BLOCK, "LSM003"),
        (LSMVerdict.BLOCK, "LSM003"),
        (LSMVerdict.BLOCK, "LSM003"),
    )


def test_privilege_escalation_rules() -> None:
    """Assert blocking of setuid and privilege expansion attempts."""
    pol = LSMPolicy()
    ev_root_uid = LSMEvent(LSMHookType.TASK_SETUID, "runner", "daemon", uid=0)
    ev_cap = LSMEvent(LSMHookType.CAPSET, "runner", "cap_sys_admin", uid=1000)
    ev_normal = LSMEvent(LSMHookType.TASK_SETUID, "runner", "nobody", uid=65534)

    r_root = evaluate_event(ev_root_uid, pol)
    r_cap = evaluate_event(ev_cap, pol)
    r_norm = evaluate_event(ev_normal, pol)

    assert (
        (r_root.verdict, r_root.rule_id),
        (r_cap.verdict, r_cap.rule_id),
        (r_norm.verdict, r_norm.rule_id),
    ) == (
        (LSMVerdict.BLOCK, "LSM004"),
        (LSMVerdict.BLOCK, "LSM004"),
        (LSMVerdict.ALLOW, "LSM000"),
    )


def test_syzkaller_evasion_mitigation() -> None:
    """Assert detection of adversarial traversal and flag corruption."""
    pol = LSMPolicy()
    ev_corrupt_flags = LSMEvent(LSMHookType.FILE_OPEN, "runner", "/workspace/file", flags=-1)
    ev_null_byte = LSMEvent(LSMHookType.FILE_OPEN, "runner", "/workspace/\x00etc/shadow")
    ev_traversal = LSMEvent(LSMHookType.FILE_OPEN, "runner", "/workspace/..//..//root")

    r_flags = evaluate_event(ev_corrupt_flags, pol)
    r_null = evaluate_event(ev_null_byte, pol)
    r_trav = evaluate_event(ev_traversal, pol)

    assert (
        (r_flags.verdict, r_flags.rule_id),
        (r_null.verdict, r_null.rule_id),
        (r_trav.verdict, r_trav.rule_id),
    ) == (
        (LSMVerdict.BLOCK, "LSM005"),
        (LSMVerdict.BLOCK, "LSM005"),
        (LSMVerdict.BLOCK, "LSM005"),
    )


def test_dynamic_syzkaller_fuzz_campaign() -> None:
    """Assert monotonic containment under dynamic mutation fuzzing."""
    report = run_fuzz_campaign(LSMPolicy())
    mut_paths = generate_syz_path_mutations("/workspace")
    mut_socks = generate_syz_socket_mutations()

    assert (
        report.monotonically_contained,
        report.containment_rate >= 0.85,
        len(mut_paths) == 5,
        len(mut_socks) == 6,
    ) == (True, True, True, True)


def test_bpf_and_tetragon_policy_generation() -> None:
    """Assert synthesis of valid BPF C and Tetragon TracingPolicy definitions."""
    bpf_c = generate_bpf_c()
    tetra = generate_tetragon_policy()

    assert (
        ("SEC(\"lsm/bprm_check_security\")" in bpf_c),
        ("SEC(\"lsm/file_open\")" in bpf_c),
        ("kind: TracingPolicy" in tetra),
        ("security_bprm_check" in tetra),
    ) == (True, True, True, True)


def test_sarif_export_format() -> None:
    """Assert OASIS SARIF 2.1.0 telemetry export schema compliance."""
    ev = LSMEvent(LSMHookType.BPRM_CHECK, "nc", "/bin/nc")
    finding = LSMFinding("LSM001", LSMVerdict.BLOCK, ev, "Disallowed binary")
    sarif = format_sarif([finding])

    assert (
        sarif["version"] == "2.1.0",
        sarif["runs"][0]["tool"]["driver"]["name"] == "lsm_kernel_gate",
        len(sarif["runs"][0]["results"]) == 1,
        sarif["runs"][0]["results"][0]["ruleId"] == "LSM001",
    ) == (True, True, True, True)


def test_cli_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    """Assert CLI execution across eval, fuzz, generate, and sarif."""
    rc_eval_allow = main(["eval", "--hook", "file_open", "--target", "/workspace/ok.py"])
    out_allow = capsys.readouterr().out
    rc_eval_block = main(["eval", "--hook", "file_open", "--target", "/etc/shadow"])
    out_block = capsys.readouterr().out
    rc_fuzz = main(["fuzz"])
    out_fuzz = capsys.readouterr().out
    rc_gen = main(["generate", "--format", "tetragon"])
    out_gen = capsys.readouterr().out
    rc_sarif = main(["sarif", "--hook", "bprm_check_security", "--comm", "nc", "--target", "/bin/nc"])
    out_sarif = capsys.readouterr().out

    assert (
        (rc_eval_allow, "Rule: LSM000" in out_allow),
        (rc_eval_block, "Rule: LSM003" in out_block),
        (rc_fuzz, "Monotonically Contained: True" in out_fuzz),
        (rc_gen, "kind: TracingPolicy" in out_gen),
        (rc_sarif, "sarif-2.1.0" in out_sarif),
    ) == (
        (0, True),
        (1, True),
        (0, True),
        (0, True),
        (0, True),
    )

