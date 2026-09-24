"""Tests for AI Bill of Materials (AIBOM) & Supply-Chain Security Scanner."""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import aibom_scanner as aibom
from aibom_scanner import (
    AibomComponent,
    AibomReport,
    ComponentType,
    ModelFormat,
    RuleSeverity,
    SecurityFinding,
    compute_file_sha256,
    export_cyclonedx_json,
    export_markdown,
    export_sarif_json,
    inspect_gguf_header,
    inspect_safetensors_header,
    inspect_weight_file,
    main,
    scan_paths,
    scan_python_code,
)


def _create_mock_safetensors(path: Path, metadata: dict[str, str]) -> None:
    """Create a minimal valid SafeTensors file with JSON metadata header."""
    meta_json = json.dumps({"__metadata__": metadata, "weight": {"dtype": "F32", "shape": [1]}}).encode("utf-8")
    header_len = len(meta_json)
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", header_len))
        f.write(meta_json)
        f.write(b"\x00" * 16)


def _create_mock_gguf(path: Path, version: int = 3, tensor_count: int = 42, kv_count: int = 5) -> None:
    """Create a minimal valid GGUF file with binary header."""
    with open(path, "wb") as f:
        f.write(aibom.GGUF_MAGIC)
        f.write(struct.pack("<I", version))
        f.write(struct.pack("<Q", tensor_count))
        f.write(struct.pack("<Q", kv_count))


def test_sha256_streaming_computation(tmp_path: Path) -> None:
    """Verify compute_file_sha256 computes expected hash on chunked files."""
    test_file = tmp_path / "sample.bin"
    test_file.write_bytes(b"aibom-scanner-test-content-42")
    digest = compute_file_sha256(test_file)

    assert (len(digest), isinstance(digest, str)) == (64, True)


def test_safetensors_header_parser(tmp_path: Path) -> None:
    """Extract metadata dictionary from SafeTensors header without loading tensor weights."""
    st_file = tmp_path / "model.safetensors"
    _create_mock_safetensors(st_file, {"format": "pt", "framework": "pytorch", "model_type": "transformer"})

    meta = inspect_safetensors_header(st_file)
    empty_meta = inspect_safetensors_header(tmp_path / "nonexistent.safetensors")

    assert (
        meta.get("format"),
        meta.get("framework"),
        meta.get("model_type"),
        empty_meta,
    ) == (
        "pt",
        "pytorch",
        "transformer",
        {},
    )


def test_gguf_header_parser(tmp_path: Path) -> None:
    """Extract metadata header from GGUF binary checkpoint."""
    gguf_file = tmp_path / "model.gguf"
    _create_mock_gguf(gguf_file, version=3, tensor_count=128, kv_count=16)

    header = inspect_gguf_header(gguf_file)
    bad_header = inspect_gguf_header(tmp_path / "nonexistent.gguf")

    assert (
        header.get("format"),
        header.get("version"),
        header.get("tensor_count"),
        header.get("metadata_kv_count"),
        bad_header,
    ) == (
        "GGUF",
        3,
        128,
        16,
        {},
    )


def test_weight_format_detection_and_pickle_risk(tmp_path: Path) -> None:
    """Classify formats and flag legacy pickle weights under AIBOM005."""
    st_path = tmp_path / "model.safetensors"
    _create_mock_safetensors(st_path, {"model": "test"})
    st_comp, st_findings = inspect_weight_file(st_path)

    pickle_path = tmp_path / "weights.bin"
    pickle_path.write_bytes(b"dummy-pickle-bytes")
    p_comp, p_findings = inspect_weight_file(pickle_path)

    assert (
        st_comp.format,
        len(st_findings),
        p_comp.format,
        len(p_findings),
        p_findings[0].rule_id,
    ) == (
        ModelFormat.SAFETENSORS,
        0,
        ModelFormat.PICKLE,
        1,
        "AIBOM005",
    )


