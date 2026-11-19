# Observation 46: Closed-Loop PR Review Thread Synchronization & Sequential Resolution

> **Project**: `vibes` & `devops-cli`  
> **Environment**: GitHub GraphQL API v4, Python 3.12+, Python AST, OASIS SARIF 2.1.0, Branch Protection  
> **Classification**: Agent Orchestration, Review Thread Lifecycle, Semantic Symbol Anchoring, Closed-Loop Verification  
> **Related**: [Observation 03 (devops-cli)](../devops-cli/03-autonomous-project-governance.md), [Observation 08 (devops-cli)](../devops-cli/08-closed-loop-feedback-inversion-and-autonomous-quality-elevation.md), [Observation 45 (Systems)](./45-sycophantic-compliance-and-mechanical-refusal-oracles.md), [Pattern: Closed-Loop PR Review Thread Synchronization](../../patterns/closed-loop-pr-review-thread-synchronization.md)  
> **Key Metric**: Not measured. The reference tool reads review threads from a JSON file and prints GraphQL mutations; no grounding or false-resolution rate was recorded.  
> **TLDR**: A stale line number can point at the wrong code after later commits; a thread synchronizer should map each thread to current code, verify the fix mechanically, and only then reply and resolve via GraphQL. The reference tool finds the function of a complexity or docstring thread by its review-time name and fails the thread when that function is gone; its nesting and sanitization checks still read the line or the whole file.  
> **ELI:7b**: When a teacher leaves a note on line 40 of your paper and you add new sentences, line 40 moves to line 50. If a robot only looks at line 40, it gets confused. Instead, the robot must look at the paragraph name, check that the mistake was actually fixed, and check off the teacher's note.  

---

## 1. Executive Context & Baseline

Pull request review discussions form the collaborative backbone of enterprise software engineering. Modern continuous integration platforms, review bots, and human reviewers leave structured review comments anchored to specific diff hunks and line numbers in GitHub Pull Requests.

Under standard GitHub Branch Protection rules, repositories frequently enforce:
- **"Require conversation resolution before merging"**: All review threads must be resolved before a branch can be merged into `main`.
- **Automated bot reviews**: Review bots (such as `pr_triage_bot.py` or SonarQube) audit cyclomatic complexity, nesting depth, zero-trust sanitization, and typing, generating inline comments.

In autonomous multi-agent environments, this lifecycle breaks down in practice due to a dual failure mode:
1. **Review Thread Amnesia**: Agents push valid commits that address the review feedback, but completely abandon the open review threads on GitHub. The PR remains unmergeable, forcing human developers to manually inspect and click "Resolve conversation" on dozens of threads.
2. **False or Premature Resolution**: When prompted to resolve open threads, stochastic agents often emit conversational replies (*"I have resolved this issue"*) and blindly resolve the thread without verifying whether the underlying code defect was actually repaired or if a regression was introduced.

Compounding both failures is the flaw of **ephemeral line coordinates**: lines drift with every commit. GitHub itself maps each thread to the latest commit: a `PullRequestReviewThread` has `line` (current) and `originalLine`, and `isOutdated` is true when newer changes touched the commented lines. The drift hazard is an agent that keeps a line number from an earlier fetch, or that reads `originalLine` against the current file.

---

## 2. The Observed Phenomenon: Line Drift and Review Desynchronization

Line-based review tracking can fail when a file is refactored. The sequence below shows the intended design; Section 4 lists what the reference tool actually does:

```mermaid
sequenceDiagram
    autonumber
    participant Reviewer as Review Bot / Human
    participant GitHub as GitHub PR Thread API
    participant Agent as Autonomous AI Agent
    participant Disk as Local Workspace AST
    participant Resolver as PR Thread Synchronizer

    Reviewer->>GitHub: Post comment on line 42 ("Complexity M=12 exceeds cap 10")
    GitHub-->>Agent: Fetch open PR review threads
    Agent->>Disk: Refactor function (adds imports, moves code to lines 55-70)
    Agent->>Disk: Commit & push new revision
    Note over Disk,Agent: Line 42 is now a docstring or blank line!
    Resolver->>Disk: Parse review-time AST: line 42 is in 'calculate_metrics'
    Disk-->>Resolver: Current AST: 'calculate_metrics' is now at lines 55-70
    Resolver->>Disk: Audit function 'calculate_metrics' with AST Oracle
    Disk-->>Resolver: Verified: M=4 <= 10 (FIX_VERIFIED)
    Resolver->>GitHub: One GraphQL request: addReply("Verified fix in symbol") + resolveReviewThread
    GitHub-->>Reviewer: Thread resolved with verified proof
```

