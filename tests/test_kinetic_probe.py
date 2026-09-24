"""Tests for kinetic probe execution and invariant leashing."""

from __future__ import annotations

import json
import sys
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
    """A probe that exits with non-zero indicates the vulnerability hypothesis was refuted."""
    engine = KineticProbeEngine(workspace_root=tmp_path)
    probe_code = "raise PermissionError('Denied by sandboxed security boundary')"
    result = engine.execute_probe(probe_code, claim_id="VIBES-CWE-22-SAFE")

    assert (result.verdict, result.exit_code) == (
        ProbeVerdict.KINETICALLY_REFUTED,
        1,
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
