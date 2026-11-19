# Observation 35: 3-Way AST Semantic Reconciliation and Worktree Collision Arbitration

> **Project**: Autonomous Git Swarm Coordination & Semantic Conflict Resolution  
> **Environment**: Multi-agent concurrent worktrees, Git 3-way merge drivers, Python AST symbol graphs  
> **Classification**: Software Architecture, Git Internals, Multi-Agent Concurrency, AST Engineering  
> **Related**: [Observation 06](./06-multi-agent-concurrency-shared-workspace-hazards-and-swarm-coordination.md), [Observation 09](../devops-cli/09-mechanical-ast-rewriting-and-automated-refactoring-convergence.md), [Pattern: 3-Way AST Semantic Reconciliation](../../patterns/3-way-ast-semantic-reconciliation.md)
> **TLDR**: Reconcile concurrent agent git collisions using commutative AST symbol graphs and 3-way import set unions rather than raw line diffs.
> **ELI:7b**: Merge code changes from multiple AIs by comparing functions and classes instead of lines, so separate additions do not conflict. The merged file loses its comments.

---

## 1. Executive Context & Baseline

When scaling autonomous AI coding swarms across parallel development streams, isolated git worktrees ([`worktree-swarm-arbiter`](../../examples/worktree-swarm-arbiter/)) prevent concurrent processes from colliding on lockfiles (`.git/index.lock`). However, when individual agent topic branches reconcile back into an integration branch or release candidate, they must pass through Git's 3-way merge engine (`Base`, `Ours`, `Theirs`).

Git's merge strategies (`ort`, `recursive`) merge file contents with the built-in `text` merge driver, which compares contiguous lines. (`diff3` is a conflict-marker style, `merge.conflictStyle`, not a driver.) While optimal for unstructured text or prose, line-oriented diffing breaks down severely when applied to structured source code manipulated by concurrent autonomous agents.

---

## 2. The Observed Breakdown Modes

Line-based merges report some conflicts that come from line adjacency rather than true semantic collisions. This repository has no measurement of how often that happens. Four forms occur:

### 2.1 Commutative Function Append Clashes
In autonomous development, subagents frequently implement distinct helper functions or tests at the bottom of a module. If Subagent A appends `def helper_alpha()` and Subagent B appends `def helper_beta()` at the same file offset, line-based merge flags a fatal conflict:

```text
<<<<<<< HEAD
def helper_alpha():
    return "alpha"
=======
def helper_beta():
    return "beta"
>>>>>>> feat/subagent-b
```

In Python, two plain module-level `def` statements with different names and no decorators **commute**: you can swap them without a change in behavior. This is not true in general. Module code runs from top to bottom, so decorators, import-time calls and redefinitions depend on order. Textual diff tools lack AST awareness and cannot recognize that both functions can be preserved simultaneously.

### 2.2 Disjoint Class Method Additions
When two subagents extend the same class—one adding `def pause(self)` and the other adding `def resume(self)`—line-based tools attempt to reconcile the class body textually and can emit a conflict block inside the class definition.

### 2.3 Import Set Cascades
Concurrent subagents introduce new library imports at the top of a file. When both append imports to line 12, Git cannot determine whether the lines should be interleaved, unioned, or rejected, requiring human or agent manual intervention.

### 2.4 Syntax Invalidation via Conflict Markers
When line-based merge conflicts are left unresolved in working copies, tools write raw marker strings (`<<<<<<<`, `=======`, `>>>>>>>`). If an agent attempts to run test suites or compile code before detecting these markers, the Python interpreter crashes with unhandled `SyntaxError`, derailing automated verification pipelines.

---

## 3. The Architectural Solution: 3-Way AST Semantic Reconciliation

To remove false conflicts for disjoint additions and to emit only parseable output, we designed the **Autonomous 3-Way AST Semantic Reconciler** ([`examples/ast-semantic-reconciler/`](../../examples/ast-semantic-reconciler/)). It writes its output with `ast.unparse` and then parses the result again with `ast.parse`. If that parse fails, it reports a collision and writes no code. The result loses every comment and the original formatting, and the imports move to the top in sorted order.

