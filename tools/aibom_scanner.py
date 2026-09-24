#!/usr/bin/env python3
"""AI Bill of Materials (AIBOM) & Supply-Chain Security Scanner.

Generates machine-readable AIBOM inventories compliant with CycloneDX 1.6 ML-BOM,
extracts pretrained weights provenance and SafeTensors/GGUF headers without executing
untrusted code, and audits AI codebases for critical supply-chain vulnerabilities:
- AIBOM001: trust_remote_code=True arbitrary code execution risk (CVE-2024-4372)
- AIBOM002: Unsafe checkpoint loading via pickle without weights_only=True
- AIBOM003: Unpinned model revision susceptible to upstream model confusion
- AIBOM004: Plaintext HTTP model weight and configuration download
- AIBOM005: Legacy pickle format model checkpoints (.bin/.pt/.pkl)
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import struct
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

MAX_WEIGHT_INSPECT_BYTES: Final[int] = 50 * 1024 * 1024
MAX_SAFETENSORS_HEADER_BYTES: Final[int] = 10 * 1024 * 1024
HASH_CHUNK_SIZE: Final[int] = 64 * 1024

GGUF_MAGIC: Final[bytes] = b"GGUF"
CANONICAL_MOCK_HOST: Final[str] = "example.com"

HEX_SHA_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{40}$")
MODEL_REF_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

AI_FRAMEWORK_MODULES: Final[dict[str, str]] = {
    "torch": "PyTorch deep learning framework",
    "transformers": "Hugging Face Transformers model hub and architecture library",
    "safetensors": "Hugging Face SafeTensors secure serialization format",
    "onnx": "Open Neural Network Exchange model format",
    "onnxruntime": "ONNX Runtime cross-platform acceleration engine",
    "vllm": "vLLM high-throughput LLM serving engine",
    "ollama": "Ollama local model execution runtime",
    "datasets": "Hugging Face Datasets hub and streaming library",
    "diffusers": "Hugging Face Diffusers generative model library",
    "timm": "PyTorch Image Models computer vision library",
}


class ComponentType(StrEnum):
    """Component category for AI Bill of Materials inventory."""

    MACHINE_LEARNING_MODEL = "machine-learning-model"
    DATASET = "dataset"
    AI_FRAMEWORK = "ai-framework"
    MODEL_CHECKPOINT = "model-checkpoint"


class ModelFormat(StrEnum):
    """Serialization format of model weight artifacts."""

    SAFETENSORS = "safetensors"
    GGUF = "gguf"
    ONNX = "onnx"
    PYTORCH = "pytorch"
    PICKLE = "pickle"
    UNKNOWN = "unknown"


class RuleSeverity(StrEnum):
    """Severity tier for supply chain security findings."""

    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"


@dataclass(frozen=True)
class AibomComponent:
    """An inventoried machine learning model, dataset, or framework component."""

    bom_ref: str
    component_type: ComponentType
    name: str
    version: str = ""
    format: ModelFormat = ModelFormat.UNKNOWN
    hashes: tuple[tuple[str, str], ...] = ()
    licenses: tuple[str, ...] = ()
    external_references: tuple[tuple[str, str], ...] = ()
    properties: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SecurityFinding:
    """A supply-chain or deserialization vulnerability identified in code or artifacts."""

    rule_id: str
    severity: RuleSeverity
    file_path: str
    line_number: int
    title: str
    message: str
    snippet: str = ""
    recommendation: str = ""


@dataclass(frozen=True)
class AibomReport:
    """Aggregated scan results containing components, security findings, and telemetry."""

    scanned_files: int
    components: list[AibomComponent]
    findings: list[SecurityFinding]
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def has_errors(self) -> bool:
        """Return True if any security finding is of error severity."""
        return any(f.severity == RuleSeverity.ERROR for f in self.findings)

    @property
    def total_findings(self) -> int:
        """Return total count of security findings."""
        return len(self.findings)


# --- Weight Header Inspection Helpers (Zero-Execution) ---------------------------------


def compute_file_sha256(path: Path) -> str:
    """Compute streaming SHA-256 digest of file without buffering entire content."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(HASH_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def _parse_safetensors_header_dict(raw_json: bytes) -> dict[str, Any]:
    """Parse and return metadata dictionary from SafeTensors header bytes."""
    try:
        data = json.loads(raw_json.decode("utf-8"))
        return data.get("__metadata__", {}) if isinstance(data, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def inspect_safetensors_header(path: Path) -> dict[str, Any]:
    """Extract metadata dictionary from SafeTensors header without loading tensor weights."""
    if not path.is_file():
        return {}
    file_size = path.stat().st_size
    if file_size < 8:
        return {}
    with open(path, "rb") as f:
        header_len_bytes = f.read(8)
        (header_len,) = struct.unpack("<Q", header_len_bytes)
        if header_len > MAX_SAFETENSORS_HEADER_BYTES or header_len + 8 > file_size:
            return {}
        header_bytes = f.read(header_len)
        return _parse_safetensors_header_dict(header_bytes)


def inspect_gguf_header(path: Path) -> dict[str, Any]:
    """Extract metadata header from GGUF model checkpoint without loading tensors."""
    if not path.is_file() or path.stat().st_size < 24:
        return {}
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != GGUF_MAGIC:
            return {}
        version_bytes = f.read(4)
        (version,) = struct.unpack("<I", version_bytes)
        tensors_bytes = f.read(8)
        (tensor_count,) = struct.unpack("<Q", tensors_bytes)
        meta_bytes = f.read(8)
        (kv_count,) = struct.unpack("<Q", meta_bytes)
        return {
            "format": "GGUF",
            "version": version,
            "tensor_count": tensor_count,
            "metadata_kv_count": kv_count,
        }


def _detect_weight_format(path: Path) -> ModelFormat:
    """Classify model file format by suffix and magic byte signature."""
    suffix = path.suffix.lower()
    format_map = {
        ".safetensors": ModelFormat.SAFETENSORS,
        ".gguf": ModelFormat.GGUF,
        ".onnx": ModelFormat.ONNX,
        ".pt": ModelFormat.PYTORCH,
        ".pth": ModelFormat.PYTORCH,
        ".bin": ModelFormat.PICKLE,
        ".pkl": ModelFormat.PICKLE,
    }
    return format_map.get(suffix, ModelFormat.UNKNOWN)


def _extract_weight_properties(path: Path, fmt: ModelFormat, file_size: int) -> list[tuple[str, str]]:
    """Extract format-specific properties and metadata attributes."""
    props: list[tuple[str, str]] = [("file_size_bytes", str(file_size))]
    if fmt == ModelFormat.SAFETENSORS:
        meta = inspect_safetensors_header(path)
        props.extend((f"metadata.{k}", str(v)) for k, v in meta.items())
    elif fmt == ModelFormat.GGUF:
        meta = inspect_gguf_header(path)
        props.extend((f"gguf.{k}", str(v)) for k, v in meta.items())
    return props


def _check_weight_format_risk(path: Path, fmt: ModelFormat) -> list[SecurityFinding]:
    """Flag legacy pickle or unverified binary formats as potential deserialization risks."""
    if fmt in (ModelFormat.PICKLE, ModelFormat.PYTORCH):
        return [
            SecurityFinding(
                rule_id="AIBOM005",
                severity=RuleSeverity.WARNING,
                file_path=str(path),
                line_number=1,
                title="Legacy Pickle Weight Format",
                message=f"Model artifact '{path.name}' uses pickle-based weights susceptible to arbitrary execution.",
                recommendation="Convert model checkpoint to SafeTensors format (.safetensors).",
            )
        ]
    return []


def inspect_weight_file(path: Path) -> tuple[AibomComponent, list[SecurityFinding]]:
    """Inspect model weight file, extracting metadata and flagging legacy pickle formats."""
    fmt = _detect_weight_format(path)
    file_size = path.stat().st_size if path.is_file() else 0
    can_hash = path.is_file() and file_size <= MAX_WEIGHT_INSPECT_BYTES
    sha256_hash = compute_file_sha256(path) if can_hash else ""
    props = _extract_weight_properties(path, fmt, file_size)

    component = AibomComponent(
        bom_ref=f"pkg:ml/{path.name}",
        component_type=ComponentType.MODEL_CHECKPOINT,
        name=path.name,
        format=fmt,
        hashes=(("SHA-256", sha256_hash),) if sha256_hash else (),
        properties=tuple(props),
    )
    return component, _check_weight_format_risk(path, fmt)


# --- AST Security & Inventory Analysis --------------------------------------------------


def _has_keyword_value(call: ast.Call, kwarg_name: str, expected_val: Any) -> bool:
    """Predicate reporting whether a Call node passes a specific keyword argument value."""
    for kw in call.keywords:
        if kw.arg == kwarg_name and isinstance(kw.value, ast.Constant):
            return kw.value.value == expected_val
    return False


def _check_trust_remote_code(call: ast.Call, path: str, line: int) -> SecurityFinding | None:
    """Detect dangerous trust_remote_code=True keyword arguments."""
    if _has_keyword_value(call, "trust_remote_code", True):
        return SecurityFinding(
            rule_id="AIBOM001",
            severity=RuleSeverity.ERROR,
            file_path=path,
            line_number=line,
            title="Dangerous trust_remote_code=True Enabled",
            message="Model loader explicitly enables remote Python code execution from untrusted model repos.",
            recommendation="Audit model repository or set trust_remote_code=False to prevent RCE (CVE-2024-4372).",
        )
    return None


def _is_torch_load_call(call: ast.Call) -> bool:
    """Predicate reporting whether Call node targets torch.load."""
    if isinstance(call.func, ast.Attribute) and call.func.attr == "load":
        return isinstance(call.func.value, ast.Name) and call.func.value.id == "torch"
    return isinstance(call.func, ast.Name) and call.func.id == "torch_load"


def _check_torch_load(call: ast.Call, path: str, line: int) -> SecurityFinding | None:
    """Detect unsafe torch.load invocations lacking weights_only=True."""
    if not _is_torch_load_call(call) or _has_keyword_value(call, "weights_only", True):
        return None
    return SecurityFinding(
        rule_id="AIBOM002",
        severity=RuleSeverity.ERROR,
        file_path=path,
        line_number=line,
        title="Unsafe torch.load Deserialization",
        message="torch.load invoked without weights_only=True, allowing unpickling of arbitrary Python objects.",
        recommendation="Use safetensors.torch.load_file or pass weights_only=True to prevent arbitrary code execution.",
    )


def _is_pinned_revision_keyword(kw: ast.keyword) -> bool:
    """Predicate checking if keyword is a revision parameter with a 40-char commit SHA."""
    if kw.arg == "revision" and isinstance(kw.value, ast.Constant):
        return bool(HEX_SHA_PATTERN.match(str(kw.value.value)))
    return False


def _has_pinned_revision(call: ast.Call) -> bool:
    """Predicate reporting whether Call node supplies a pinned commit hash revision."""
    return any(_is_pinned_revision_keyword(kw) for kw in call.keywords)


def _check_unpinned_revision(call: ast.Call, path: str, line: int) -> SecurityFinding | None:
    """Detect from_pretrained calls lacking an explicit 40-character commit SHA revision."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "from_pretrained"):
        return None
    if _has_pinned_revision(call):
        return None
    return SecurityFinding(
        rule_id="AIBOM003",
        severity=RuleSeverity.WARNING,
        file_path=path,
        line_number=line,
        title="Unpinned Model Revision",
        message="Model loaded via from_pretrained without pinned 40-character commit SHA revision.",
        recommendation="Pin model revision to an immutable commit hash (revision='<sha>') to prevent supply chain tampering.",
    )


def _check_plaintext_url(val: str, path: str, line: int) -> SecurityFinding | None:
    """Detect unencrypted HTTP URLs for model downloads."""
    if val.startswith("http://") and ("huggingface.co" in val or "model" in val or ".safetensors" in val):
        return SecurityFinding(
            rule_id="AIBOM004",
            severity=RuleSeverity.ERROR,
            file_path=path,
            line_number=line,
            title="Plaintext HTTP Model Download",
            message=f"Model weight or config URL uses unencrypted plaintext HTTP: '{val[:60]}...'",
            recommendation="Download model artifacts strictly over TLS (https://) to prevent man-in-the-middle attacks.",
        )
    return None


def _extract_first_string_arg(call: ast.Call) -> str | None:
    """Extract string value of first positional argument in Call node."""
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return None


def _call_target_name(call: ast.Call) -> str:
    """Extract attribute or name identifier from call function."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _extract_model_call_component(call: ast.Call) -> AibomComponent | None:
    """Extract model or dataset component from from_pretrained or load_dataset calls."""
    fn_name = _call_target_name(call)
    if not fn_name:
        return None
    val = _extract_first_string_arg(call)
    if not val or not MODEL_REF_PATTERN.match(val):
        return None
    if fn_name == "from_pretrained":
        return AibomComponent(
            bom_ref=f"pkg:ml/{val}",
            component_type=ComponentType.MACHINE_LEARNING_MODEL,
            name=val,
            external_references=(("vcs", f"https://huggingface.co/{val}"),),
        )
    if fn_name == "load_dataset":
        return AibomComponent(
            bom_ref=f"pkg:dataset/{val}",
            component_type=ComponentType.DATASET,
            name=val,
            external_references=(("vcs", f"https://huggingface.co/datasets/{val}"),),
        )
    return None


class _AibomAstVisitor(ast.NodeVisitor):
    """AST visitor extracting AI components and checking supply chain security invariants."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.components: list[AibomComponent] = []
        self.findings: list[SecurityFinding] = []

    def visit_Import(self, node: ast.Import) -> None:
        """Inspect module imports for AI framework dependencies."""
        for alias in node.names:
            base_mod = alias.name.split(".")[0]
            if base_mod in AI_FRAMEWORK_MODULES:
                self.components.append(
                    AibomComponent(
                        bom_ref=f"pkg:pypi/{base_mod}",
                        component_type=ComponentType.AI_FRAMEWORK,
                        name=base_mod,
                        properties=(("description", AI_FRAMEWORK_MODULES[base_mod]),),
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Inspect from-imports for AI framework dependencies."""
        if node.module:
            base_mod = node.module.split(".")[0]
            if base_mod in AI_FRAMEWORK_MODULES:
                self.components.append(
                    AibomComponent(
                        bom_ref=f"pkg:pypi/{base_mod}",
                        component_type=ComponentType.AI_FRAMEWORK,
                        name=base_mod,
                        properties=(("description", AI_FRAMEWORK_MODULES[base_mod]),),
                    )
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Inspect call expressions for model loadings and security anti-patterns."""
        line = getattr(node, "lineno", 1)
        r_sec = _check_trust_remote_code(node, self.file_path, line)
        if r_sec:
            self.findings.append(r_sec)
        r_torch = _check_torch_load(node, self.file_path, line)
        if r_torch:
            self.findings.append(r_torch)
        r_pin = _check_unpinned_revision(node, self.file_path, line)
        if r_pin:
            self.findings.append(r_pin)

        comp = _extract_model_call_component(node)
        if comp:
            self.components.append(comp)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        """Inspect string constants for unencrypted model download URLs."""
        if isinstance(node.value, str):
            finding = _check_plaintext_url(node.value, self.file_path, getattr(node, "lineno", 1))
            if finding:
                self.findings.append(finding)
        self.generic_visit(node)


def scan_python_code(source: str, file_path: str = "inline.py") -> tuple[list[AibomComponent], list[SecurityFinding]]:
    """Scan Python source code, extracting AI components and auditing security findings."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], []
    visitor = _AibomAstVisitor(file_path)
    visitor.visit(tree)
    return visitor.components, visitor.findings


# --- Scanner Orchestrator & Exporters ---------------------------------------------------


def _deduplicate_components(components: Sequence[AibomComponent]) -> list[AibomComponent]:
    """Deduplicate AIBOM components by unique bom_ref."""
    seen: set[str] = set()
    result: list[AibomComponent] = []
    for c in components:
        if c.bom_ref not in seen:
            seen.add(c.bom_ref)
            result.append(c)
    return result


def _read_file_text_safe(path: Path) -> str:
    """Read file text safely with UTF-8 replacement, returning empty string on error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _scan_single_file(path: Path) -> tuple[list[AibomComponent], list[SecurityFinding]]:
    """Scan single file according to its format (Python or model weight)."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        content = _read_file_text_safe(path)
        return scan_python_code(content, str(path)) if content else ([], [])
    if suffix in (".safetensors", ".gguf", ".onnx", ".pt", ".bin", ".pkl"):
        comp, findings = inspect_weight_file(path)
        return [comp], findings
    return [], []


def _is_unpruned_file(child: Path) -> bool:
    """Predicate reporting whether child is an unpruned non-hidden file."""
    return child.is_file() and not any(part.startswith(".") for part in child.parts)


def _collect_dir_files(path: Path) -> Iterator[Path]:
    """Yield all unpruned files under directory path."""
    for child in path.rglob("*"):
        if _is_unpruned_file(child):
            yield child


def _resolve_path_files(path: Path) -> list[Path]:
    """Return file list for single path, resolving directories recursively."""
    if path.is_file():
        return [path]
    if path.is_dir():
        return list(_collect_dir_files(path))
    return []


def _collect_target_files(paths: Iterable[Path]) -> list[Path]:
    """Collect eligible target files from paths, pruning hidden directories."""
    files: list[Path] = []
    for path in paths:
        files.extend(_resolve_path_files(path))
    return files


def scan_paths(paths: Iterable[Path]) -> AibomReport:
    """Scan sequence of files and directories for AI assets and supply chain risks."""
    files = _collect_target_files(paths)
    all_components: list[AibomComponent] = []
    all_findings: list[SecurityFinding] = []

    for file_path in files:
        comps, finds = _scan_single_file(file_path)
        all_components.extend(comps)
        all_findings.extend(finds)

    return AibomReport(
        scanned_files=len(files),
        components=_deduplicate_components(all_components),
        findings=all_findings,
    )


def _format_hashes_list(hashes: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    """Format hash tuples into CycloneDX hash list."""
    return [{"alg": alg, "content": h} for alg, h in hashes]


def _format_ext_refs_list(refs: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    """Format external reference tuples into CycloneDX reference list."""
    return [{"type": t, "url": u} for t, u in refs]


def _format_props_list(props: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    """Format property key-value tuples into CycloneDX property list."""
    return [{"name": k, "value": v} for k, v in props]


def _component_to_cyclonedx(c: AibomComponent) -> dict[str, Any]:
    """Convert AibomComponent to standard CycloneDX 1.6 component dictionary."""
    comp: dict[str, Any] = {
        "bom-ref": c.bom_ref,
        "type": c.component_type.value,
        "name": c.name,
    }
    if c.version:
        comp["version"] = c.version
    if c.hashes:
        comp["hashes"] = _format_hashes_list(c.hashes)
    if c.external_references:
        comp["externalReferences"] = _format_ext_refs_list(c.external_references)
    if c.properties:
        comp["properties"] = _format_props_list(c.properties)
    return comp


def export_cyclonedx_json(report: AibomReport, project_name: str = "vibes") -> dict[str, Any]:
    """Export AIBOM report in standard CycloneDX 1.6 ML-BOM JSON format."""
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "timestamp": report.timestamp,
            "tools": [{"vendor": "vibes", "name": "aibom-scanner", "version": "1.0.0"}],
            "component": {"type": "application", "name": project_name},
        },
        "components": [_component_to_cyclonedx(c) for c in report.components],
    }


def export_sarif_json(report: AibomReport) -> dict[str, Any]:
    """Export security findings in standard SARIF 2.1.0 schema format."""
    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []

    for f in report.findings:
        rules.setdefault(
            f.rule_id,
            {
                "id": f.rule_id,
                "name": f.title,
                "shortDescription": {"text": f.title},
                "defaultConfiguration": {"level": f.severity.value},
            },
        )
        results.append(
            {
                "ruleId": f.rule_id,
                "level": f.severity.value,
                "message": {"text": f"{f.message} ({f.recommendation})"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f.file_path},
                            "region": {"startLine": f.line_number},
                        }
                    }
                ],
            }
        )

    return {
        "$schema": "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "vibes-aibom-scanner",
                        "informationUri": "https://github.com/dan-petty/vibes",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def export_markdown(report: AibomReport) -> str:
    """Generate human-readable GitHub-Flavored Markdown summary report."""
    lines = [
        "# AI Bill of Materials (AIBOM) & Security Report",
        "",
        f"**Scanned Files:** {report.scanned_files} | **AI Components:** {len(report.components)} | **Security Findings:** {report.total_findings}",
        "",
        "## Inventoried AI Components",
        "",
        "| Category | Component Name | Format | Details |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for c in report.components:
        details = ", ".join(f"{k}={v}" for k, v in c.properties[:2]) if c.properties else "N/A"
        lines.append(f"| `{c.component_type.value}` | **{c.name}** | `{c.format.value}` | {details} |")

    lines.extend([
        "",
        "## Supply Chain Security Findings",
        "",
    ])
    if not report.findings:
        lines.append("✅ **Zero AI supply chain vulnerabilities detected.**")
    else:
        lines.extend([
            "| Rule | Severity | Location | Issue | Remediation |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])
        for f in report.findings:
            loc = f"{f.file_path}:{f.line_number}"
            lines.append(f"| `{f.rule_id}` | `{f.severity.value}` | `{loc}` | {f.title} | {f.recommendation} |")

    return "\n".join(lines) + "\n"


# --- CLI Entrypoint -------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for AIBOM and model security scanner."""
    parser = argparse.ArgumentParser(
        description="AI Bill of Materials (AIBOM) & Supply-Chain Security Scanner.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", default=["."], help="Files or directories to scan")
    parser.add_argument(
        "--format",
        choices=["markdown", "cyclonedx", "sarif", "json"],
        default="markdown",
        help="Report serialization format",
    )
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if error-level findings are detected")
    parser.add_argument("--out", type=Path, default=None, help="Save generated report to file path")
    return parser


def _serialize_report_json(report: AibomReport) -> str:
    """Serialize raw report structures to indented JSON string."""
    data = {
        "scanned_files": report.scanned_files,
        "components_count": len(report.components),
        "findings_count": report.total_findings,
        "components": [c.__dict__ for c in report.components],
        "findings": [f.__dict__ for f in report.findings],
    }
    return json.dumps(data, indent=2)


def _serialize_report(report: AibomReport, fmt: str) -> str:
    """Dispatch report serialization by requested format string."""
    if fmt == "cyclonedx":
        return json.dumps(export_cyclonedx_json(report), indent=2)
    if fmt == "sarif":
        return json.dumps(export_sarif_json(report), indent=2)
    if fmt == "json":
        return _serialize_report_json(report)
    return export_markdown(report)


def _emit_output(output: str, out_path: Path | None, fmt: str) -> None:
    """Write serialized output to file or print to standard output."""
    if out_path:
        out_path.write_text(output, encoding="utf-8")
        print(f"Wrote {fmt} report to {out_path}")
    else:
        print(output)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI execution entrypoint."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    target_paths = [Path(p) for p in args.paths]
    report = scan_paths(target_paths)
    output = _serialize_report(report, args.format)
    _emit_output(output, args.out, args.format)

    if args.strict and report.has_errors:
        print("❌ Strict mode violation: High-severity AI supply-chain findings detected!", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
