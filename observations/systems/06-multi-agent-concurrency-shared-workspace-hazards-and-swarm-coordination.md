# Observation 06 (Systems): Multi-Agent Concurrency, Shared Workspace Hazards & Swarm Coordination

> **Project**: `devops-cli` & `vibes`
> **Topic**: Concurrency Hazards, Working Tree Isolation, SQLite Cache Contention, and Swarm Merge Serialization
> **Key Metric**: Separate worktrees remove shared-working-tree collisions; separate data directories remove coverage-file unlink races; oldest-first PR handling reduces rebase churn (no measured rates)
> **TLDR**: Eliminate working tree clobbering and database corruption in multi-agent swarms using isolated git worktrees, scoped data tiers, and FIFO PR queues.
> **ELI:7b**: Give each AI helper its own copy of the project folder so they don't overwrite each other's files while working at the same time.

---

## 1. Executive Context & The Concurrency Fallacy

As autonomous AI engineering assistants transition from single-agent interactive pairs into multi-agent swarms (such as parallel subagents, autonomous review bots, background refactoring daemons, and benchmark runners), engineering organizations encounter the **Multi-Agent Concurrency Fallacy**:

> *"If one AI agent can refactor a subsystem in 10 minutes, five simultaneous agents will refactor five subsystems in two minutes."*

In practice, running multiple autonomous agents simultaneously against an uncoordinated, shared codebase produces catastrophic thrashing:
- Throughput can fall instead of scaling with the number of agents.
- Agents enter recursive debugging loops diagnosing phantom errors caused by peer agents.
- Local build artifacts, coverage databases, and git indices suffer silent data corruption.
- Remote pull request queues lock up in cascading rebase storms and merge starvation.

This observation synthesizes the concrete failure modes observed when running multiple agents on the same codebase, dissects their architectural root causes, and details the deterministic coordination fabric required to achieve high-throughput multi-agent concurrency.

---

## 2. The 6 Multi-Agent Concurrency Hazards

```mermaid
flowchart TD
    subgraph Swarm["Uncoordinated Multi-Agent Swarm"]
        A1["Agent 1 (Feature Dev)"]
        A2["Agent 2 (Review / Invariants)"]
        A3["Agent 3 (Docs & Benchmarks)"]
    end

    subgraph Hazards["Shared Workspace Hazards"]
        H1["Hazard 1: Dirty Working Tree Clobbering"]
        H2["Hazard 2: SQLite Cache Corruption (no such table)"]
        H3["Hazard 3: Epistemic Context Drift (Stale AST)"]
        H4["Hazard 4: Cascading Rebase Live-Lock"]
        H5["Hazard 5: Local GPU VRAM Thrashing & OOM"]
        H6["Hazard 6: Host PID Zombie Leaks"]
    end

    subgraph Collapse["Systemic Collapse"]
        C1["Flaky CI Failures & False Regressions"]
        C2["Corrupt Build & Coverage Databases"]
        C3["Thundering Herd Merge Starvation"]
        C4["Workstation Resource Starvation"]
    end

    A1 -.->|Writes Shared Workdir| H1
    A2 -.->|Concurrent Test Runner| H2
    A1 -.->|Pushes Breaking API| H3
    A2 -.->|Reads Stale Signatures| H3
    A1 -.->|Pushes PR #101| H4
    A3 -.->|Pushes PR #102| H4
    A1 -.->|Unthrottled Ollama Calls| H5
    A2 -.->|Unthrottled Embeddings| H5
    A3 -.->|Uncontained Popen Leaks| H6

    H1 --> C1
    H2 --> C2
    H3 --> C1
    H4 --> C3
    H5 --> C4
    H6 --> C4
```

### Hazard 1: Unisolated Working Tree Clobbering (The Dirty Index Trap)
When two agents execute against the same root checkout (`/workspaces/<repo>`):
- **Mid-Edit Syntax Contamination**: Agent 1 initiates a multi-file refactoring, writing an incomplete AST to `module_a.py`. Concurrently, Agent 2 runs `pytest` or `devops ci` to verify an unrelated fix in `module_b.py`. Agent 2's test suite crashes on `SyntaxError in module_a.py`.
- **Hallucinated Regression Loops**: Agent 2 falsely concludes that its own modification caused the syntax error, reverts its correct change, authors unnecessary defensive workarounds, and wastes significant token context debugging code it never touched.
- **Git Index Contention**: Simultaneous `git add` or `git commit` commands trigger `.git/index.lock` collisions, causing subprocess aborts across both agent sessions.

