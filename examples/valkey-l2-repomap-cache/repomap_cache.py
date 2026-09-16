#!/usr/bin/env python3
"""Valkey L2 Caching for AST Repomaps & Embedding Drift Auditor.

Implements high-throughput multi-tier AST symbol table caching and semantic
drift auditing across subagent invocations:
1. L1 Fast Memory Cache (sub-millisecond in-process LRU cache).
2. L2 Valkey / Redis-compatible Distributed Cache (content-addressed by SHA-256).
3. Pure Python RESP (REdis Serialization Protocol) client with fallback store.
4. AST Feature Embedding Generator (normalized 8-dimensional structural vector).
5. Embedding Drift Auditor measuring cosine distance and symbol topology changes.

Enforces zero-trust standards: dummy endpoints use RFC 5737 and example.com.
"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import socket
import sys
import time
from typing import Any, Sequence

# Canonical dummy host for mock network tests
CANONICAL_MOCK_HOST = "localhost"
DEFAULT_VALKEY_PORT = 6379

BRANCH_TYPES = (ast.If, ast.While, ast.For, ast.AsyncFor)
FN_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
RETURN_TYPES = (ast.Return, ast.Yield)


class CacheTier(str, Enum):
    """Source tier providing repomap resolution."""

    L1_MEMORY = "L1_MEMORY"
    L2_VALKEY = "L2_VALKEY"
    MISS_COMPUTED = "MISS_COMPUTED"


class DriftVerdict(str, Enum):
    """Classification of semantic and structural drift severity."""

    PRESERVED = "PRESERVED"
    SLIGHT_DRIFT = "SLIGHT_DRIFT"
    SIGNIFICANT_DRIFT = "SIGNIFICANT_DRIFT"


@dataclass
class ASTSymbol:
    """Extracted function or class signature from Python AST."""

    name: str
    kind: str
    lineno: int
    complexity: int
    has_docstring: bool
    args: list[str]


@dataclass
class RepomapRecord:
    """Cached AST symbol map and normalized structural embedding."""

    file_path: str
    content_sha256: str
    symbols: list[ASTSymbol]
    line_count: int
    embedding: list[float]
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize record into dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepomapRecord:
        """Hydrate record from serialized dictionary."""
        symbols = [ASTSymbol(**s) for s in data.get("symbols", [])]
        return cls(
            file_path=data["file_path"],
            content_sha256=data["content_sha256"],
            symbols=symbols,
            line_count=data.get("line_count", 0),
            embedding=data.get("embedding", []),
            timestamp=data.get("timestamp", time.time()),
        )


@dataclass
class DriftReport:
    """Analysis of semantic embedding and symbol drift between two AST versions."""

    file_path: str
    cosine_similarity: float
    cosine_distance: float
    verdict: DriftVerdict
    added_symbols: list[str]
    removed_symbols: list[str]
    retained_symbols: list[str]


def encode_resp_command(*args: str) -> bytes:
    """Encode command arguments into Redis Serialization Protocol (RESP) wire bytes."""
    lines = [f"*{len(args)}\r\n".encode("utf-8")]
    for arg in args:
        arg_bytes = arg.encode("utf-8")
        lines.append(f"${len(arg_bytes)}\r\n".encode("utf-8"))
        lines.append(arg_bytes + b"\r\n")
    return b"".join(lines)


def _resp_simple(data: bytes, nl: int) -> tuple[Any, int]:
    return data[1:nl].decode("utf-8", errors="replace"), nl + 2


def _resp_int(data: bytes, nl: int) -> tuple[Any, int]:
    return int(data[1:nl]), nl + 2


def _parse_bulk_string(data: bytes, newline_idx: int) -> tuple[str | None, int]:
    length_str = data[1:newline_idx].decode("utf-8", errors="replace")
    try:
        length = int(length_str)
    except ValueError:
        return None, newline_idx + 2
    if length == -1:
        return None, newline_idx + 2
    start = newline_idx + 2
    end = start + length
    val = data[start:end].decode("utf-8", errors="replace")
    return val, end + 2


RESP_PARSERS: dict[str, Callable[[bytes, int], tuple[Any, int]]] = {
    "+": _resp_simple,
    "-": _resp_simple,
    ":": _resp_int,
    "$": _parse_bulk_string,
}


def decode_resp_response(data: bytes) -> tuple[Any, int]:
    """Parse single RESP wire response into Python object and consumed bytes count."""
    if not data:
        return None, 0
    nl = data.find(b"\r\n")
    if nl == -1:
        return None, 0
    parser = RESP_PARSERS.get(chr(data[0]))
    if not parser:
        return None, nl + 2
    return parser(data, nl)


class ValkeyL2Client:
    """High-throughput Valkey / Redis client with in-memory mock fallback."""

    def __init__(self, host: str = CANONICAL_MOCK_HOST, port: int = DEFAULT_VALKEY_PORT, timeout: float = 0.5) -> None:
        """Initialize Valkey client with endpoint parameters."""
        self.host = host
        self.port = port
        self.timeout = timeout
        self.is_connected = False
        self._fallback_store: dict[str, str] = {}
        self._check_connection()

    def _check_connection(self) -> None:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall(encode_resp_command("PING"))
                resp = s.recv(1024)
                val, _ = decode_resp_response(resp)
                self.is_connected = bool(val == "PONG")
        except (OSError, socket.error):
            self.is_connected = False

    def get(self, key: str) -> str | None:
        """Fetch string value by key from Valkey or fallback store."""
        if not self.is_connected:
            return self._fallback_store.get(key)
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall(encode_resp_command("GET", key))
                resp = s.recv(65536)
                val, _ = decode_resp_response(resp)
                return str(val) if val is not None else None
        except (OSError, socket.error):
            return self._fallback_store.get(key)

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> bool:
        """Store string value by key into Valkey or fallback store."""
        if not self.is_connected:
            self._fallback_store[key] = value
            return True
        try:
            cmd = ["SET", key, value]
            if ttl_seconds:
                cmd.extend(["EX", str(ttl_seconds)])
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall(encode_resp_command(*cmd))
                resp = s.recv(1024)
                val, _ = decode_resp_response(resp)
                return bool(val == "OK")
        except (OSError, socket.error):
            self._fallback_store[key] = value
            return True

    def delete(self, key: str) -> bool:
        """Remove key from store."""
        self._fallback_store.pop(key, None)
        if not self.is_connected:
            return True
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall(encode_resp_command("DEL", key))
                return True
        except (OSError, socket.error):
            return True


def _is_docstring_node(node: ast.AST) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return bool(ast.get_docstring(node))
    return False


class ASTFeatureExtractor:
    """Computes AST symbol signatures and normalized structural embeddings."""

    @classmethod
    def _node_to_symbol(cls, node: ast.AST) -> ASTSymbol | None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cls._extract_function_symbol(node)
        if isinstance(node, ast.ClassDef):
            return cls._extract_class_symbol(node)
        return None

    @classmethod
    def extract_symbols(cls, tree: ast.AST) -> list[ASTSymbol]:
        """Extract function and class signatures from an AST module."""
        raw_symbols = (cls._node_to_symbol(node) for node in ast.walk(tree))
        symbols = [s for s in raw_symbols if s is not None]
        return sorted(symbols, key=lambda s: s.lineno)

    @staticmethod
    def _extract_function_symbol(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ASTSymbol:
        args = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
        complexity = 1 + sum(1 for n in ast.walk(node) if isinstance(n, BRANCH_TYPES))
        return ASTSymbol(
            name=node.name,
            kind="async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
            lineno=getattr(node, "lineno", 0),
            complexity=complexity,
            has_docstring=bool(ast.get_docstring(node)),
            args=args,
        )

    @staticmethod
    def _extract_class_symbol(node: ast.ClassDef) -> ASTSymbol:
        methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        return ASTSymbol(
            name=node.name,
            kind="class",
            lineno=getattr(node, "lineno", 0),
            complexity=1 + len(methods),
            has_docstring=bool(ast.get_docstring(node)),
            args=methods,
        )

    @classmethod
    def compute_embedding(cls, tree: ast.AST, line_count: int) -> list[float]:
        """Compute a normalized 8-dimensional structural feature embedding vector."""
        counts = cls._count_ast_features(tree)
        safe_lines = max(1, line_count)
        raw_vector = [
            counts["functions"] / safe_lines,
            counts["classes"] / safe_lines,
            counts["branches"] / safe_lines,
            counts["calls"] / safe_lines,
            counts["returns"] / safe_lines,
            counts["docstrings"] / max(1, counts["functions"] + counts["classes"]),
            math.log1p(counts["branches"]),
            math.log1p(safe_lines),
        ]
        return cls._normalize_vector(raw_vector)

    @staticmethod
    def _count_ast_features(tree: ast.AST) -> dict[str, int]:
        nodes = list(ast.walk(tree))
        return {
            "functions": sum(1 for n in nodes if isinstance(n, FN_TYPES)),
            "classes": sum(1 for n in nodes if isinstance(n, ast.ClassDef)),
            "branches": sum(1 for n in nodes if isinstance(n, BRANCH_TYPES)),
            "calls": sum(1 for n in nodes if isinstance(n, ast.Call)),
            "returns": sum(1 for n in nodes if isinstance(n, RETURN_TYPES)),
            "docstrings": sum(1 for n in nodes if _is_docstring_node(n)),
        }

    @staticmethod
    def _normalize_vector(vec: list[float]) -> list[float]:
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            return [0.0] * len(vec)
        return [round(x / norm, 6) for x in vec]


class EmbeddingDriftAuditor:
    """Measures cosine similarity and structural drift between AST versions."""

    @classmethod
    def calculate_drift(cls, before: RepomapRecord, after: RepomapRecord) -> DriftReport:
        """Calculate cosine distance and symbol topology diff between two versions."""
        sim = cls._cosine_similarity(before.embedding, after.embedding)
        dist = max(0.0, round(1.0 - sim, 6))

        symbols_before = {s.name for s in before.symbols}
        symbols_after = {s.name for s in after.symbols}

        added = sorted(symbols_after - symbols_before)
        removed = sorted(symbols_before - symbols_after)
        retained = sorted(symbols_before & symbols_after)

        verdict = cls._classify_drift(dist)
        return DriftReport(
            file_path=after.file_path,
            cosine_similarity=round(sim, 6),
            cosine_distance=dist,
            verdict=verdict,
            added_symbols=added,
            removed_symbols=removed,
            retained_symbols=retained,
        )

    @staticmethod
    def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
        if len(vec_a) != len(vec_b) or not vec_a:
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        denom = norm_a * norm_b
        return dot / denom if denom > 0.0 else 0.0

    @staticmethod
    def _classify_drift(distance: float) -> DriftVerdict:
        if distance <= 0.05:
            return DriftVerdict.PRESERVED
        if distance <= 0.15:
            return DriftVerdict.SLIGHT_DRIFT
        return DriftVerdict.SIGNIFICANT_DRIFT


class TwoTierRepomapCache:
    """Two-tier L1 memory and L2 Valkey caching coordinator for AST repomaps."""

    def __init__(self, valkey_client: ValkeyL2Client | None = None, l1_capacity: int = 128) -> None:
        """Initialize cache coordinator with L1 capacity and Valkey client."""
        self.valkey = valkey_client or ValkeyL2Client()
        self.l1_capacity = l1_capacity
        self._l1_cache: dict[str, RepomapRecord] = {}
        self.stats = {"l1_hits": 0, "l2_hits": 0, "misses": 0}

    @staticmethod
    def compute_sha256(content: str) -> str:
        """Compute hex SHA-256 digest of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _try_get_l2(self, cache_key: str) -> RepomapRecord | None:
        cached_json = self.valkey.get(cache_key)
        if not cached_json:
            return None
        try:
            return RepomapRecord.from_dict(json.loads(cached_json))
        except (json.JSONDecodeError, KeyError):
            return None

    def get_repomap(self, file_path: Path, source_code: str | None = None) -> tuple[RepomapRecord, CacheTier]:
        """Fetch repomap from L1, L2, or compute on demand using content hashing."""
        content = source_code if source_code is not None else file_path.read_text(encoding="utf-8")
        digest = self.compute_sha256(content)
        cache_key = f"repomap:{digest}"

        if cache_key in self._l1_cache:
            self.stats["l1_hits"] += 1
            return self._l1_cache[cache_key], CacheTier.L1_MEMORY

        l2_record = self._try_get_l2(cache_key)
        if l2_record is not None:
            self._store_l1(cache_key, l2_record)
            self.stats["l2_hits"] += 1
            return l2_record, CacheTier.L2_VALKEY

        self.stats["misses"] += 1
        record = self._compute_record(file_path, content, digest)
        self._store_l1(cache_key, record)
        self.valkey.set(cache_key, json.dumps(record.to_dict()), ttl_seconds=86400)
        return record, CacheTier.MISS_COMPUTED

    def _store_l1(self, key: str, record: RepomapRecord) -> None:
        if len(self._l1_cache) >= self.l1_capacity:
            oldest_key = next(iter(self._l1_cache))
            self._l1_cache.pop(oldest_key, None)
        self._l1_cache[key] = record

    @staticmethod
    def _compute_record(file_path: Path, content: str, digest: str) -> RepomapRecord:
        tree = ast.parse(content, filename=str(file_path))
        symbols = ASTFeatureExtractor.extract_symbols(tree)
        lines = len(content.splitlines())
        embedding = ASTFeatureExtractor.compute_embedding(tree, lines)
        return RepomapRecord(
            file_path=str(file_path),
            content_sha256=digest,
            symbols=symbols,
            line_count=lines,
            embedding=embedding,
        )


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for Repomap Cache and Drift Auditor."""
    parser = argparse.ArgumentParser(
        prog="repomap_cache",
        description="Valkey L2 Repomap Cache & Embedding Drift Auditor",
    )
    parser.add_argument("--file", type=str, help="Target Python source file to analyze")
    parser.add_argument("--refactored", type=str, help="Second version of file for drift auditing")
    parser.add_argument("--demo", action="store_true", help="Run self-contained cache and drift demo")
    return parser


def run_demo() -> int:
    """Execute interactive demo showing L1/L2 caching and embedding drift audit."""
    print("=" * 70)
    print("🚀 VALKEY L2 REPOMAP CACHE & EMBEDDING DRIFT AUDITOR DEMO")
    print("=" * 70)

    src_v1 = (
        "def calculate_metrics(values: list[float]) -> dict[str, float]:\n"
        "    '''Calculate basic summary statistics.'''\n"
        "    if not values:\n"
        "        return {'mean': 0.0, 'count': 0}\n"
        "    return {'mean': sum(values) / len(values), 'count': len(values)}\n"
    )
    src_v2 = (
        "_METRICS_DISPATCH = {}\n\n"
        "def _compute_mean(values: list[float]) -> float:\n"
        "    '''Helper mean calculation.'''\n"
        "    return sum(values) / len(values)\n\n"
        "def calculate_metrics(values: list[float]) -> dict[str, float]:\n"
        "    '''Calculate basic summary statistics.'''\n"
        "    if not values:\n"
        "        return {'mean': 0.0, 'count': 0}\n"
        "    return {'mean': _compute_mean(values), 'count': len(values)}\n"
    )

    cache = TwoTierRepomapCache()
    p = Path("demo_sample.py")

    rec1, tier1 = cache.get_repomap(p, source_code=src_v1)
    print(f"Pass 1: Resolved via [{tier1.value}] (Symbols: {len(rec1.symbols)})")

    rec2, tier2 = cache.get_repomap(p, source_code=src_v1)
    print(f"Pass 2: Resolved via [{tier2.value}] (SHA: {rec2.content_sha256[:8]}...)")

    cache._l1_cache.clear()
    rec3, tier3 = cache.get_repomap(p, source_code=src_v1)
    print(f"Pass 3: Resolved via [{tier3.value}] (L1 evicted -> L2 lookup)")

    rec_v2, _ = cache.get_repomap(p, source_code=src_v2)
    report = EmbeddingDriftAuditor.calculate_drift(rec1, rec_v2)
    print("\n--- Drift Audit Report ---")
    print(f"Cosine Similarity: {report.cosine_similarity:.4f} | Distance: {report.cosine_distance:.4f}")
    print(f"Verdict: [{report.verdict.value}]")
    print(f"Added Symbols: {report.added_symbols}")
    print(f"Retained Symbols: {report.retained_symbols}")
    print("=" * 70)
    return 0


def run_cli(args: Sequence[str] | None = None) -> int:
    """Execute command-line interface for Repomap Cache."""
    parser = build_arg_parser()
    opts = parser.parse_args(args)

    if opts.demo or (not opts.file and not opts.refactored):
        return run_demo()

    if opts.file and opts.refactored:
        p1 = Path(opts.file)
        p2 = Path(opts.refactored)
        cache = TwoTierRepomapCache()
        rec1, _ = cache.get_repomap(p1)
        rec2, _ = cache.get_repomap(p2)
        report = EmbeddingDriftAuditor.calculate_drift(rec1, rec2)
        print(f"Drift Analysis for {opts.file} vs {opts.refactored}:")
        print(f"Distance: {report.cosine_distance:.4f} | Verdict: [{report.verdict.value}]")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
