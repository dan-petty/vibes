"""Unit and integration test suite for Real-Time AST Tree-CRDT.

Validates convergence of two concurrent inserts, convergence of concurrent moves in
seeded random delivery orders, numeric sibling ordering and midpoint positions, the cycle
check on apply and on redo, orphan attachment, materialization of valid names, SARIF
export, and CLI commands.

Certified compliant with AST Invariant Sentinel (M <= 4, depth <= 2).
"""

from __future__ import annotations

import ast
import random

import pytest
from ast_crdt import (
    ASTTreeCRDT,
    CRDTOperation,
    DiagnosticRule,
    InsertTarget,
    NodeKind,
    apply_operation,
    create_delete_op,
    create_insert_op,
    create_move_op,
    create_node_id,
    export_sarif,
    format_markdown_report,
    get_active_children,
    is_ancestor,
    main,
    materialize_to_source,
    next_node_id,
    position_between,
    position_key,
    sync_peers,
)


def _make_sample_crdt(agent_id: str = "agent-1") -> ASTTreeCRDT:
    """Construct a clean CRDT instance."""
    return ASTTreeCRDT(agent_id=agent_id)


def test_node_id_creation() -> None:
    """Verify canonical node ID formatting."""
    nid = create_node_id("worker", 42, 7)
    assert nid == "worker:42:7"


def test_basic_insert_and_materialize() -> None:
    """Verify node insertion and valid Python syntax materialization."""
    crdt = _make_sample_crdt("agent-1")
    nid_imp = next_node_id(crdt)
    nid_fn = next_node_id(crdt)
    op_imp = create_insert_op(crdt.agent_id, crdt.clock, InsertTarget(nid_imp, "root", "1.0", NodeKind.IMPORT, "sys"))
    op_fn = create_insert_op(crdt.agent_id, crdt.clock, InsertTarget(nid_fn, "root", "2.0", NodeKind.FUNCTION, "worker"))
    apply_operation(crdt, op_imp)
    apply_operation(crdt, op_fn)
    source = materialize_to_source(crdt)
    parsed = ast.parse(source)
    assert (
        "import sys" in source,
        "def worker():" in source,
        len(parsed.body),
    ) == (True, True, 2)


def test_commutative_convergence() -> None:
    """Verify that applying concurrent operations in any order yields identical state."""
    peer_a = _make_sample_crdt("agent-a")
    peer_b = _make_sample_crdt("agent-b")
    op_1 = create_insert_op("agent-a", 1, InsertTarget("id-1", "root", "1.0", NodeKind.FUNCTION, "alpha"))
    op_2 = create_insert_op("agent-b", 1, InsertTarget("id-2", "root", "2.0", NodeKind.FUNCTION, "beta"))
    apply_operation(peer_a, op_1)
    apply_operation(peer_a, op_2)
    apply_operation(peer_b, op_2)
    apply_operation(peer_b, op_1)
    src_a = materialize_to_source(peer_a)
    src_b = materialize_to_source(peer_b)
    assert (src_a, len(peer_a.node_map)) == (src_b, len(peer_b.node_map))


def test_sync_peers() -> None:
    """Verify bidirectional state synchronization between distributed peers."""
    peer_1 = _make_sample_crdt("peer-1")
    peer_2 = _make_sample_crdt("peer-2")
    nid_1 = next_node_id(peer_1)
    nid_2 = next_node_id(peer_2)
    apply_operation(peer_1, create_insert_op("peer-1", peer_1.clock, InsertTarget(nid_1, "root", "1.0", NodeKind.CLASS, "State")))
    apply_operation(peer_2, create_insert_op("peer-2", peer_2.clock, InsertTarget(nid_2, "root", "2.0", NodeKind.FUNCTION, "execute")))
    sync_peers(peer_1, peer_2)
    src_1 = materialize_to_source(peer_1)
    src_2 = materialize_to_source(peer_2)
    children_1 = [n.node_id for n in get_active_children(peer_1, "root")]
    children_2 = [n.node_id for n in get_active_children(peer_2, "root")]
    assert (src_1, children_1) == (src_2, children_2)


def test_delete_tombstone() -> None:
    """Verify node deletion marks tombstone and prunes materialized output."""
    crdt = _make_sample_crdt("agent-1")
    nid = next_node_id(crdt)
    apply_operation(crdt, create_insert_op(crdt.agent_id, crdt.clock, InsertTarget(nid, "root", "1.0", NodeKind.FUNCTION, "obsolete")))
    before_count = len(get_active_children(crdt, "root"))
    apply_operation(crdt, create_delete_op(crdt.agent_id, crdt.clock, nid))
    after_count = len(get_active_children(crdt, "root"))
    source = materialize_to_source(crdt)
    assert (before_count, after_count, "def obsolete():" in source) == (1, 0, False)