```mermaid
flowchart TD
    subgraph "Three-Way AST Sources"
        B["Base (Ancestor AST)"]
        O["Ours (Local Branch AST)"]
        T["Theirs (Remote Branch AST)"]
    end

    subgraph "Symbol Partitioning & Hashing"
        B --> PB["Top-Level AST Decomposition"]
        O --> PO["Top-Level AST Decomposition"]
        T --> PT["Top-Level AST Decomposition"]
        PB & PO & PT --> SH["Deterministic SHA-256 Hashing"]
    end

    subgraph "Arbitration Engine"
        SH --> IU["3-Way Import Union: (O | T) - (B - (O & T))"]
        SH --> CF["Commutative Disjoint Function Merge"]
        SH --> CM["Recursive Class Member Arbitration"]
    end

    subgraph "Verification & Telemetry"
        IU & CF & CM --> VAL{"Collision Detected?"}
        VAL -- "No" --> CODE["Synthesize Valid AST (ast.unparse)"]
        VAL -- "Yes" --> DIAG["Emit Collision Diagnostics (SARIF 2.1.0)"]
    end
```

### 3.1 Mathematical Formulation of Commutative AST Merge

Let a source module $M$ be represented as a tuple of its leading docstring $D$, import set $I$, and top-level symbol map $S$:

$$M = \langle D, I, S \rangle$$

Where each symbol $s \in S$ has an identifier $k$ and structural hash $H(s)$. For a 3-way merge with base $M_B$, local branch $M_O$, and remote branch $M_T$:

1. **Import Set Reconciliation**:
   $$I_{\text{merged}} = (I_O \cup I_T) \setminus (I_B \setminus (I_O \cap I_T))$$
   All valid imports added by either branch are retained, deduplicated, and sorted deterministically.

2. **Commutative Symbol Arbitration**:
   For any symbol identifier $k \in \operatorname{dom}(S_O) \cup \operatorname{dom}(S_T)$:
   $$\operatorname{Arbitrate}(k) = \begin{cases}
   S_T(k) & \text{if } H(S_O(k)) = H(S_B(k)) \\
   S_O(k) & \text{if } H(S_T(k)) = H(S_B(k)) \lor H(S_O(k)) = H(S_T(k)) \\
   \operatorname{ReconcileClass}(S_B(k), S_O(k), S_T(k)) & \text{if } \operatorname{Kind}(k) = \text{Class} \\
   \text{Collision}(k) & \text{otherwise}
   \end{cases}$$

If both branches add disjoint symbols ($k \notin \operatorname{dom}(S_B)$ and $k$ is present in only one branch), both are included commutatively, converting would-be textual conflicts into clean merges.

$\operatorname{ReconcileClass}$ applies the same rule to each member of the class body: the docstring, attributes, methods, nested classes and other statements. The class header (decorators, bases, keywords) is arbitrated as one more symbol. The docstring $D$ of the module is arbitrated like a symbol too, so the merged module holds it one time.

---

## 4. Evaluation Status

No benchmark against Git was run. This repository holds no 120-branch testbed and no data for false-conflict rates, collision rates, output validity or latency. The 26 tests in `test_reconciler.py` run the reconciler on fixed three-way inputs: disjoint functions, imports, class members and docstrings, true collisions, and assembled code that does not parse.

---

## 5. Architectural Invariants & Reference Implementation

The complete executable reference tool is located at [`examples/ast-semantic-reconciler/`](../../examples/ast-semantic-reconciler/):
- **Executable Core**: [`ast_reconciler.py`](../../examples/ast-semantic-reconciler/ast_reconciler.py) certified compliant under AST Invariant Sentinel `--preset strict` ($M \le 6$, depth $\le 3$, parameters $\le 4$).
- **Test Suite**: [`test_reconciler.py`](../../examples/ast-semantic-reconciler/test_reconciler.py) with 26 tests. They cover 97% of the statements in `ast_reconciler.py` (`coverage run --include=examples/ast-semantic-reconciler/ast_reconciler.py -m pytest examples/ast-semantic-reconciler`, run on 2026-11-19).
- **Git Driver Protocol**: Integrates as a native git custom merge driver via `.gitattributes` (`*.py merge=ast-reconciler`).
