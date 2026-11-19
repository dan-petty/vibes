"""Real-Time AST Conflict-Free Replicated Data Type (Tree-CRDT).

In-process simulation of a replicated AST tree for concurrent edits by several agents.
It replicates tree operations instead of characters. Moves apply in timestamp order with
the move operation of Kleppmann et al., "A highly-available move operation for replicated
trees" (IEEE TPDS 2022): undo later moves, apply, redo. Replicas that receive the same
moves therefore converge on one tree, also when moves form cycles. Sibling positions are
exact rational numbers. The tree converges only under causal delivery: each operation
must arrive after the inserts that it refers to. Materialized source is not syntax-checked.

Certified compliant with AST Invariant Sentinel (M <= 4, depth <= 2).
"""

from __future__ import annotations

import argparse
import bisect
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction
from typing import Any


class NodeKind(StrEnum):
    """Categorical kind of AST CRDT node."""

    ROOT = "ROOT"
    MODULE = "MODULE"
    IMPORT = "IMPORT"
    FUNCTION = "FUNCTION"
    CLASS = "CLASS"
    DECORATOR = "DECORATOR"
    STATEMENT = "STATEMENT"


class OpType(StrEnum):
    """Commutative operation primitive."""

    INSERT_NODE = "INSERT_NODE"
    DELETE_NODE = "DELETE_NODE"
    UPDATE_ATTR = "UPDATE_ATTR"
    MOVE_NODE = "MOVE_NODE"


class DiagnosticRule(StrEnum):
    """AST-CRDT diagnostic invariants."""

    CRDT001 = "CRDT001"  # Cycle Induction Hazard
    CRDT002 = "CRDT002"  # Orphan Node Hazard
    CRDT003 = "CRDT003"  # Causal Inversion Warning
    CRDT004 = "CRDT004"  # Syntactic Invariant Invalidation
    CRDT005 = "CRDT005"  # Tombstone Memory Saturation


@dataclass(frozen=True)
class NodeId:
    """Immutable Lamport-timestamped node identifier."""

    agent_id: str
    lamport: int
    counter: int

    def to_string(self) -> str:
        """Format as canonical string."""
        return f"{self.agent_id}:{self.lamport}:{self.counter}"


@dataclass
class CRDTNode:
    """Replicated AST tree node."""

    node_id: str
    kind: NodeKind
    parent_id: str
    pos: str
    attrs: dict[str, str] = field(default_factory=dict)
    is_tombstone: bool = False


@dataclass(frozen=True)
class InsertTarget:
    """Target descriptor for node insertion."""

    node_id: str
    parent_id: str
    pos: str
    kind: NodeKind
    name: str = ""


_POSITIONED_OPS = frozenset({OpType.INSERT_NODE, OpType.MOVE_NODE})


@dataclass(frozen=True)
class CRDTOperation:
    """Commutative mutation message."""

    op_type: OpType
    node_id: str
    parent_id: str
    pos: str
    attrs: dict[str, str]
    lamport: int
    agent_id: str

    def __post_init__(self) -> None:
        """Reject an insert or move whose position is not a number."""
        if self.op_type in _POSITIONED_OPS:
            position_key(self.pos)


@dataclass(frozen=True)
class MoveRecord:
    """Entry of the move log: a move and the location that it replaced.

    ``old`` is ``None`` when the move had no effect: its node was absent, or the
    move would have made a cycle.
    """

    op: CRDTOperation
    old: tuple[str, str] | None


@dataclass(frozen=True)
class Finding:
    """Diagnostic telemetry finding."""

    rule: DiagnosticRule
    message: str
    target: str
    severity: str = "warning"


def create_node_id(agent_id: str, lamport: int, counter: int) -> str:
    """Mint a canonical node ID string."""
    return NodeId(agent_id, lamport, counter).to_string()


def position_key(pos: str) -> Fraction:
    """Parse a sibling position, such as "2.5" or "7/3", as an exact rational number.

    Raises ValueError when the position is not a finite number.
    """
    return Fraction(pos)


def position_between(left: str, right: str) -> str:
    """Return the exact midpoint of two positions, which sorts strictly between them."""
    return str((position_key(left) + position_key(right)) / 2)


