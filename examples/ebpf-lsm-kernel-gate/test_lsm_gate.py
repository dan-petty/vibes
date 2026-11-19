"""Unit test suite for eBPF Runtime LSM Kernel Gate Oracle."""

# sentinel: allow[ZeroTrustSanitization] — negative fixtures asserting the detector fires

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from lsm_gate import (
    LSMEvent,
    LSMFinding,
    LSMHookType,
    LSMPolicy,
    LSMVerdict,
    evaluate_event,
    format_sarif,
    generate_syz_path_mutations,
    generate_syz_socket_mutations,
    generate_tetragon_policy,
    main,
    resolve_blocked_paths,
    run_fuzz_campaign,
)

MOCK_DOMAIN: Final[str] = "http://example.com"

# Each spelling reaches a blocked file once the kernel resolves `.`, `..` and repeated slashes.
NORMALIZATION_VARIANTS: Final[tuple[tuple[str, str], ...]] = (
    ("/etc/./shadow", "LSM003"),
    ("/etc//shadow", "LSM003"),
    ("/etc/../etc/shadow", "LSM005"),
    ("//etc/shadow", "LSM003"),
    ("/repo/.git/./hooks/pre-commit", "LSM003"),
)

# Default-policy verdicts at and next to an absolute or home-relative blocked entry.
# A sibling that only starts with the same characters, such as `/rootfs`, is not protected.
COMPONENT_CASES: Final[dict[str, LSMVerdict]] = {
    "/root": LSMVerdict.BLOCK,
    "/root/x": LSMVerdict.BLOCK,
    "/rootfs/x": LSMVerdict.ALLOW,
    "/rootkit": LSMVerdict.ALLOW,
    "/etc/shadow": LSMVerdict.BLOCK,
    "/etc/shadow-": LSMVerdict.BLOCK,
    "/etc/shadowed": LSMVerdict.ALLOW,
    "/home/user/.ssh/id_ed25519": LSMVerdict.BLOCK,
    "/home/user/.sshkeys/id_ed25519": LSMVerdict.ALLOW,
    "/home/user/.aws/config": LSMVerdict.BLOCK,
    "/root/.aws/credentials": LSMVerdict.BLOCK,
}

# How Tetragon applies the string operators that the file selectors use.
TETRAGON_STRING_OPS: Final[dict[str, Any]] = {"Equal": str.__eq__, "Prefix": str.startswith}


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
    ev_ssh = LSMEvent(LSMHookType.FILE_OPEN, "runner", "/home/user/.ssh/id_rsa")
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
    """Assert containment over the policy-derived probe list (no random fuzzing)."""
    report = run_fuzz_campaign(LSMPolicy())
    mut_paths = generate_syz_path_mutations()
    mut_socks = generate_syz_socket_mutations()

    assert (
        report.monotonically_contained,
        (report.total_probes, report.blocked_probes, report.allowed_probes),
        len(mut_paths),
        len(mut_socks),
    ) == (True, (35, 34, 1), 25, 6)


def test_file_open_normalizes_path_variants() -> None:
    """Assert that dot, double-slash and parent forms of a blocked path are blocked, and safe paths pass."""
    results = [evaluate_event(LSMEvent(LSMHookType.FILE_OPEN, "python3", p)) for p, _ in NORMALIZATION_VARIANTS]
    safe = evaluate_event(LSMEvent(LSMHookType.FILE_OPEN, "python3", "/workspace/./src//app.py"))

    assert (
        [(r.verdict, r.rule_id) for r in results],
        safe.verdict,
    ) == (
        [(LSMVerdict.BLOCK, rule) for _, rule in NORMALIZATION_VARIANTS],
        LSMVerdict.ALLOW,
    )


def test_fuzz_path_probes_follow_policy() -> None:
    """Assert that the path probes come from the policy and that the gate blocks every one."""
    paths = generate_syz_path_mutations()
    verdicts = {evaluate_event(LSMEvent(LSMHookType.FILE_OPEN, "runner", p)).verdict for p in paths}
    custom = generate_syz_path_mutations(LSMPolicy(blocked_paths=("/srv/secret",)), base="/w")

    assert (
        {"/etc/./shadow", "/etc//shadow", "/etc/../etc/shadow", "/./root", "//root"} <= set(paths),
        verdicts,
        custom[:4],
    ) == (
        True,
        {LSMVerdict.BLOCK},
        ["/srv/secret", "/srv/./secret", "/srv//secret", "/srv/../srv/secret"],
    )