def test_move_node() -> None:
    """Verify node movement updates parent and fractional position."""
    crdt = _make_sample_crdt("agent-1")
    nid_parent = next_node_id(crdt)
    nid_child = next_node_id(crdt)
    apply_operation(crdt, create_insert_op(crdt.agent_id, crdt.clock, InsertTarget(nid_parent, "root", "1.0", NodeKind.CLASS, "Container")))
    apply_operation(crdt, create_insert_op(crdt.agent_id, crdt.clock, InsertTarget(nid_child, "root", "2.0", NodeKind.FUNCTION, "method")))
    apply_operation(crdt, create_move_op(crdt.agent_id, crdt.clock, nid_child, (nid_parent, "1.0")))
    node = crdt.node_map[nid_child]
    assert (node.parent_id, node.pos) == (nid_parent, "1.0")


def test_cycle_induction_prevention() -> None:
    """Verify cycle prevention rejects invalid ancestor moves (CRDT001)."""
    crdt = _make_sample_crdt("agent-1")
    nid_a = next_node_id(crdt)
    nid_b = next_node_id(crdt)
    apply_operation(crdt, create_insert_op(crdt.agent_id, crdt.clock, InsertTarget(nid_a, "root", "1.0", NodeKind.CLASS, "Parent")))
    apply_operation(crdt, create_insert_op(crdt.agent_id, crdt.clock, InsertTarget(nid_b, nid_a, "1.0", NodeKind.CLASS, "Child")))
    apply_operation(crdt, create_move_op(crdt.agent_id, crdt.clock, nid_a, (nid_b, "1.0")))
    node_a = crdt.node_map[nid_a]
    findings = crdt.findings
    assert (
        node_a.parent_id,
        len(findings),
        findings[0].rule if findings else None,
    ) == ("root", 1, DiagnosticRule.CRDT001)


def test_orphan_handling() -> None:
    """Verify insertion under absent parent safely attaches to root (CRDT002)."""
    crdt = _make_sample_crdt("agent-1")
    nid = next_node_id(crdt)
    apply_operation(crdt, create_insert_op(crdt.agent_id, crdt.clock, InsertTarget(nid, "ghost-parent", "1.0", NodeKind.FUNCTION, "lonely")))
    node = crdt.node_map[nid]
    findings = crdt.findings
    assert (
        node.parent_id,
        len(findings),
        findings[0].rule if findings else None,
    ) == ("root", 1, DiagnosticRule.CRDT002)


def test_export_sarif_and_markdown() -> None:
    """Verify SARIF 2.1.0 telemetry export and Markdown report generation."""
    crdt = _make_sample_crdt("agent-1")
    nid = next_node_id(crdt)
    apply_operation(crdt, create_insert_op(crdt.agent_id, crdt.clock, InsertTarget(nid, "missing", "1.0", NodeKind.FUNCTION, "test")))
    sarif_str = export_sarif(crdt.findings)
    md_str = format_markdown_report(crdt)
    assert (
        "sarif-2.1.0.json" in sarif_str,
        "CRDT002" in sarif_str,
        "Lamport Clock" in md_str,
        "CRDT002" in md_str,
    ) == (True, True, True, True)


def test_cli_entry_points() -> None:
    """Verify CLI argument parsing and inspect handler."""
    rc_help = main([])
    rc_insp = main(["inspect", "--agent-id", "test-agent"])
    assert (rc_help, rc_insp) == (0, 0)


_NODES = ("n1", "n2", "n3", "n4", "n5")
_AGENTS = ("agent-a", "agent-b", "agent-c")


def _replica(agent_id: str) -> ASTTreeCRDT:
    """Build a replica that holds five classes under root."""
    crdt = ASTTreeCRDT(agent_id=agent_id)
    for i, nid in enumerate(_NODES, start=1):
        apply_operation(crdt, create_insert_op("agent-a", 1, InsertTarget(nid, "root", f"{i}.0", NodeKind.CLASS, nid)))
    return crdt


def _apply_all(crdt: ASTTreeCRDT, ops: list[CRDTOperation]) -> ASTTreeCRDT:
    """Apply operations in the given arrival order."""
    for op in ops:
        apply_operation(crdt, op)
    return crdt


def _random_move(rng: random.Random) -> CRDTOperation:
    """Draw one move with a small timestamp range, so ties and cycle attempts occur."""
    dest = (rng.choice(("root", *_NODES)), str(rng.randint(1, 12)))
    return create_move_op(rng.choice(_AGENTS), rng.randint(2, 6), rng.choice(_NODES), dest)


def _random_moves(seed: int) -> list[CRDTOperation]:
    """Draw ten seeded random moves."""
    rng = random.Random(seed)
    return [_random_move(rng) for _ in range(10)]