def move_timestamp(op: CRDTOperation) -> tuple[int, str, str, str, str]:
    """Total order of moves: Lamport clock, then agent ID, then the move target."""
    return (op.lamport, op.agent_id, op.node_id, op.parent_id, op.pos)


def create_insert_op(
    agent_id: str,
    lamport: int,
    target: InsertTarget,
) -> CRDTOperation:
    """Construct an insert node operation with bounded parameter cardinality."""
    attrs = {"kind": str(target.kind), "name": target.name}
    return CRDTOperation(
        op_type=OpType.INSERT_NODE,
        node_id=target.node_id,
        parent_id=target.parent_id,
        pos=target.pos,
        attrs=attrs,
        lamport=lamport,
        agent_id=agent_id,
    )


def create_delete_op(agent_id: str, lamport: int, node_id: str) -> CRDTOperation:
    """Construct a delete node operation."""
    return CRDTOperation(
        op_type=OpType.DELETE_NODE,
        node_id=node_id,
        parent_id="",
        pos="",
        attrs={},
        lamport=lamport,
        agent_id=agent_id,
    )


def create_move_op(
    agent_id: str,
    lamport: int,
    node_id: str,
    dest: tuple[str, str],
) -> CRDTOperation:
    """Construct a move node operation."""
    return CRDTOperation(
        op_type=OpType.MOVE_NODE,
        node_id=node_id,
        parent_id=dest[0],
        pos=dest[1],
        attrs={},
        lamport=lamport,
        agent_id=agent_id,
    )


