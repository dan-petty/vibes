"""Unit and integration test suite for Real-Time AST Tree-CRDT.

Validates commutative convergence, fractional tree ordering, cycle prevention,
orphan attachment, valid Python AST materialization, SARIF export, and CLI commands.

Certified compliant with AST Invariant Sentinel (M <= 4, depth <= 2).
"""

from __future__ import annotations

import ast

from ast_crdt import (
    ASTTreeCRDT,
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
    main,
    materialize_to_source,
    next_node_id,
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