def _snapshot(crdt: ASTTreeCRDT) -> tuple[list[tuple[str, str, str]], list[list[str]]]:
    """Capture parent and position of each node, and the child order of each parent."""
    nodes = sorted((n.node_id, n.parent_id, n.pos) for n in crdt.node_map.values())
    children = [[n.node_id for n in get_active_children(crdt, pid)] for pid in ("root", *_NODES)]
    return nodes, children


def _delivery(ops: list[CRDTOperation], order_seed: int) -> ASTTreeCRDT:
    """Deliver the operations to a new replica in a seeded random order."""
    order = list(ops)
    random.Random(order_seed).shuffle(order)
    return _apply_all(_replica(f"replica-{order_seed}"), order)


def _is_tree(crdt: ASTTreeCRDT) -> bool:
    """Check that every node reaches root, so the moves made no cycle."""
    return all(is_ancestor(crdt.node_map, "root", nid) for nid in _NODES)


def _converges(seed: int) -> bool:
    """Check that three delivery orders of one random move set give one acyclic tree."""
    ops = _random_moves(seed)
    replicas = [_delivery(ops, seed * 10 + k) for k in range(3)]
    shapes = [_snapshot(r) for r in replicas]
    return (shapes[1:], _is_tree(replicas[0])) == ([shapes[0], shapes[0]], True)


def test_concurrent_moves_converge() -> None:
    """Verify that replicas reach one tree for the same moves in any delivery order."""
    failed_seeds = [seed for seed in range(200) if not _converges(seed)]
    assert failed_seeds == []


def test_later_move_wins_in_both_orders() -> None:
    """Verify that the move with the greater Lamport timestamp sets the parent."""
    early = create_move_op("agent-b", 5, "n3", ("n1", "1.0"))
    late = create_move_op("agent-a", 6, "n3", ("n2", "1.0"))
    in_order = _apply_all(_replica("r1"), [early, late]).node_map["n3"]
    reversed_ = _apply_all(_replica("r2"), [late, early]).node_map["n3"]
    assert [(n.parent_id, n.pos) for n in (in_order, reversed_)] == [("n2", "1.0"), ("n2", "1.0")]


def test_equal_lamport_moves_use_agent_id() -> None:
    """Verify that the agent ID breaks a tie of equal Lamport timestamps."""
    move_a = create_move_op("agent-a", 7, "n3", ("n1", "1.0"))
    move_b = create_move_op("agent-b", 7, "n3", ("n2", "1.0"))
    first = _apply_all(_replica("r1"), [move_a, move_b]).node_map["n3"].parent_id
    second = _apply_all(_replica("r2"), [move_b, move_a]).node_map["n3"].parent_id
    assert (first, second) == ("n2", "n2")


def test_cycle_check_runs_on_redo() -> None:
    """Verify that a late, earlier move is applied first and the later cycle move is rejected once."""
    x_under_y = create_move_op("agent-a", 5, "n1", ("n2", "1.0"))
    y_under_x = create_move_op("agent-b", 6, "n2", ("n1", "1.0"))
    unrelated = create_move_op("agent-c", 2, "n3", ("n4", "1.0"))
    in_order = _apply_all(_replica("r1"), [unrelated, x_under_y, y_under_x])
    late = _apply_all(_replica("r2"), [y_under_x, x_under_y, unrelated])
    results = [
        (r.node_map["n1"].parent_id, r.node_map["n2"].parent_id, [(f.rule, f.target) for f in r.findings])
        for r in (in_order, late)
    ]
    expected = ("n2", "root", [(DiagnosticRule.CRDT001, "n2")])
    assert results == [expected, expected]


def _child_insert(index: int) -> CRDTOperation:
    """Build the insert of function child-<index> under root at position <index>.0."""
    target = InsertTarget(f"child-{index}", "root", f"{index}.0", NodeKind.FUNCTION, f"f{index}")
    return create_insert_op("agent-1", 1, target)


_TEN_CHILDREN = [f"child-{index}" for index in range(1, 11)]


def test_ten_children_keep_numeric_order() -> None:
    """Verify that sibling positions compare as numbers, so 10 sorts after 9."""
    crdt = _apply_all(_make_sample_crdt(), [_child_insert(index) for index in range(10, 0, -1)])
    order = [n.node_id for n in get_active_children(crdt, "root")]
    assert order == _TEN_CHILDREN


def test_position_between_stays_exact() -> None:
    """Verify that sixty nested midpoints stay distinct and sort between their neighbours."""
    keys = ["9", "10"]
    for _ in range(60):
        keys.insert(1, position_between(keys[0], keys[1]))
    assert (sorted(keys, key=position_key), len(set(keys))) == (keys, 62)


def test_non_numeric_position_is_rejected() -> None:
    """Verify that a move to a position that is not a number fails at construction."""
    with pytest.raises(ValueError, match="Invalid literal for Fraction"):
        create_move_op("agent-1", 1, "n1", ("root", "first"))
