"""Tests for kinetic probe execution and invariant leashing."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from kinetic_probe import (
    InvariantLeashRegistry,
    KineticProbeEngine,
    ProbeVerdict,
    main,
)


def test_kinetic_probe_confirms_exploit(tmp_path: Path) -> None:
    """A probe that exits with 0 confirms the vulnerability hypothesis."""
    engine = KineticProbeEngine(workspace_root=tmp_path)
    probe_code = "import sys; sys.exit(0)"
    result = engine.execute_probe(probe_code, claim_id="VIBES-CWE-22")

    assert (result.verdict, result.exit_code, result.claim_id) == (
        ProbeVerdict.EXPLOIT_CONFIRMED,
        0,
        "VIBES-CWE-22",
    )


def test_kinetic_probe_refutes_disproven_claim(tmp_path: Path) -> None:
    """A probe that handles the expected denial and exits with 1 refutes the hypothesis."""
    engine = KineticProbeEngine(workspace_root=tmp_path)
    probe_code = (
        "import sys\n"
        "try:\n"
        "    raise PermissionError('Denied by sandboxed security boundary')\n"
        "except PermissionError:\n"
        "    sys.exit(1)\n"
    )
    result = engine.execute_probe(probe_code, claim_id="VIBES-CWE-22-SAFE")

    assert (result.verdict, result.exit_code) == (
        ProbeVerdict.KINETICALLY_REFUTED,
        1,
    )


@pytest.mark.parametrize(
    ("probe_code", "expected_text"),
    [
        pytest.param("raise RuntimeError('boom')", "RuntimeError: boom", id="uncaught-exception"),
        pytest.param("data = bytearray(256 * 1024 * 1024)", "MemoryError", id="over-memory-limit"),
        pytest.param("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", "", id="killed-by-signal"),
        pytest.param("import sys; sys.exit(3)", "", id="unexpected-exit-status"),
    ],
)
def test_kinetic_probe_reports_broken_probe_as_inconclusive(
    tmp_path: Path, probe_code: str, expected_text: str
) -> None:
    """A probe that crashes, exceeds the memory limit or exits unexpectedly refutes nothing."""
    engine = KineticProbeEngine(workspace_root=tmp_path)
    result = engine.execute_probe(probe_code, claim_id="VIBES-BROKEN-PROBE")

    assert (result.verdict, expected_text in result.output) == ("inconclusive", True)


def test_kinetic_probe_timeout_kills_probe_process_group(tmp_path: Path) -> None:
    """A timeout kills the processes that the probe started, not only the probe."""
    heartbeat = tmp_path / "heartbeat"
    worker = (
        "import pathlib, sys, time\n"
        "end = time.monotonic() + 5.0\n"
        "while time.monotonic() < end:\n"
        "    with pathlib.Path(sys.argv[1]).open('a') as handle:\n"
        "        handle.write('.')\n"
        "    time.sleep(0.02)\n"
    )
    probe_code = (
        "import pathlib, subprocess, sys, time\n"
        f"heartbeat = pathlib.Path({str(heartbeat)!r})\n"
        f"subprocess.Popen([sys.executable, '-c', {worker!r}, str(heartbeat)])\n"
        "while not heartbeat.exists():\n"
        "    time.sleep(0.01)\n"
        "time.sleep(30)\n"
    )
    engine = KineticProbeEngine(workspace_root=tmp_path)
    result = engine.execute_probe(probe_code, timeout=1.0)

    time.sleep(0.1)
    beats_at_return = heartbeat.stat().st_size if heartbeat.exists() else 0
    time.sleep(0.3)
    beats_later = heartbeat.stat().st_size if heartbeat.exists() else 0

    assert (result.verdict, beats_at_return > 0, beats_later - beats_at_return) == (
        ProbeVerdict.RESOURCE_EXHAUSTION,
        True,
        0,
    )


def test_kinetic_probe_reports_start_failure_as_error(tmp_path: Path) -> None:
    """A probe that cannot start because the workspace is missing gives an error verdict."""
    engine = KineticProbeEngine(workspace_root=tmp_path / "missing")
    result = engine.execute_probe("import sys; sys.exit(0)")

    assert (result.verdict, result.exit_code, result.output.startswith("Execution error:")) == (
        ProbeVerdict.PROBE_ERROR,
        -2,
        True,
    )


def test_kinetic_probe_detects_resource_exhaustion(tmp_path: Path) -> None:
    """A probe that exceeds timeout is flagged as resource exhaustion (CWE-400)."""
    engine = KineticProbeEngine(workspace_root=tmp_path)
    probe_code = "import time; time.sleep(1.0)"
    result = engine.execute_probe(probe_code, timeout=0.1, claim_id="VIBES-CWE-400")

    assert (result.verdict, result.exit_code) == (
        ProbeVerdict.RESOURCE_EXHAUSTION,
        -1,
    )


def test_invariant_leash_lifecycle(tmp_path: Path) -> None:
    """Invariant leashes correctly detect perimeter invalidations across file changes."""
    registry = InvariantLeashRegistry()
    registry.register_leash("DEFECT-001", ["src/perimeter/auth.py", "src/gateway.py"])
    registry.register_leash("DEFECT-002", ["src/storage/fs.py"])

    # Delta modifying gateway should invalidate DEFECT-001 but not DEFECT-002
    invalidated = registry.get_invalidated_defects(["src/gateway.py"])
    assert invalidated == ["DEFECT-001"]

    # Serialization and deserialization roundtrip
    serialized = registry.to_dict()
    restored = InvariantLeashRegistry.from_dict(serialized)
    all_invalidated = restored.get_invalidated_defects(["src/gateway.py", "src/storage/fs.py"])
    assert set(all_invalidated) == {"DEFECT-001", "DEFECT-002"}


def test_kinetic_probe_cli_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI run subcommand executes probe and outputs structured JSON."""
    probe_file = tmp_path / "probe.py"
    probe_file.write_text("print('Exploit verified'); exit(0)", encoding="utf-8")

    exit_code = main(["run", str(probe_file), "--claim-id", "CLI-TEST-1"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert (exit_code, payload["verdict"], payload["claim_id"]) == (
        0,
        "confirmed",
        "CLI-TEST-1",
    )


def test_kinetic_probe_cli_leash(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI leash subcommand identifies invalidated defects from JSON registry."""
    registry_file = tmp_path / "leashes.json"
    registry_file.write_text(
        json.dumps({"SEC-101": ["src/middleware/jwt.py"]}), encoding="utf-8"
    )

    exit_code = main(
        ["leash", "--registry", str(registry_file), "--changed", "src/middleware/jwt.py"]
    )
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert (exit_code, data["invalidated_defects"]) == (2, ["SEC-101"])