def _kprobes(policy: LSMPolicy | None = None) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Parse the generated TracingPolicy and index its kprobes by kernel function."""
    doc = yaml.safe_load(generate_tetragon_policy(policy))
    return doc, {k["call"]: k for k in doc["spec"]["kprobes"]}


def _selector_summary(kprobe: dict[str, Any]) -> list[tuple[list[tuple[int, str, list[str]]], list[str]]]:
    """Reduce each selector of a kprobe to its argument matches and its actions."""
    return [
        (
            [(m["index"], m["operator"], m["values"]) for m in sel.get("matchArgs", [])],
            [a["action"] for a in sel.get("matchActions", [])],
        )
        for sel in kprobe.get("selectors", [])
    ]


def _verdicts(paths: Any, policy: LSMPolicy | None = None) -> dict[str, LSMVerdict]:
    """Evaluate a file open of each path and map the path to its verdict."""
    return {p: evaluate_event(LSMEvent(LSMHookType.FILE_OPEN, "runner", p), policy).verdict for p in paths}


def test_file_open_matches_whole_path_components() -> None:
    """Assert that an entry blocks the path itself and paths below it, never a sibling with the same first characters."""
    relative = _verdicts(["/repo/.git/hooks/pre-commit", "/repo/x.git/hooks/pre-commit", "/repo/.git/hooksx"])

    assert (_verdicts(COMPONENT_CASES), relative) == (
        COMPONENT_CASES,
        {
            "/repo/.git/hooks/pre-commit": LSMVerdict.BLOCK,
            "/repo/x.git/hooks/pre-commit": LSMVerdict.ALLOW,
            "/repo/.git/hooksx": LSMVerdict.ALLOW,
        },
    )


def test_file_open_expands_home_relative_entries() -> None:
    """Assert that `~` entries become absolute paths under each configured home directory."""
    pol = LSMPolicy(blocked_paths=("~/.ssh", "~"), home_dirs=("/home/agent", "/srv/home/"))
    _, kprobes = _kprobes(LSMPolicy(blocked_paths=("~/.aws",), home_dirs=("/home/agent",)))

    assert (
        resolve_blocked_paths(LSMPolicy()),
        resolve_blocked_paths(pol),
        _verdicts(["/home/agent/.ssh/id_ed25519", "/srv/home/notes", "/home/user/.ssh/id_ed25519"], pol),
        _selector_summary(kprobes["security_file_open"]),
    ) == (
        ("/etc/shadow", "/etc/shadow-", "/etc/sudoers", "/root", "/home/user/.ssh", "/home/user/.aws", ".git/hooks", "/proc/kcore"),
        ("/home/agent", "/srv/home"),
        {
            "/home/agent/.ssh/id_ed25519": LSMVerdict.BLOCK,
            "/srv/home/notes": LSMVerdict.BLOCK,
            "/home/user/.ssh/id_ed25519": LSMVerdict.ALLOW,
        },
        [
            ([(0, "Equal", ["/home/agent/.aws"])], ["Sigkill"]),
            ([(0, "Prefix", ["/home/agent/.aws/"])], ["Sigkill"]),
        ],
    )


def _kernel_kills(kprobe: dict[str, Any], path: str) -> bool:
    """Apply the selectors of a file kprobe to a path the way Tetragon does: OR over selectors, AND over matches."""
    return any(
        all(any(TETRAGON_STRING_OPS[m["operator"]](path, v) for v in m["values"]) for m in sel["matchArgs"])
        for sel in kprobe["selectors"]
    )


def test_tetragon_file_selectors_match_whole_components() -> None:
    """Assert that the file selectors kill where the simulation blocks, and hold no `~` and no bare prefix."""
    text = generate_tetragon_policy()
    _, kprobes = _kprobes()
    file_open = kprobes["security_file_open"]
    prefixes = [v for sel in file_open["selectors"] for m in sel["matchArgs"] if m["operator"] == "Prefix" for v in m["values"]]

    assert (
        "~" in text,
        [v for v in prefixes if not v.endswith("/")],
        {p: _kernel_kills(file_open, p) for p in COMPONENT_CASES},
    ) == (
        False,
        [],
        {p: v == LSMVerdict.BLOCK for p, v in COMPONENT_CASES.items()},
    )


def test_tetragon_policy_enforces_with_sigkill() -> None:
    """Assert that each generated kprobe kills a caller that matches the default policy."""
    doc, kprobes = _kprobes()
    binaries = ["/chattr", "/nc", "/ncat", "/netcat", "/socat", "/tcpdump", "/telnet"]
    # `/root/.ssh` and `/root/.aws` lie below `/root`, so the generator emits only `/root`.
    file_paths = ["/etc/shadow", "/etc/shadow-", "/etc/sudoers", "/root", "/home/user/.ssh", "/home/user/.aws", "/proc/kcore"]

    assert (
        doc["kind"],
        doc["metadata"]["annotations"],
        doc["spec"]["podSelector"],
        {call: [(a["index"], a["type"]) for a in k["args"]] for call, k in kprobes.items()},
        {call: _selector_summary(k) for call, k in kprobes.items()},
    ) == (
        "TracingPolicy",
        {"agent-lsm-gate/mode": "enforce"},
        {"matchLabels": {"app.kubernetes.io/name": "agent-sandbox"}},
        {
            "security_bprm_check": [(0, "linux_binprm")],
            "security_file_open": [(0, "file")],
            "security_socket_connect": [(0, "socket"), (1, "sockaddr")],
        },
        {
            "security_bprm_check": [([(0, "Postfix", binaries)], ["Sigkill"])],
            "security_file_open": [
                ([(0, "Equal", file_paths)], ["Sigkill"]),
                ([(0, "Prefix", [f"{p}/" for p in file_paths])], ["Sigkill"]),
            ],
            "security_socket_connect": [
                ([(1, "Family", ["AF_INET", "AF_INET6"]), (1, "NotSAddr", ["127.0.0.0/8", "::1/128"])], ["Sigkill"])
            ],
        },
    )


def test_tetragon_policy_follows_custom_policy() -> None:
    """Assert that the generator reads the policy and drops a kprobe that has nothing to match."""
    pol = LSMPolicy(blocked_binaries=frozenset({"curl"}), blocked_paths=(".git/hooks",), allow_loopback_only=False)
    _, kprobes = _kprobes(pol)

    assert {call: _selector_summary(k) for call, k in kprobes.items()} == {
        "security_bprm_check": [([(0, "Postfix", ["/curl"])], ["Sigkill"])],
        "security_socket_connect": [
            ([(1, "Family", ["AF_INET", "AF_INET6"]), (1, "SAddr", ["169.254.0.0/16"])], ["Sigkill"])
        ],
    }


def test_tetragon_policy_follows_pod_label(capsys: pytest.CaptureFixture[str]) -> None:
    """Assert that the policy field and `generate --pod-label` replace the default selector."""
    doc, _ = _kprobes(LSMPolicy(tetragon_pod_label=("app", "other")))
    rc = main(["generate", "--pod-label", "app=other"])
    cli_doc = yaml.safe_load(capsys.readouterr().out)

    assert (doc["spec"]["podSelector"], rc, cli_doc["spec"]["podSelector"]) == (
        {"matchLabels": {"app": "other"}},
        0,
        {"matchLabels": {"app": "other"}},
    )


def test_generate_rejects_a_pod_label_without_equals(capsys: pytest.CaptureFixture[str]) -> None:
    """Assert that `generate --pod-label` refuses a value that has no `=`, or no key."""
    codes = []
    for value in ("agent-sandbox", "=agent-sandbox"):
        with pytest.raises(SystemExit) as exc:
            main(["generate", "--pod-label", value])
        codes.append(exc.value.code)
    err = capsys.readouterr().err

    assert (codes, "--pod-label" in err, "not a key=value label: agent-sandbox" in err) == ([2, 2], True, True)


def test_generate_ships_no_bpf_c(capsys: pytest.CaptureFixture[str]) -> None:
    """Assert that generate refuses the removed bpf format, and that the README states the mode."""
    with pytest.raises(SystemExit) as exc:
        main(["generate", "--format", "bpf"])
    capsys.readouterr()
    rc = main(["generate"])
    out = capsys.readouterr().out
    readme = (Path(__file__).parent / "README.md").read_text(encoding="utf-8")

    assert (
        (exc.value.code, rc, "kind: TracingPolicy" in out, 'SEC("lsm/' in out),
        ("agent-lsm-gate/mode: enforce" in readme, "ships no BPF C" in readme),
    ) == ((2, 0, True, False), (True, True))


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