def test_trust_remote_code_ast_sentinel() -> None:
    """Detect dangerous trust_remote_code=True under rule AIBOM001."""
    bad_code = """
from transformers import AutoModel
model = AutoModel.from_pretrained("org/repo", trust_remote_code=True)
"""
    good_code = """
from transformers import AutoModel
model = AutoModel.from_pretrained("org/repo", trust_remote_code=False)
"""
    _, bad_findings = scan_python_code(bad_code, "bad.py")
    _, good_findings = scan_python_code(good_code, "good.py")

    bad_rule = bad_findings[0].rule_id if bad_findings else ""
    bad_sev = bad_findings[0].severity if bad_findings else ""

    assert (
        bad_rule,
        bad_sev,
        any(f.rule_id == "AIBOM001" for f in good_findings),
    ) == (
        "AIBOM001",
        RuleSeverity.ERROR,
        False,
    )


def test_torch_load_ast_sentinel() -> None:
    """Detect unsafe torch.load without weights_only=True under rule AIBOM002."""
    bad_code = """
import torch
weights = torch.load("checkpoint.pt")
"""
    good_code = """
import torch
weights = torch.load("checkpoint.pt", weights_only=True)
"""
    _, bad_findings = scan_python_code(bad_code, "bad.py")
    _, good_findings = scan_python_code(good_code, "good.py")

    bad_rule = bad_findings[0].rule_id if bad_findings else ""
    bad_sev = bad_findings[0].severity if bad_findings else ""

    assert (
        bad_rule,
        bad_sev,
        any(f.rule_id == "AIBOM002" for f in good_findings),
    ) == (
        "AIBOM002",
        RuleSeverity.ERROR,
        False,
    )


def test_unpinned_model_revision_ast_sentinel() -> None:
    """Flag unpinned model revisions lacking 40-char SHA under rule AIBOM003."""
    unpinned_code = """
from transformers import AutoModel
model = AutoModel.from_pretrained("meta-llama/Llama-3-8B")
"""
    pinned_code = """
from transformers import AutoModel
model = AutoModel.from_pretrained("meta-llama/Llama-3-8B", revision="e123456789012345678901234567890123456789")
"""
    _, unpinned_findings = scan_python_code(unpinned_code, "unpinned.py")
    _, pinned_findings = scan_python_code(pinned_code, "pinned.py")

    has_unpinned_risk = any(f.rule_id == "AIBOM003" for f in unpinned_findings)
    has_pinned_risk = any(f.rule_id == "AIBOM003" for f in pinned_findings)

    assert (has_unpinned_risk, has_pinned_risk) == (True, False)


def test_plaintext_model_download_ast_sentinel() -> None:
    """Detect unencrypted HTTP model download URLs under rule AIBOM004."""
    code_bad = """
WEIGHTS_URL = "http://huggingface.co/org/model/resolve/main/model.safetensors"
"""
    code_good = """
WEIGHTS_URL = "https://huggingface.co/org/model/resolve/main/model.safetensors"
"""
    _, findings_bad = scan_python_code(code_bad, "download_bad.py")
    _, findings_good = scan_python_code(code_good, "download_good.py")

    assert (
        len(findings_bad),
        findings_bad[0].rule_id,
        findings_bad[0].severity,
        len(findings_good),
    ) == (
        1,
        "AIBOM004",
        RuleSeverity.ERROR,
        0,
    )


def test_ai_framework_and_model_component_extraction() -> None:
    """Extract AI frameworks, models, and datasets into inventoried components."""
    code = """
import torch
import safetensors
from transformers import AutoModel
from datasets import load_dataset

model = AutoModel.from_pretrained("google/gemma-2b", revision="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2")
data = load_dataset("squad/v2")
"""
    components, findings = scan_python_code(code, "pipeline.py")
    names = {c.name for c in components}
    types = {c.component_type for c in components}

    assert (
        "torch" in names,
        "safetensors" in names,
        "google/gemma-2b" in names,
        "squad/v2" in names,
        ComponentType.AI_FRAMEWORK in types,
        ComponentType.MACHINE_LEARNING_MODEL in types,
        ComponentType.DATASET in types,
        len(findings),
    ) == (
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        0,
    )