### Hazard 2: SQLite Build Artifact & Coverage Database Corruption
Modern test runners, typecheckers, and coverage engines rely on SQLite databases for state tracking (e.g. `.coverage.*` produced by `pytest-cov`, `.mypy_cache`, and ephemeral benchmark indices):
- **Database Unlink Races**: A common pattern in CI runner scripts is cleaning up stale coverage files (`_clean_coverage_artifacts()`) prior to running a test suite. When Agent 1 invokes CI while Agent 2's background test suite is actively executing, Agent 1's cleanup unlinks the `.data/.coverage.*` SQLite file while Agent 2 holds an open file descriptor.
- **Table Missing Panic**: An already-open connection keeps writing to the unlinked file. When a process next opens that path, SQLite creates a bare, uninitialized database file, triggering `sqlite3.OperationalError: no such table: file` or `no such table: line_bits`. Both CI pipelines fail despite zero source code defects.

### Hazard 3: Epistemic Context Drift (Stale AST Hallucination)
- When Agent 1 modifies a function signature or removes an obsolete parameter in `src/...`, it commits and pushes to its branch or merges to the mainline.
- Agent 2, operating within a long-running multi-turn session, retains the pre-refactor symbol definitions within its cached context window.
- Agent 2 generates new feature code invoking the old signature, introducing regressions that fail typechecking (`mypy`) and contract validation during integration.

### Hazard 4: Cascading Rebase Live-Lock & PR Starvation
- When five agents create five distinct PRs branched off `main` at timestamp $T_0$:
  - Agent 1 merges PR #1 at $T_1$.
  - PRs #2, #3, #4, and #5 immediately become out of date.
  - If all four remaining agents simultaneously pull `main` and execute `git rebase main`:
    - Agent 2 finishes its rebase and merges PR #2 at $T_2$.
    - The remaining agents (3, 4, 5) find their newly rebased branches invalidated once again.
- In high-velocity swarms, this produces **rebase live-lock**: agents spend most of their tokens rebasing and re-running test suites against rapidly moving base branches, while older PRs suffer complete starvation.

### Hazard 5: Inference Host & Local GPU VRAM Exhaustion
- Unlike remote cloud APIs with massive serverless concurrency, local inference runtimes (such as Ollama or vLLM running on developer workstations or private homelab nodes) have strict hardware boundaries.
- When three agents simultaneously issue unthrottled streaming generation and embedding requests:
  - Local GPU VRAM saturates, triggering memory thrashing and model eviction cycles (unloading the reasoning model to load the embedding model, then reloading the reasoning model).
  - Per-token inference latency rises sharply, triggering HTTP `ReadTimeout` exceptions in agent clients.

### Hazard 6: Process Tree Leaks & Host Resource Starvation
- Autonomous agents execute background daemons, watchers, and test runners via subprocesses.
- Calling standard `proc.kill()` upon task completion terminates only the parent shell, leaving grandchild processes adopted by PID 1.
- Across multiple concurrent agents, orphaned processes accumulate, consuming CPU, memory, and file handles until the entire host becomes unresponsive.

---

## 3. The Countermeasure: Hardened Multi-Agent Coordination Fabric

To enable deterministic, collision-free concurrency across multiple agents, we engineered the **Multi-Agent Coordination Fabric**:

```mermaid
flowchart TD
    subgraph SwarmControl["Swarm Coordinator & Task Allocator"]
        Goal["Epic Goal / Backlog Issues"] --> Allocator["Task Allocator (Orthogonal Domain Slicing)"]
        Allocator --> FIFO["FIFO PR Shepherding Queue"]
    end

    subgraph AgentWorkspaces["Isolated Agent Workspaces (Git Worktrees)"]
        FIFO --> W1["Agent 1 Worktree (worktrees/agent-1/)"]
        FIFO --> W2["Agent 2 Worktree (worktrees/agent-2/)"]
    end

    subgraph IsolatedData["Partitioned Data & Process Tiers"]
        W1 --> D1[".data/agent/agent-1/ (Isolated Coverage DB)"]
        W2 --> D2[".data/agent/agent-2/ (Isolated Coverage DB)"]
        W1 --> PG1["POSIX Process Group (start_new_session=True)"]
        W2 --> PG2["POSIX Process Group (start_new_session=True)"]
    end

    subgraph SharedCoordination["Shared Coordination Tier (Thread-Safe)"]
        W1 <--> Valkey["Shared Valkey L2 Cache & Mutexes"]
        W2 <--> Valkey
        Valkey --> Limiter["Token-Bucket Rate Limiter (run_gh / Ollama)"]
    end

    subgraph Gatekeeper["Deterministic Merge Gatekeeper"]
        W1 --> Gate["devops ci quality gates"]
        W2 --> Gate
        Gate --> Sentinel["Architectural Invariant Sentinel (M <= 10, Depth <= 5)"]
        Sentinel --> Merge["Atomic Mainline Merge (Oldest PR First)"]
    end
```

### 1. Mandatory Git Worktree & Workspace Branch Isolation
Autonomous agents must never share an active working tree. Each agent must operate within a dedicated POSIX git worktree or isolated container volume:
```bash
# Provision an isolated worktree for Agent 1
git worktree add ../worktrees/task-128 feat/issue-128-streaming-reasoning

# Agent 1 operates exclusively within its isolated worktree
cd ../worktrees/task-128
```
- **Zero Syntax Cross-Contamination**: Transient file edits in worktree A are completely invisible to worktree B.
- **Independent Git State**: Each worktree maintains its own `HEAD`, index, and staging area, so `git add` in one worktree does not take the index lock of another. Refs, the object store, `git stash` and the repository config stay shared, so `git fetch`, `git gc` and stash operations still contend across worktrees.