### The Three Pathologies of Line-Based Thread Tracking

1. **Drift Desynchronization**: A comment left on line 42 of revision `HEAD~2` points to line 58 after imports are added. When an agent queries line 42 of the active file, it inspects an unrelated AST node and either reports a false positive or attempts to refactor the wrong code.
2. **Ghost Resolution**: When agents are tasked with resolving threads, they can execute GraphQL `resolveReviewThread` mutations based purely on conversational intent without verifying the file on disk. No audit measured how often this happens.
3. **Orphaned Thread Abandonment**: When a file is renamed, decomposed into multiple modules, or deleted, threads anchored to the old path become "orphans." Agents fail to recognize that the path no longer exists and loop indefinitely trying to find the missing file.

---

## 3. Evaluation Status

No evaluation was run. This repository holds no record of 25 pull requests or 184 review threads, and no harness produces grounding, false-resolution, dangling-thread, review-time, speed or SARIF-conformance figures for `tools/pr_thread_sync.py`.

---

## 4. Invariant Enforcement & Architectural Mechanics

[`tools/pr_thread_sync.py`](../../tools/pr_thread_sync.py) implements a four-stage pipeline. It reads review threads from a JSON file (`--input`) and does not call GitHub: with `--resolve-verified` it prints the GraphQL mutations for a person or another tool to send.

```mermaid
flowchart TD
    subgraph Ingestion["1. Ingestion & Normalization"]
        G[GitHub GraphQL / REST JSON] --> P[parse_review_threads]
        P --> RT[ReviewThread Objects]
    end

    subgraph Grounding["2. Semantic AST Grounding"]
        RT --> LOC[locate_ast_symbol]
        LOC --> AST[Python AST Symbol Table]
        AST --> SYM["Enclosing Symbol: 'compute_metrics' (lines 55-70)"]
    end

    subgraph Audit["3. Mechanical Verification Oracle"]
        SYM --> CLAS[classify_comment_rule: CC001 / ND001 / SEC001]
        CLAS --> VFY[verify_thread_resolution]
        VFY --> DEC{Defect Present on Disk?}
        DEC -->|No| VF[Status: FIX_VERIFIED]
        DEC -->|Yes| AR[Status: ACTION_REQUIRED]
        DEC -->|Missing File| OP[Status: ORPHANED_PATH]
    end

    subgraph Resolution["4. Sequential Mutations & Telemetry"]
        VF --> GQL["Printed GraphQL Mutation:\naddPullRequestReviewThreadReply + resolveReviewThread"]
        AR --> SARIF["OASIS SARIF 2.1.0 Telemetry Output"]
        OP --> SARIF
    end
```

### 1. Robust AST Symbol Locating
`parse_review_threads` keeps the review-time symbol name of each thread: the node's `symbolName` field, else the definitions that enclose the commented line in the first comment's `diffHunk` (GitHub ends that hunk at the commented line), else the hunk header's context text (`symbol_from_diff_hunk`). The hunk header can name a different definition than the one that holds the line: the class of a method, or the function above the line. With `--base-root`, a directory that holds the base version of the files (the version that the review was made on, for example a git worktree of the reviewed commit), `resolve_review_symbol` first looks in that version. It takes the dotted path of the definitions that enclose the review-time line (`originalLine`, else `line`), for example `B.run` for a line in method `run` of class `B`. The name from the diff hunk is used only when that lookup fails, and a `symbolName` field always wins. `locate_ast_symbol` parses the current file with Python's `ast` module and finds the definition of that name (`find_symbol_by_name`), so a function that moved is still found. A dotted name such as `Calc.compute` is resolved part by part; a bare name takes the outermost, then earliest, match. One `--base-root` serves all threads, so a thread from an earlier review commit can get the definition at the wrong line.

A thread with no symbol name falls back to the most specific function, method, or class node that encloses `thread.line` (or `originalLine` when `line` is missing). That fallback does not correct drift: if the code moved, it names whatever symbol now sits at that line. If the line falls outside any declaration it reports `module level`; a non-Python file is `file scope`.

