#!/usr/bin/env python3
"""Valkey L2 Caching for AST Repomaps & Embedding Drift Auditor.

Implements multi-tier AST symbol table caching and structural
drift auditing across subagent invocations:
1. L1 Fast Memory Cache (in-process LRU cache; a hit refreshes a key).
2. L2 Valkey / Redis-compatible Distributed Cache (content-addressed by SHA-256).
3. Pure Python RESP (REdis Serialization Protocol) client with a reported fallback store.
4. AST Feature Embedding Generator (normalized shape, operator and identifier-name blocks).
5. Embedding Drift Auditor measuring cosine distance and symbol topology changes.

The default endpoint is localhost:6379. When nothing answers there, or a command fails,
an in-process dict stands in for L2. The client logs one warning, keeps the reason in
`fallback_reason`, and the cache reports each hit from the dict as `L2_FALLBACK`.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import math
import socket
import sys
import time
import zlib
from collections import Counter, OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

# Canonical dummy host for mock network tests
CANONICAL_MOCK_HOST = "localhost"
DEFAULT_VALKEY_PORT = 6379

BRANCH_TYPES = (ast.If, ast.While, ast.For, ast.AsyncFor)
FN_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
RETURN_TYPES = (ast.Return, ast.Yield)

# One vector dimension per operator type, so `==` and `!=` are different features. The
# order is fixed here, not read from the `ast` module, so a cached vector keeps its meaning.
OPERATOR_TYPES: tuple[type[ast.AST], ...] = (
    ast.Add, ast.Sub, ast.Mult, ast.MatMult, ast.Div, ast.Mod, ast.Pow, ast.LShift,
    ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd, ast.FloorDiv,
    ast.And, ast.Or,
    ast.Invert, ast.Not, ast.UAdd, ast.USub,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn,
)

# Identifier names go into this many buckets by a stable hash (CRC-32). `hash()` changes
# per process, so a vector made with it would not match its own cached copy.
NAME_BUCKETS = 64
_IDENTIFIER_FIELDS: dict[type[ast.AST], str] = {
    ast.Name: "id",
    ast.Attribute: "attr",
    ast.arg: "arg",
    ast.FunctionDef: "name",
    ast.AsyncFunctionDef: "name",
    ast.ClassDef: "name",
}

# The key carries the record version. A record from the older 8-feature vector has a
# different length, and the cosine of two vectors of different length is not defined.
CACHE_KEY_PREFIX = "repomap:v2:"

logger = logging.getLogger(__name__)


class CacheTier(StrEnum):
    """Source tier providing repomap resolution."""

    L1_MEMORY = "L1_MEMORY"
    L2_VALKEY = "L2_VALKEY"
    L2_FALLBACK = "L2_FALLBACK"
    MISS_COMPUTED = "MISS_COMPUTED"


class DriftVerdict(StrEnum):
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
    lines = [f"*{len(args)}\r\n".encode()]
    for arg in args:
        arg_bytes = arg.encode("utf-8")
        lines.append(f"${len(arg_bytes)}\r\n".encode())
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
    if len(data) < end + 2:
        # The declared length has not arrived yet. Slicing anyway produced a short string
        # with a plausible consumed count and no error at all, so a value split across TCP
        # segments — which a conformant server may do, and Valkey does above roughly 16 KB —
        # came back silently clipped. `0` consumed is the signal to read more.
        return None, 0
    val = data[start:end].decode("utf-8", errors="replace")
    return val, end + 2


RESP_PARSERS: dict[str, Callable[[bytes, int], tuple[Any, int]]] = {
    "+": _resp_simple,
    "-": _resp_simple,
    ":": _resp_int,
    "$": _parse_bulk_string,
}


def read_resp_reply(sock: Any, chunk: int = 65536) -> Any:
    """Read until one complete RESP reply has arrived, then decode it.

    A single `recv` is not a reply. TCP delivers a stream, and a conformant server is free
    to write the `$<len>\\r\\n` header and the payload in separate segments — which Valkey
    does above roughly 16 KB. `decode_resp_response` then parsed a truncated buffer and
    returned a short string with no error, so a large cached value came back silently
    clipped and every consumer treated the fragment as the whole entry.

    `decode_resp_response` reports how many bytes it consumed and returns `(None, 0)` when
    it has not yet seen a whole reply, which is exactly the signal a read loop needs.
    """
    buffer = b""
    while True:
        value, consumed = decode_resp_response(buffer)
        if consumed:
            return value
        received = sock.recv(chunk)
        if not received:
            # The peer closed before completing the reply. A partial answer is not an
            # answer, and it is not a nil reply either: `None` would read as a cache miss.
            raise ConnectionError("connection closed before a complete RESP reply")
        buffer += received


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
    """Valkey / Redis RESP client (one TCP connection per command) with in-memory fallback.

    The client uses the server only while `is_connected` is true. A failed `PING` at start,
    or a transport error on any later command, moves it to the in-process dict for the rest
    of its life: reads and writes then do not split between two stores. The move logs one
    warning and records why in `fallback_reason`. A nil `GET` reply is a cache miss, not a
    failure.
    """

    def __init__(
        self, host: str = CANONICAL_MOCK_HOST, port: int = DEFAULT_VALKEY_PORT, timeout: float = 0.5
    ) -> None:
        """Initialize Valkey client with endpoint parameters."""
        self.host = host
        self.port = port
        self.timeout = timeout
        self.is_connected = False
        self.fallback_reason: str | None = None
        self._fallback_store: dict[str, str] = {}
        self._check_connection()

    def _execute_command(self, *cmd: str) -> Any:
        """Send one command on a new connection. A transport failure raises `OSError`."""
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.sendall(encode_resp_command(*cmd))
            return read_resp_reply(s)

    def _use_fallback(self, reason: str) -> None:
        """Move to the in-process dict, record why, and warn. It runs once per client."""
        self.is_connected = False
        self.fallback_reason = reason
        logger.warning(
            "Valkey at %s:%s is not in use (%s). An in-process dict serves L2 for this client.",
            self.host,
            self.port,
            reason,
        )

    def _call(self, *cmd: str) -> tuple[bool, Any]:
        """Run one command on the server. Return (False, None) when it moved to the fallback."""
        try:
            return True, self._execute_command(*cmd)
        except OSError as exc:
            self._use_fallback(f"{cmd[0]} failed: {exc}")
            return False, None

    def _check_connection(self) -> None:
        answered, reply = self._call("PING")
        if not answered:
            return
        if reply != "PONG":
            self._use_fallback(f"PING replied {reply!r}")
            return
        self.is_connected = True

    def get(self, key: str) -> str | None:
        """Fetch string value by key from Valkey or fallback store."""
        answered, reply = self._call("GET", key) if self.is_connected else (False, None)
        if not answered:
            return self._fallback_store.get(key)
        return None if reply is None else str(reply)

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> bool:
        """Store string value by key into Valkey or fallback store."""
        cmd = ["SET", key, value] + (["EX", str(ttl_seconds)] if ttl_seconds else [])
        answered, reply = self._call(*cmd) if self.is_connected else (False, None)
        if not answered:
            self._fallback_store[key] = value
            return True
        return bool(reply == "OK")

    def delete(self, key: str) -> bool:
        """Remove key from store."""
        self._fallback_store.pop(key, None)
        if self.is_connected:
            self._call("DEL", key)
        return True


def _is_docstring_node(node: ast.AST) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return bool(ast.get_docstring(node))
    return False


# One predicate per structural feature, so adding a feature is a row rather than another
# generator expression over another walk of the same tree.
_FEATURE_TESTS: dict[str, Callable[[ast.AST], bool]] = {
    "functions": lambda n: isinstance(n, FN_TYPES),
    "classes": lambda n: isinstance(n, ast.ClassDef),
    "branches": lambda n: isinstance(n, BRANCH_TYPES),
    "calls": lambda n: isinstance(n, ast.Call),
    "returns": lambda n: isinstance(n, RETURN_TYPES),
    "docstrings": _is_docstring_node,
}


def _tally_node_features(node: ast.AST, counts: dict[str, int]) -> None:
    """Tally matching AST structural feature counts for a single syntax node."""
    for name, test in _FEATURE_TESTS.items():
        if test(node):
            counts[name] += 1


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
    def _fn_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        return [a.arg for a in node.args.args if a.arg not in ("self", "cls")]

    @staticmethod
    def _fn_complexity(node: ast.AST) -> int:
        return 1 + sum(1 for n in ast.walk(node) if isinstance(n, BRANCH_TYPES))

    @classmethod
    def _extract_function_symbol(cls, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ASTSymbol:
        kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
        return ASTSymbol(
            name=node.name,
            kind=kind,
            lineno=getattr(node, "lineno", 0),
            complexity=cls._fn_complexity(node),
            has_docstring=bool(ast.get_docstring(node)),
            args=cls._fn_args(node),
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
        """Compute a normalized vector of three blocks: shape, operators and names.

        Each block is scaled to unit length before the blocks are joined. The cosine of two
        such vectors is then the mean of the three block cosines, so no block can hide the
        others. In the older single vector, two `ln(1 + n)` terms outweighed every density,
        and unrelated modules scored PRESERVED.
        """
        counts = cls._count_ast_features(tree)
        safe_lines = max(1, line_count)
        shape = [
            counts["functions"] / safe_lines,
            counts["classes"] / safe_lines,
            counts["branches"] / safe_lines,
            counts["calls"] / safe_lines,
            counts["returns"] / safe_lines,
            counts["docstrings"] / max(1, counts["functions"] + counts["classes"]),
        ]
        operators, names = cls._operator_and_name_counts(tree)
        joined = [x for block in (shape, operators, names) for x in _unit_vector(block)]
        return cls._normalize_vector(joined)

    @staticmethod
    def _count_ast_features(tree: ast.AST) -> dict[str, int]:
        """Count every structural feature in one pass over the tree."""
        counts = dict.fromkeys(_FEATURE_TESTS, 0)
        for node in ast.walk(tree):
            _tally_node_features(node, counts)
        return counts

    @staticmethod
    def _operator_and_name_counts(tree: ast.AST) -> tuple[list[float], list[float]]:
        """Count each operator type, and each identifier name by its hash bucket, in one pass."""
        types: Counter[type[ast.AST]] = Counter()
        names = [0.0] * NAME_BUCKETS
        for node in ast.walk(tree):
            types[type(node)] += 1
            field_name = _IDENTIFIER_FIELDS.get(type(node))
            if field_name:
                names[_name_bucket(getattr(node, field_name))] += 1.0
        return [float(types[op]) for op in OPERATOR_TYPES], names

    @staticmethod
    def _normalize_vector(vec: list[float]) -> list[float]:
        return [round(x, 6) for x in _unit_vector(vec)]


def _unit_vector(vec: list[float]) -> list[float]:
    """Scale a vector to unit length; a zero vector stays zero."""
    norm = math.hypot(*vec)
    if norm == 0.0:
        return [0.0] * len(vec)
    return [x / norm for x in vec]


def _name_bucket(name: str) -> int:
    """Map an identifier name to its bucket with a hash that is the same in every process."""
    return zlib.crc32(name.encode("utf-8")) % NAME_BUCKETS


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
        if not vec_a or len(vec_a) != len(vec_b):
            return 0.0
        denom = math.hypot(*vec_a) * math.hypot(*vec_b)
        if denom == 0.0:
            return 0.0
        return sum(a * b for a, b in zip(vec_a, vec_b, strict=True)) / denom

    @staticmethod
    def _classify_drift(distance: float) -> DriftVerdict:
        if distance <= 0.05:
            return DriftVerdict.PRESERVED
        if distance <= 0.15:
            return DriftVerdict.SLIGHT_DRIFT
        return DriftVerdict.SIGNIFICANT_DRIFT


_TIER_STATS = {CacheTier.L2_VALKEY: "l2_hits", CacheTier.L2_FALLBACK: "l2_fallback_hits"}


class TwoTierRepomapCache:
    """Two-tier L1 memory and L2 Valkey caching coordinator for AST repomaps."""

    def __init__(self, valkey_client: ValkeyL2Client | None = None, l1_capacity: int = 128) -> None:
        """Initialize cache coordinator with L1 capacity and Valkey client."""
        self.valkey = valkey_client or ValkeyL2Client()
        self.l1_capacity = l1_capacity
        self._l1_cache: OrderedDict[str, RepomapRecord] = OrderedDict()
        self.stats = {"l1_hits": 0, "l2_hits": 0, "l2_fallback_hits": 0, "misses": 0}

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
        cache_key = f"{CACHE_KEY_PREFIX}{digest}"

        if cache_key in self._l1_cache:
            self._l1_cache.move_to_end(cache_key)
            self.stats["l1_hits"] += 1
            return self._l1_cache[cache_key], CacheTier.L1_MEMORY

        l2_record = self._try_get_l2(cache_key)
        if l2_record is not None:
            self._store_l1(cache_key, l2_record)
            # Read the state after the GET: a GET that failed has already moved the client.
            tier = CacheTier.L2_VALKEY if self.valkey.is_connected else CacheTier.L2_FALLBACK
            self.stats[_TIER_STATS[tier]] += 1
            return l2_record, tier

        self.stats["misses"] += 1
        record = self._compute_record(file_path, content, digest)
        self._store_l1(cache_key, record)
        self.valkey.set(cache_key, json.dumps(record.to_dict()), ttl_seconds=86400)
        return record, CacheTier.MISS_COMPUTED

    def _store_l1(self, key: str, record: RepomapRecord) -> None:
        """Insert as most recently used, then evict the least recently used over capacity."""
        self._l1_cache[key] = record
        self._l1_cache.move_to_end(key)
        if len(self._l1_cache) > self.l1_capacity:
            self._l1_cache.popitem(last=False)

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
    _rec3, tier3 = cache.get_repomap(p, source_code=src_v1)
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


def _run_drift_cli(file1: str, file2: str) -> int:
    """Execute drift analysis between two file revisions."""
    cache = TwoTierRepomapCache()
    rec1, _ = cache.get_repomap(Path(file1))
    rec2, _ = cache.get_repomap(Path(file2))
    report = EmbeddingDriftAuditor.calculate_drift(rec1, rec2)
    print(f"Drift Analysis for {file1} vs {file2}:")
    print(f"Distance: {report.cosine_distance:.4f} | Verdict: [{report.verdict.value}]")
    return 0


def run_cli(args: Sequence[str] | None = None) -> int:
    """Execute command-line interface for Repomap Cache."""
    parser = build_arg_parser()
    opts = parser.parse_args(args)

    if opts.file and opts.refactored:
        return _run_drift_cli(opts.file, opts.refactored)
    return run_demo()


if __name__ == "__main__":
    sys.exit(run_cli())