### 2. Ephemeral Data Directory Partitioning (`.data/agent/<agent-id>`)
Build artifacts, coverage measurements, and test logs must be strictly partitioned by agent identity:
- Give each agent its own data root, for example `.data/agent/<agent-id>`.
- In October 2026, devops-cli read its data root from `DEVOPS_CLI_DATA_DIR` (default `./.data`), and its `AGENTS.md` told agents to keep their artifacts under one `<data_dir>/agent/` folder. It had no per-agent subfolder, and `pytest-cov` wrote `.data/.coverage*`.
- `_clean_coverage_artifacts()` (`src/devops_cli/commands/ci.py`) deletes `.coverage*` files in the project root and in `.data/`, not in a per-agent folder. It returns early when `PYTEST_CURRENT_TEST` is set, and from v0.2.23 (2026-09-28) it skips files that an active coverage worker owns.

### 3. Centralized Resource Arbitration via Valkey & Token-Bucket Limiters
Shared external resources are coordinated through a centralized, thread-safe coordination layer:
- **Shared Valkey L2 Cache**: Content-addressed AST parse results and embeddings are cached globally by SHA-256 digest, preventing redundant computation across agents.
- **Client-Side Token-Bucket Rate Limiter**: In devops-cli, GitHub calls go through `run_gh()` and its `GitHubRateLimiter`, a client-side token bucket with quota guards and a read cache, to reduce HTTP 429 responses and quota exhaustion.
- **Model Prewarming & Keep-Alive Pinning**: High-priority reasoning models are prewarmed and pinned in VRAM (`DEFAULT_AI_PREWARM_KEEP_ALIVE = "1h"`) to reduce evictions during concurrent agent workloads.

### 4. Strict FIFO Pull Request Shepherding & Dry-Rebase Verification
To eliminate rebase live-locks and PR starvation:
- **Strict Chronological Priority**: Process pull requests oldest first (lowest PR number first). In October 2026, devops-cli's `AGENTS.md` gave agents this rule as an instruction. No merge queue or check stopped a newer PR from merging first, and the rule is no longer in `AGENTS.md`.
- **Dry-Rebase Verification**: Check for conflicts before a rebase. devops-cli's roadmap listed `devops pr check-readiness --dry-rebase` as an open item in October 2026, and the option was never built. `git merge-tree --write-tree` (Git 2.38 and later) reports conflicts without touching the working tree, the index or the reflog.

### 5. Deterministic Architectural Invariant Sentinel
The centralized merge gatekeeper enforces immutable quality standards:
- In October 2026, devops-cli blocked nesting depth above 5 at pre-commit and required coverage of at least 90%. Cyclomatic complexity above 10 was reported, not blocked (240 functions breached it).
- In November 2026, Ruff enforces `max-complexity = 10` and `max-nested-blocks = 4`, and coverage must reach 85% overall and 90% on changed lines.
- Universal egress sanitization (RFC 5737 documentation IPs, loopback, zero secret leakage).

---

## 4. Empirical Impact & Benchmark Metrics

No benchmark in vibes or devops-cli compares an uncoordinated swarm with this fabric, so this observation reports no rates. Each mechanism above removes one named hazard:

| Hazard | Mechanism | Expected Effect |
|---|---|---|
| **Working tree collisions** | One git worktree per agent | Edits in one tree are not visible in another |
| **Coverage file unlink races** | One data directory per worktree | Cleanup in one tree cannot delete another tree's coverage files |
| **Rebase churn** | Oldest-first PR handling | Fewer rebases of the same branch |
| **Local inference overload** | Concurrency limits and model keep-alive | Fewer model evictions and timeouts |
| **Orphaned processes** | Process groups and `killpg` | Descendants still in the group are killed |

---

## 5. Architectural Invariant Rules for Multi-Agent Systems

1. **Rule of Spatial Isolation**: Multiple agents MUST NEVER operate within the same physical git working tree. Always provision dedicated git worktrees (`git worktree add`) or isolated container volumes.
2. **Rule of State Partitioning**: All ephemeral data stores, coverage databases, and log files MUST reside in dedicated per-agent subdirectories (`.data/agent/<agent-id>`).
3. **Rule of Resource Pacing**: External APIs and local GPU inference endpoints MUST be gated by token-bucket rate limiters and concurrency semaphores.
4. **Rule of FIFO Queue Governance**: Pull requests MUST be shepherded in strict chronological order from oldest to newest to prevent branch starvation and rebase live-locks.
5. **Rule of the Mechanical Oracle**: No agent may bypass the centralized invariant gatekeeper. Merge readiness requires 100% passing status across all deterministic quality gates.