### 2. Heuristic Rule Classification
`classify_comment_rule` parses the comment bodies using regex dispatch patterns to classify the complaint into deterministic categories:
- `CC001`: Cyclomatic complexity violations (the check uses $M > 10$).
- `ND001`: Nesting depth violations (the check uses $\text{depth} > 5$).
- `SEC001`: Zero-trust sanitization leaks (RFC 1918 private IPs or plaintext credentials).
- `DOC001`: Documentation or missing docstring complaints.
- `TYP001`: Static typing or annotation defects.
- `GENERAL`: Unclassified comments requiring manual review.

### 3. Mechanical Fix Verification Oracles
For each thread, `verify_thread_resolution` evaluates whether the defect has actually been cured on disk:
- For `CC001`, it scores the function that the thread names with radon's McCabe cyclomatic complexity (`metric_reference.cyclomatic_complexity`). If $M \le 10$, it certifies resolution. If the file has no function of that name, or the thread names no symbol and no function encloses its line, the thread stays `ACTION_REQUIRED`.
- For `ND001`, it divides the indentation of the single target line by four. If that depth is $\le 5$, it certifies resolution. A line number outside the file also passes.
- For `SEC001`, it scans the file content for RFC 1918 IP addresses (`_RFC1918_PATTERN`). If clean, it certifies resolution. A complaint about a secret, token or credential passes when the file has no RFC 1918 address: credentials are not checked.
- For `DOC001`, it requires a non-empty docstring on the definition that the thread names, or on the one that encloses the line when the thread names none. A missing definition, a missing docstring, or a file that does not parse as Python (for example Markdown) keeps the thread `ACTION_REQUIRED`. The check shows that a docstring exists; it cannot show that a typo or a link was fixed.
- If the target file was deleted or moved, it designates the thread as `ORPHANED_PATH`.

### 4. Sequential Resolution Mutations & SARIF Telemetry
When a thread is certified as `FIX_VERIFIED`, `generate_resolution_mutation` constructs one GraphQL document with both mutations. GraphQL runs top-level mutation fields one after another (GraphQL specification, "Mutation" execution), but it does not roll back the reply if the resolve fails, so the pair is not atomic:
```graphql
mutation {
  addPullRequestReviewThreadReply(input: {
    pullRequestReviewThreadId: "PRRT_kwDO12345",
    body: "Verified: radon measures cyclomatic complexity M=4 for calculate_metrics, at or below the limit of 10. In the base version, calculate_metrics measured M=12."
  }) { comment { id } }
  resolveReviewThread(input: { threadId: "PRRT_kwDO12345" }) {
    thread { isResolved }
  }
}
```
The reply states only what the tool measured. A `CC001` reply gives the symbol, its radon complexity now and in the base version, and the limit; without `--base-root` it says that the old complexity was not measured. It does not say how the code changed. An `ND001` reply gives the measured depth of the line, and a `SEC001` reply says that secrets, tokens and credentials were not checked. Separately, `export_sarif` produces OASIS SARIF 2.1.0 output for ingestion into GitHub Security and CI quality dashboards.

---

## 5. Actionable Guidance & Antidotes

1. **Never Resolve Threads Solely on Prompt Assertions**: An agent must never execute a `resolveReviewThread` GraphQL mutation unless an automated mechanical verification oracle confirms that the defect no longer exists on disk.
2. **Anchor Feedback to AST Symbols, Not Drifting Lines**: Treat line numbers as provisional coordinates at the time of review. Map them immediately to their enclosing AST symbol before storing, tracking, or remediating review items.
3. **Send Reply and Resolve in One Request**: Put `addPullRequestReviewThreadReply` and `resolveReviewThread` in one GraphQL document so they run in order in one round trip. This is not a transaction: if the resolve fails, the reply stays, so check both results.
4. **Export SARIF Telemetry for Continuous Visibility**: Pipe review thread analysis into SARIF artifacts (`--sarif review_sync.sarif`) so that open review obligations are visible in GitHub Advanced Security and CI summaries.
5. **Enforce Clean PR Exit Gates**: Wire `pr_thread_sync --exit-code` into PR pre-merge checks. It fails only when a thread is `ACTION_REQUIRED`. Orphaned threads do not fail it, and the weaker checks above can pass a thread whose complaint is not fixed (`ND001` reads one line, `SEC001` does not check credentials, `DOC001` does not check typos or links), so it cannot guarantee that no actionable comment remains.