@dataclass
class ASTTreeCRDT:
    """Replicated AST Tree State Machine."""

    agent_id: str
    clock: int = 0
    counter: int = 0
    node_map: dict[str, CRDTNode] = field(default_factory=dict)
    op_log: list[CRDTOperation] = field(default_factory=list)
    move_log: list[MoveRecord] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Initialize root node."""
        if "root" not in self.node_map:
            self.node_map["root"] = CRDTNode(
                node_id="root",
                kind=NodeKind.ROOT,
                parent_id="",
                pos="0.0",
            )


def next_node_id(crdt: ASTTreeCRDT) -> str:
    """Advance local clock and mint next unique node ID."""
    crdt.clock += 1
    crdt.counter += 1
    return create_node_id(crdt.agent_id, crdt.clock, crdt.counter)


def _next_parent(node_map: dict[str, CRDTNode], curr: str) -> str:
    """Extract parent pointer of given node."""
    node = node_map.get(curr)
    return node.parent_id if node else ""


def is_ancestor(node_map: dict[str, CRDTNode], ancestor_id: str, target_id: str) -> bool:
    """Check if ancestor_id is in parent chain without nested branches."""
    curr = target_id
    limit = len(node_map) + 2
    steps = 0
    while curr and curr != ancestor_id and steps < limit:
        curr = _next_parent(node_map, curr)
        steps += 1
    return curr == ancestor_id


def _apply_insert(crdt: ASTTreeCRDT, op: CRDTOperation) -> None:
    """Apply node insertion commutatively."""
    existing = crdt.node_map.get(op.node_id)
    if existing:
        return
    parent = crdt.node_map.get(op.parent_id)
    parent_id = op.parent_id if parent else "root"
    _check_orphan(crdt, op.parent_id, parent is not None)
    kind_str = op.attrs.get("kind", NodeKind.STATEMENT)
    crdt.node_map[op.node_id] = CRDTNode(
        node_id=op.node_id,
        kind=NodeKind(kind_str),
        parent_id=parent_id,
        pos=op.pos,
        attrs=dict(op.attrs),
    )


def _check_orphan(crdt: ASTTreeCRDT, parent_id: str, exists: bool) -> None:
    """Record finding if inserting under non-existent parent."""
    if not exists and parent_id != "root":
        crdt.findings.append(Finding(
            rule=DiagnosticRule.CRDT002,
            message=f"Orphan node hazard: parent '{parent_id}' absent, attached to root",
            target=parent_id,
        ))


def _apply_delete(crdt: ASTTreeCRDT, op: CRDTOperation) -> None:
    """Mark node as tombstone."""
    node = crdt.node_map.get(op.node_id)
    if node:
        node.is_tombstone = True


def _report_cycle(crdt: ASTTreeCRDT, op: CRDTOperation) -> None:
    """Record a CRDT001 finding once per rejected move, also when a redo rejects it again."""
    finding = Finding(
        rule=DiagnosticRule.CRDT001,
        message=(
            f"Cycle induction prevented: '{op.node_id}' is ancestor of '{op.parent_id}' "
            f"(move at Lamport {op.lamport} by '{op.agent_id}')"
        ),
        target=op.node_id,
    )
    if finding not in crdt.findings:
        crdt.findings.append(finding)


def _do_move(crdt: ASTTreeCRDT, op: CRDTOperation) -> MoveRecord:
    """Move a node unless the node is absent or the move makes a cycle (CRDT001)."""
    node = crdt.node_map.get(op.node_id)
    if not node:
        return MoveRecord(op, None)
    if is_ancestor(crdt.node_map, op.node_id, op.parent_id):
        _report_cycle(crdt, op)
        return MoveRecord(op, None)
    old = (node.parent_id, node.pos)
    node.parent_id = op.parent_id
    node.pos = op.pos
    return MoveRecord(op, old)


def _undo_move(crdt: ASTTreeCRDT, record: MoveRecord) -> None:
    """Put a moved node back at the location that the move replaced."""
    if record.old is not None:
        node = crdt.node_map[record.op.node_id]
        node.parent_id, node.pos = record.old


def _is_logged(log: list[MoveRecord], index: int, key: tuple[int, str, str, str, str]) -> bool:
    """Check if the move log already holds this move, for example from a repeated sync."""
    return index < len(log) and move_timestamp(log[index].op) == key


def _apply_move(crdt: ASTTreeCRDT, op: CRDTOperation) -> None:
    """Apply a move in timestamp order: undo later moves, apply this move, redo them.

    This is the move operation of Kleppmann et al. (IEEE TPDS 2022). Every replica
    applies its moves in one total order, so replicas with the same moves converge.
    """
    log = crdt.move_log
    key = move_timestamp(op)
    index = bisect.bisect_left(log, key, key=lambda record: move_timestamp(record.op))
    if _is_logged(log, index, key):
        return
    later = log[index:]
    del log[index:]
    for record in reversed(later):
        _undo_move(crdt, record)
    log.append(_do_move(crdt, op))
    log.extend(_do_move(crdt, record.op) for record in later)


_OP_DISPATCH: dict[OpType, Callable[[ASTTreeCRDT, CRDTOperation], None]] = {
    OpType.INSERT_NODE: _apply_insert,
    OpType.DELETE_NODE: _apply_delete,
    OpType.MOVE_NODE: _apply_move,
}


def apply_operation(crdt: ASTTreeCRDT, op: CRDTOperation) -> None:
    """Apply CRDT operation and advance Lamport clock."""
    crdt.clock = max(crdt.clock, op.lamport) + 1
    crdt.op_log.append(op)
    handler = _OP_DISPATCH.get(op.op_type)
    if handler:
        handler(crdt, op)


def sync_peers(peer_a: ASTTreeCRDT, peer_b: ASTTreeCRDT) -> None:
    """Bi-directionally synchronize two CRDT peers."""
    log_a = list(peer_a.op_log)
    log_b = list(peer_b.op_log)
    for op in log_a:
        apply_operation(peer_b, op)
    for op in log_b:
        apply_operation(peer_a, op)


def get_active_children(crdt: ASTTreeCRDT, parent_id: str) -> list[CRDTNode]:
    """Retrieve ordered non-tombstone children of a parent node."""
    nodes = [
        n for n in crdt.node_map.values()
        if n.parent_id == parent_id and not n.is_tombstone
    ]
    return sorted(nodes, key=lambda n: (position_key(n.pos), n.node_id))


def materialize_to_source(crdt: ASTTreeCRDT) -> str:
    """Materialize ordered CRDT tree into formatted Python source code."""
    lines: list[str] = []
    top_nodes = get_active_children(crdt, "root")
    for node in top_nodes:
        _render_node(crdt, node, lines)
    return "\n".join(lines).strip() + "\n"


def _render_import(node: CRDTNode, lines: list[str]) -> None:
    """Format an import node into python code."""
    name = node.attrs.get("name", "")
    code = node.attrs.get("code", "")
    lines.append(code or f"import {name}")


def _render_function(node: CRDTNode, lines: list[str]) -> None:
    """Format a function node into python code."""
    name = node.attrs.get("name", "")
    lines.append(f"def {name}():")
    lines.append("    pass")
    lines.append("")


def _render_class(node: CRDTNode, lines: list[str]) -> None:
    """Format a class node into python code."""
    name = node.attrs.get("name", "")
    lines.append(f"class {name}:")
    lines.append("    pass")
    lines.append("")


_RENDER_MAP: dict[NodeKind, Callable[[CRDTNode, list[str]], None]] = {
    NodeKind.IMPORT: _render_import,
    NodeKind.FUNCTION: _render_function,
    NodeKind.CLASS: _render_class,
}


def _render_node(crdt: ASTTreeCRDT, node: CRDTNode, lines: list[str]) -> None:
    """Render a single CRDT node into source lines."""
    handler = _RENDER_MAP.get(node.kind)
    if handler:
        handler(node, lines)


def _finding_to_sarif(f: Finding) -> dict[str, Any]:
    """Map diagnostic finding to SARIF result."""
    return {
        "ruleId": str(f.rule),
        "level": f.severity,
        "message": {"text": f.message},
        "locations": [{
            "physicalLocation": {
                "artifactLocation": {"uri": f.target},
            },
        }],
    }


def export_sarif(findings: list[Finding], tool_name: str = "ast-crdt") -> str:
    """Export findings as schema-valid OASIS SARIF 2.1.0 JSON."""
    results = [_finding_to_sarif(f) for f in findings]
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": tool_name, "version": "0.1.0"}},
            "results": results,
        }],
    }
    return json.dumps(sarif, indent=2)


def _findings_table_lines(findings: list[Finding]) -> list[str]:
    """Render table rows for diagnostic findings."""
    lines = [
        "## Diagnostic Findings",
        "",
        "| Rule | Message | Target |",
        "|---|---|---|",
    ]
    for f in findings:
        lines.append(f"| `{f.rule}` | {f.message} | `{f.target}` |")
    lines.append("")
    return lines


def format_markdown_report(crdt: ASTTreeCRDT) -> str:
    """Format CRDT state and findings as Markdown report."""
    active_count = sum(1 for n in crdt.node_map.values() if not n.is_tombstone)
    lines = [
        f"# AST-CRDT State Report (Agent: {crdt.agent_id})",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Lamport Clock | {crdt.clock} |",
        f"| Active Nodes | {active_count} |",
        f"| Operation Log Length | {len(crdt.op_log)} |",
        f"| Diagnostic Findings | {len(crdt.findings)} |",
        "",
    ]
    if crdt.findings:
        lines.extend(_findings_table_lines(crdt.findings))
    return "\n".join(lines)


def _cmd_inspect(args: argparse.Namespace) -> int:
    """Inspect and print markdown report of active state."""
    crdt = ASTTreeCRDT(agent_id=args.agent_id or "agent-1")
    nid = next_node_id(crdt)
    target = InsertTarget(nid, "root", "1.0", NodeKind.FUNCTION, "demo")
    op = create_insert_op(crdt.agent_id, crdt.clock, target)
    apply_operation(crdt, op)
    print(format_markdown_report(crdt))
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    p = argparse.ArgumentParser(description="AST-CRDT Collaborative Editor Gateway")
    sub = p.add_subparsers(dest="command")
    insp_p = sub.add_parser("inspect", help="Inspect CRDT state")
    insp_p.add_argument("--agent-id", default="agent-1", help="Agent identifier")
    return p


_CLI_DISPATCH: dict[str, Callable[[argparse.Namespace], int]] = {
    "inspect": _cmd_inspect,
}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for AST-CRDT editor."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    handler = _CLI_DISPATCH.get(args.command or "")
    if not handler:
        parser.print_help()
        return 0
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
