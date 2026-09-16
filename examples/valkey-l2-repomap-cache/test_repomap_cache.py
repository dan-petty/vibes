"""Unit tests for Valkey L2 Repomap Cache and Embedding Drift Auditor.

Verifies:
- RESP wire protocol command encoding and multi-type payload decoding.
- In-memory mock fallback of Valkey L2 client for zero-dependency CI.
- AST symbol extraction across functions, async functions, and classes.
- Normalized structural feature embedding vector generation.
- Two-tier caching lifecycle (L1 memory hit, L2 distributed hit, content-addressed miss).
- Content-addressed cache invalidation on source modification.
- Cosine distance measurement and semantic drift classification.
- Interactive CLI demo execution.
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys

_app_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_app_dir))

from repomap_cache import (
    ASTFeatureExtractor,
    CacheTier,
    DriftVerdict,
    EmbeddingDriftAuditor,
    TwoTierRepomapCache,
    ValkeyL2Client,
    decode_resp_response,
    encode_resp_command,
    run_demo,
)

SAMPLE_PY_V1 = """
def calculate_metrics(values: list[float]) -> dict[str, float]:
    '''Calculate basic summary statistics.'''
    if not values:
        return {'mean': 0.0, 'count': 0}
    return {'mean': sum(values) / len(values), 'count': len(values)}
"""

SAMPLE_PY_V2 = """
_METRICS_DISPATCH = {}

def _compute_mean(values: list[float]) -> float:
    '''Helper mean calculation.'''
    return sum(values) / len(values)

def calculate_metrics(values: list[float]) -> dict[str, float]:
    '''Calculate basic summary statistics.'''
    if not values:
        return {'mean': 0.0, 'count': 0}
    return {'mean': _compute_mean(values), 'count': len(values)}
"""

SAMPLE_PY_V3 = """
class DataPipeline:
    '''Class based pipeline.'''
    def run(self) -> None:
        pass
"""


def test_resp_encoding_and_decoding() -> None:
    """Verify RESP wire protocol encoding and parsing across primitive types."""
    encoded = encode_resp_command("PING")
    assert encoded == b"*1\r\n$4\r\nPING\r\n"

    simple_resp, len1 = decode_resp_response(b"+PONG\r\n")
    int_resp, len2 = decode_resp_response(b":42\r\n")
    bulk_resp, len3 = decode_resp_response(b"$5\r\nhello\r\n")
    null_resp, len4 = decode_resp_response(b"$-1\r\n")

    assert (simple_resp, int_resp, bulk_resp, null_resp) == ("PONG", 42, "hello", None)
    assert (len1, len2, len3, len4) == (7, 5, 11, 5)


def test_valkey_client_in_memory_fallback() -> None:
    """Verify in-memory fallback store when Valkey daemon is not present."""
    client = ValkeyL2Client(host="127.0.0.1", port=65530)
    assert client.is_connected is False

    set_ok = client.set("key:test", "sample_val", ttl_seconds=60)
    val = client.get("key:test")
    del_ok = client.delete("key:test")
    post_del = client.get("key:test")

    assert (set_ok, val, del_ok, post_del) == (True, "sample_val", True, None)


def test_ast_symbol_extraction() -> None:
    """Verify AST symbol extraction for functions and classes."""
    tree = ast.parse(SAMPLE_PY_V1)
    symbols = ASTFeatureExtractor.extract_symbols(tree)
    assert len(symbols) == 1
    sym = symbols[0]
    assert (sym.name, sym.kind, sym.has_docstring, sym.args) == (
        "calculate_metrics",
        "function",
        True,
        ["values"],
    )


def test_embedding_computation_and_normalization() -> None:
    """Verify normalized 8-dimensional feature embedding computation."""
    tree = ast.parse(SAMPLE_PY_V1)
    embedding = ASTFeatureExtractor.compute_embedding(tree, 6)
    assert len(embedding) == 8
    # Verify unit vector normalization ||v|| ~= 1.0
    norm_sq = sum(x * x for x in embedding)
    assert 0.99 <= norm_sq <= 1.01


def test_two_tier_caching_flow() -> None:
    """Verify L1 memory cache hit, L2 distributed cache hit, and computed miss."""
    client = ValkeyL2Client(host="127.0.0.1", port=65530)
    cache = TwoTierRepomapCache(valkey_client=client)
    p = Path("test_sample.py")

    # Pass 1: Miss computed
    rec1, tier1 = cache.get_repomap(p, source_code=SAMPLE_PY_V1)
    assert (tier1, len(rec1.symbols)) == (CacheTier.MISS_COMPUTED, 1)

    # Pass 2: L1 memory hit
    rec2, tier2 = cache.get_repomap(p, source_code=SAMPLE_PY_V1)
    assert (tier2, rec2.content_sha256) == (CacheTier.L1_MEMORY, rec1.content_sha256)

    # Pass 3: Evict L1 to verify L2 Valkey hit
    cache._l1_cache.clear()
    rec3, tier3 = cache.get_repomap(p, source_code=SAMPLE_PY_V1)
    assert (tier3, rec3.content_sha256) == (CacheTier.L2_VALKEY, rec1.content_sha256)


def test_cache_invalidation_on_content_change() -> None:
    """Verify content hashing invalidates cache when source code is altered."""
    cache = TwoTierRepomapCache()
    p = Path("test_sample.py")

    rec1, tier1 = cache.get_repomap(p, source_code=SAMPLE_PY_V1)
    rec2, tier2 = cache.get_repomap(p, source_code=SAMPLE_PY_V2)

    assert (tier1, tier2) == (CacheTier.MISS_COMPUTED, CacheTier.MISS_COMPUTED)
    assert rec1.content_sha256 != rec2.content_sha256


def test_embedding_drift_auditor_preserved() -> None:
    """Verify cosine distance on refactored function preserves structural semantics."""
    cache = TwoTierRepomapCache()
    p = Path("test_sample.py")
    rec1, _ = cache.get_repomap(p, source_code=SAMPLE_PY_V1)
    rec2, _ = cache.get_repomap(p, source_code=SAMPLE_PY_V2)

    report = EmbeddingDriftAuditor.calculate_drift(rec1, rec2)
    assert report.cosine_distance <= 0.05
    assert report.verdict == DriftVerdict.PRESERVED
    assert (report.added_symbols, report.retained_symbols) == (
        ["_compute_mean"],
        ["calculate_metrics"],
    )


def test_embedding_drift_auditor_significant_drift() -> None:
    """Verify cosine distance on completely changed architecture detects drift."""
    cache = TwoTierRepomapCache()
    p = Path("test_sample.py")
    rec1, _ = cache.get_repomap(p, source_code=SAMPLE_PY_V1)
    rec3, _ = cache.get_repomap(p, source_code=SAMPLE_PY_V3)

    report = EmbeddingDriftAuditor.calculate_drift(rec1, rec3)
    assert report.cosine_distance > 0.05
    assert report.verdict in (DriftVerdict.SLIGHT_DRIFT, DriftVerdict.SIGNIFICANT_DRIFT)


def test_run_demo() -> None:
    """Verify that interactive demo runs to completion."""
    assert run_demo() == 0