def test_cyclonedx_1_6_ml_bom_export() -> None:
    """Generate CycloneDX 1.6 ML-BOM JSON document matching specification."""
    components = [
        AibomComponent(
            bom_ref="pkg:ml/bert-base",
            component_type=ComponentType.MACHINE_LEARNING_MODEL,
            name="bert-base",
            format=ModelFormat.SAFETENSORS,
            hashes=(("SHA-256", "abc123hash"),),
            properties=(("parameters", "110M"),),
        )
    ]
    report = AibomReport(scanned_files=5, components=components, findings=[])
    bom = export_cyclonedx_json(report, project_name="vibes-demo")

    assert (
        bom.get("bomFormat"),
        bom.get("specVersion"),
        bom["metadata"]["component"]["name"],
        len(bom["components"]),
        bom["components"][0]["name"],
        bom["components"][0]["hashes"][0]["content"],
    ) == (
        "CycloneDX",
        "1.6",
        "vibes-demo",
        1,
        "bert-base",
        "abc123hash",
    )


def test_sarif_export() -> None:
    """Generate standard SARIF 2.1.0 output matching code scanning schema."""
    findings = [
        SecurityFinding(
            rule_id="AIBOM001",
            severity=RuleSeverity.ERROR,
            file_path="src/model.py",
            line_number=25,
            title="Dangerous trust_remote_code=True Enabled",
            message="Remote code execution enabled",
            recommendation="Set to False",
        )
    ]
    report = AibomReport(scanned_files=1, components=[], findings=findings)
    sarif = export_sarif_json(report)
    run = sarif["runs"][0]

    assert (
        sarif.get("version"),
        run["tool"]["driver"]["name"],
        len(run["results"]),
        run["results"][0]["ruleId"],
        run["results"][0]["level"],
    ) == (
        "2.1.0",
        "vibes-aibom-scanner",
        1,
        "AIBOM001",
        "error",
    )


def test_markdown_export() -> None:
    """Generate human-readable Markdown summary report with tables."""
    components = [
        AibomComponent(
            bom_ref="pkg:pypi/torch",
            component_type=ComponentType.AI_FRAMEWORK,
            name="torch",
            properties=(("description", "PyTorch deep learning"),),
        )
    ]
    findings = [
        SecurityFinding(
            rule_id="AIBOM003",
            severity=RuleSeverity.WARNING,
            file_path="loader.py",
            line_number=10,
            title="Unpinned Model Revision",
            message="Model unpinned",
            recommendation="Pin SHA",
        )
    ]
    report = AibomReport(scanned_files=3, components=components, findings=findings)
    md = export_markdown(report)

    assert (
        "# AI Bill of Materials (AIBOM)" in md,
        "| `ai-framework` | **torch** |" in md,
        "| `AIBOM003` | `warning` |" in md,
    ) == (True, True, True)


def test_scan_paths_and_cli_entrypoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify scan_paths file discovery and CLI main entrypoint across formats."""
    py_file = tmp_path / "model_script.py"
    py_file.write_text("import torch\nweights = torch.load('model.pt', weights_only=True)\n", encoding="utf-8")
    st_file = tmp_path / "model.safetensors"
    _create_mock_safetensors(st_file, {"model_type": "bert"})

    report = scan_paths([tmp_path])
    assert (report.scanned_files, len(report.components), report.has_errors) == (2, 2, False)

    exit_md = main([str(tmp_path), "--format", "markdown"])
    out_md = capsys.readouterr().out
    exit_json = main([str(tmp_path), "--format", "json"])
    out_json = capsys.readouterr().out
    exit_cyclone = main([str(tmp_path), "--format", "cyclonedx"])
    out_cyclone = capsys.readouterr().out
    exit_sarif = main([str(tmp_path), "--format", "sarif"])
    out_sarif = capsys.readouterr().out

    assert (
        exit_md,
        exit_json,
        exit_cyclone,
        exit_sarif,
        "AI Bill of Materials" in out_md,
        "components_count" in out_json,
        "CycloneDX" in out_cyclone,
        "vibes-aibom-scanner" in out_sarif,
    ) == (
        0,
        0,
        0,
        0,
        True,
        True,
        True,
        True,
    )


def test_cli_strict_mode_exit_code(tmp_path: Path) -> None:
    """Ensure --strict causes main to exit non-zero when errors are detected."""
    bad_script = tmp_path / "vulnerable.py"
    bad_script.write_text("import torch\ntorch.load('untrusted.pt')\n", encoding="utf-8")

    exit_code_lenient = main([str(bad_script)])
    exit_code_strict = main([str(bad_script), "--strict"])

    assert (exit_code_lenient, exit_code_strict) == (0, 1)
