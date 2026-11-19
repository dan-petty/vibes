"""Unit tests for Valkey L2 Repomap Cache and Embedding Drift Auditor.

Verifies:
- RESP wire protocol command encoding and multi-type payload decoding.
- In-memory fallback of the Valkey L2 client: one warning, a reason, and its own tier.
- AST symbol extraction across functions, async functions, and classes.
- Normalized structural vector of shape, operator and identifier-name blocks.
- Two-tier caching lifecycle (L1 memory hit, L2 hit, content-addressed miss) and LRU order.
- Content-addressed cache invalidation on source modification.
- Cosine distance measurement and semantic drift classification.
- Interactive CLI demo execution.
"""

from __future__ import annotations

import ast
import logging
import socket
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_app_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_app_dir))

import repomap_cache
from repomap_cache import (
    _FEATURE_TESTS,
    NAME_BUCKETS,
    OPERATOR_TYPES,
    ASTFeatureExtractor,
    CacheTier,
    DriftVerdict,
    EmbeddingDriftAuditor,
    TwoTierRepomapCache,
    ValkeyL2Client,
    _tally_node_features,
    decode_resp_response,
    encode_resp_command,
    read_resp_reply,
    run_cli,
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

# Two modules with no function or class name in common. The 8-feature vector scored them
# PRESERVED (distance 0.0079): its two logarithmic terms outweighed every density.
UNRELATED_STATS = '''
def mean_of(samples):
    """Return the arithmetic mean."""
    if not samples:
        return 0.0
    total = 0.0
    for sample in samples:
        total += sample * weight(sample)
    return total / len(samples)


def weight(sample):
    """Return a weight in the range 0 to 1."""
    if sample < 0 or sample > 100:
        return 0.0
    return sample / 100
'''

UNRELATED_PATHS = '''
class PathFilter:
    """Keep the paths that match a suffix."""

    def __init__(self, suffix):
        self.suffix = suffix

    def keep(self, names):
        """Return the names that end with the suffix."""
        kept = []
        for name in names:
            if name.endswith(self.suffix) and name not in kept:
                kept.append(name.lower())
        return kept
'''

EQUALITY_CHECK = '''
def is_ready(state, expected):
    """Return True when the state is the expected state."""
    if state.phase == expected.phase:
        return state.count == expected.count
    return False
'''


@pytest.fixture
def refused_port() -> Iterator[int]:
    """Yield a loopback port that refuses connections: it is bound, and nothing listens."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        yield holder.getsockname()[1]


class _FakeValkey:
    """A RESP server double: one in-memory socket that answers PING, GET, SET and DEL.

    A command named in `fail` raises `OSError`, as a reset connection does.
    """

    def __init__(self, fail: frozenset[str] = frozenset(), ping: bytes = b"+PONG\r\n") -> None:
        self.fail = fail
        self.ping = ping
        self.data: dict[str, str] = {}
        self.commands: list[str] = []
        self._reply = b""

    def connect(self, _address: tuple[str, int], timeout: float | None = None) -> _FakeValkey:
        """Stand in for `socket.create_connection`."""
        return self

    def __enter__(self) -> _FakeValkey:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def sendall(self, payload: bytes) -> None:
        """Decode one RESP command and prepare its reply."""
        args = [part.decode() for part in payload.split(b"\r\n")[2::2]]
        self.commands.append(args[0])
        if args[0] in self.fail:
            raise OSError(f"connection reset during {args[0]}")
        self._reply = self._answer(args)

    def _answer(self, args: list[str]) -> bytes:
        if args[0] == "PING":
            return self.ping
        if args[0] == "SET":
            self.data[args[1]] = args[2]
            return b"+OK\r\n"
        if args[0] == "DEL":
            return b":%d\r\n" % (self.data.pop(args[1], None) is not None)
        value = self.data.get(args[1])
        if value is None:
            return b"$-1\r\n"
        encoded = value.encode()
        return b"$%d\r\n%s\r\n" % (len(encoded), encoded)

    def recv(self, _size: int) -> bytes:
        """Return the prepared reply once, then end of stream."""
        reply, self._reply = self._reply, b""
        return reply


def _fake_client(monkeypatch: pytest.MonkeyPatch, server: _FakeValkey) -> ValkeyL2Client:
    monkeypatch.setattr(repomap_cache.socket, "create_connection", server.connect)
    return ValkeyL2Client()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


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


def test_valkey_client_in_memory_fallback(refused_port: int) -> None:
    """Verify in-memory fallback store when Valkey daemon is not present."""
    client = ValkeyL2Client(host="127.0.0.1", port=refused_port)
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
    """Verify the vector joins 6 shape, 29 operator and 64 name-bucket features."""
    tree = ast.parse(SAMPLE_PY_V1)
    embedding = ASTFeatureExtractor.compute_embedding(tree, 6)
    assert len(embedding) == len(_FEATURE_TESTS) + len(OPERATOR_TYPES) + NAME_BUCKETS == 99
    # Verify unit vector normalization ||v|| ~= 1.0
    norm_sq = sum(x * x for x in embedding)
    assert 0.99 <= norm_sq <= 1.01


def test_two_tier_caching_flow(refused_port: int) -> None:
    """Verify L1 memory cache hit, L2 fallback hit, and computed miss with no server."""
    client = ValkeyL2Client(host="127.0.0.1", port=refused_port)
    cache = TwoTierRepomapCache(valkey_client=client)
    p = Path("test_sample.py")

    # Pass 1: Miss computed
    rec1, tier1 = cache.get_repomap(p, source_code=SAMPLE_PY_V1)
    assert (tier1, len(rec1.symbols)) == (CacheTier.MISS_COMPUTED, 1)

    # Pass 2: L1 memory hit
    rec2, tier2 = cache.get_repomap(p, source_code=SAMPLE_PY_V1)
    assert (tier2, rec2.content_sha256) == (CacheTier.L1_MEMORY, rec1.content_sha256)

    # Pass 3: Evict L1. No server answered, so the in-process dict serves L2 and says so.
    cache._l1_cache.clear()
    rec3, tier3 = cache.get_repomap(p, source_code=SAMPLE_PY_V1)
    assert (tier3, rec3.content_sha256) == (CacheTier.L2_FALLBACK, rec1.content_sha256)
    assert (cache.stats["l2_hits"], cache.stats["l2_fallback_hits"]) == (0, 1)


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
    assert (
        report.cosine_distance <= 0.05,
        report.verdict,
        report.added_symbols,
        report.retained_symbols,
    ) == (
        True,
        DriftVerdict.PRESERVED,
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


class _SplitSocket:
    """A conformant server writing the bulk-string header and payload as separate writes."""

    def __init__(self, payload: bytes, drop_tail: bool = False) -> None:
        self.chunks = [b"$%d\r\n" % len(payload), payload[:100], payload[100:] + b"\r\n"]
        if drop_tail:
            self.chunks.pop()

    def recv(self, _size: int) -> bytes:
        """Return the next scripted segment, then end of stream."""
        return self.chunks.pop(0) if self.chunks else b""


def test_a_reply_split_across_writes_is_read_whole() -> None:
    """One `recv` is not a reply.

    TCP delivers a stream, and a conformant server may write the `$<len>` header and the
    payload separately — which Valkey does above roughly 16 KB. The parser sliced the
    buffer anyway and returned a short string with a plausible consumed count and no error,
    so a large cached value came back silently clipped.
    """
    payload = b"x" * 5000
    assert read_resp_reply(_SplitSocket(payload)) == payload.decode()


def test_an_incomplete_bulk_string_asks_for_more_rather_than_guessing() -> None:
    """`0` consumed is the signal a read loop needs; a short string is a silent lie."""
    assert decode_resp_response(b"$5\r\nhel") == (None, 0)
    assert decode_resp_response(b"$5\r\nhello\r\n") == ("hello", 11)


def test_tally_node_features_tallies_correctly() -> None:
    """Verify that _tally_node_features accurately increments matching feature buckets."""
    counts = dict.fromkeys(_FEATURE_TESTS, 0)
    func_node = ast.parse("def hello(): pass").body[0]
    class_node = ast.parse("class Hello: pass").body[0]
    branch_node = ast.parse("if True: pass").body[0]

    _tally_node_features(func_node, counts)
    _tally_node_features(class_node, counts)
    _tally_node_features(branch_node, counts)

    assert (counts["functions"], counts["classes"], counts["branches"]) == (1, 1, 1)


def test_run_cli_with_files(tmp_path: Path) -> None:
    """Verify run_cli execution with file and refactored arguments."""
    f1 = tmp_path / "v1.py"
    f2 = tmp_path / "v2.py"
    f1.write_text(SAMPLE_PY_V1, encoding="utf-8")
    f2.write_text(SAMPLE_PY_V2, encoding="utf-8")

    res = run_cli(["--file", str(f1), "--refactored", str(f2)])
    assert res == 0


def test_unreachable_valkey_warns_once_and_reports_the_fallback(
    refused_port: int, caplog: pytest.LogCaptureFixture
) -> None:
    """With no server, the client says so once, keeps the reason, and a hit has its own tier."""
    client = ValkeyL2Client(host="127.0.0.1", port=refused_port)
    cache = TwoTierRepomapCache(valkey_client=client)
    p = Path("test_sample.py")
    reads = (client.get("absent:1"), client.get("absent:2"))
    cache.get_repomap(p, source_code=SAMPLE_PY_V1)
    cache._l1_cache.clear()
    _, tier = cache.get_repomap(p, source_code=SAMPLE_PY_V1)

    assert (reads, tier, client.is_connected) == ((None, None), CacheTier.L2_FALLBACK, False)
    assert "PING" in (client.fallback_reason or "")
    assert len(_warnings(caplog)) == 1


def test_a_ping_reply_other_than_pong_selects_the_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A server that needs a password answers PING with an error; that is not a connection."""
    server = _FakeValkey(ping=b"-NOAUTH Authentication required.\r\n")
    client = _fake_client(monkeypatch, server)

    assert (client.is_connected, server.commands) == (False, ["PING"])
    assert "NOAUTH" in (client.fallback_reason or "")
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize(
    ("failing", "expected", "sent"),
    [("GET", None, ["PING", "SET", "GET"]), ("SET", "value", ["PING", "SET"])],
)
def test_a_failed_command_falls_back_and_reports_it(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing: str,
    expected: str | None,
    sent: list[str],
) -> None:
    """A GET or SET that raises moves the client to the in-process dict, visibly and once.

    The client stays on the dict after the first failure, so reads and writes do not split
    between two stores, and the tier of every later L2 hit is `L2_FALLBACK`.
    """
    server = _FakeValkey(fail=frozenset({failing}))
    client = _fake_client(monkeypatch, server)
    assert client.is_connected is True

    stored = client.set("key", "value")
    value = client.get("key")
    client.get("key")

    assert (stored, value, client.is_connected) == (True, expected, False)
    assert server.commands == sent
    assert failing in (client.fallback_reason or "")
    assert len(_warnings(caplog)) == 1


def test_a_live_server_hit_is_reported_as_valkey(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A nil GET reply is a cache miss, not a fallback; a server hit is `L2_VALKEY`."""
    server = _FakeValkey()
    client = _fake_client(monkeypatch, server)
    cache = TwoTierRepomapCache(valkey_client=client)
    p = Path("test_sample.py")

    _, first = cache.get_repomap(p, source_code=SAMPLE_PY_V1)
    cache._l1_cache.clear()
    _, second = cache.get_repomap(p, source_code=SAMPLE_PY_V1)
    deleted = client.delete(next(iter(server.data)))

    assert (first, second, deleted) == (CacheTier.MISS_COMPUTED, CacheTier.L2_VALKEY, True)
    assert (client.is_connected, client.fallback_reason, server.data) == (True, None, {})
    assert (cache.stats["l2_hits"], cache.stats["l2_fallback_hits"]) == (1, 0)
    assert _warnings(caplog) == []


def test_a_peer_that_closes_mid_reply_is_a_transport_error() -> None:
    """A partial reply is not a nil reply; it must select the fallback, not read as a miss."""
    with pytest.raises(ConnectionError):
        read_resp_reply(_SplitSocket(b"x" * 5000, drop_tail=True))


def test_unrelated_modules_are_not_preserved(refused_port: int) -> None:
    """Two modules that share no function or class name must not score PRESERVED."""
    cache = TwoTierRepomapCache(ValkeyL2Client(port=refused_port))
    stats, _ = cache.get_repomap(Path("stats.py"), source_code=UNRELATED_STATS)
    paths, _ = cache.get_repomap(Path("paths.py"), source_code=UNRELATED_PATHS)

    report = EmbeddingDriftAuditor.calculate_drift(stats, paths)
    assert report.retained_symbols == []
    assert report.verdict != DriftVerdict.PRESERVED
    assert report.cosine_distance > 0.05


def test_an_equality_flip_changes_the_distance(refused_port: int) -> None:
    """`==` and `!=` are different operators, so the vector must tell them apart."""
    cache = TwoTierRepomapCache(ValkeyL2Client(port=refused_port))
    before, _ = cache.get_repomap(Path("ready.py"), source_code=EQUALITY_CHECK)
    after, _ = cache.get_repomap(Path("ready.py"), source_code=EQUALITY_CHECK.replace("==", "!="))

    assert EmbeddingDriftAuditor.calculate_drift(before, after).cosine_distance > 0.0


def test_a_read_refreshes_lru_order(refused_port: int) -> None:
    """Reading the oldest key makes it the newest, so the next insert evicts the other one."""
    cache = TwoTierRepomapCache(ValkeyL2Client(port=refused_port), l1_capacity=2)
    oldest, middle, newest = "a = 1\n", "b = 2\n", "c = 3\n"
    for source in (oldest, middle):
        cache.get_repomap(Path("m.py"), source_code=source)

    _, read_tier = cache.get_repomap(Path("m.py"), source_code=oldest)
    cache.get_repomap(Path("m.py"), source_code=newest)
    _, kept_tier = cache.get_repomap(Path("m.py"), source_code=oldest)
    _, evicted_tier = cache.get_repomap(Path("m.py"), source_code=middle)

    assert (read_tier, kept_tier) == (CacheTier.L1_MEMORY, CacheTier.L1_MEMORY)
    assert evicted_tier != CacheTier.L1_MEMORY
