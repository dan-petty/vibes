# Observation 21: Milestone Horizon Expansion & Autonomous Scope Cascades

> **Project**: `devops-cli`
> **Topic**: Milestone Horizon Inflation Trap, Scope Live-Locks, Autonomous Agent Triage Cascades, and Release Air-Lock Governance
> **Key Metric**: Milestone 36 (`v0.2.23`) expanded to 196 items, issues and pull requests counted together (167 closed, 29 open on 2026-09-28), across 9 days with peak scope velocity $V_{\text{scope}} = 81$ items/day, holding completion at $85.2\%$; the proposed remedy is convergence tracking ($C_R = V_{\text{burn}} / V_{\text{scope}}$) and a three-phase air-lock in the `vibes` tool `tools/milestone_governor.py`, which `devops-cli` never adopted.
> **TLDR**: Halt runaway milestone creep using convergence velocity metrics (C_R >= 1.2) and a 3-phase air-lock ratchet that rolls secondary tasks to vNext.
> **ELI:7b**: Stop release dates from slipping forever by locking the door to non-critical feature ideas and moving extra tasks to the next version.

---

## 1. Executive Context & Baseline

In software development assisted by autonomous AI agents, throughput increases by orders of magnitude. Agents can plan, implement, test, and merge dozens of pull requests per day. However, this velocity unmasks a severe second-order project management failure mode: **The Milestone Horizon Inflation Trap**.

When AI agents operate with high autonomy, their code exploration, automated reviews, invariant scanners, and integration tests constantly surface valid improvements, refactor opportunities, sub-tasks, and latent edge cases. If incoming issues and discovered defects are continuously admitted into the *active milestone* without strict admission gates, the milestone boundary recedes faster than work can be burned down.

Milestone 36 (`v0.2.23`) of `devops-cli` is an empirical case study of this dynamic in an agentic development environment.

---

## 2. The Observed Phenomenon: Milestone 36 Scope Trajectory

During the `v0.2.23` release cycle, a target milestone intended for core infrastructure enhancements and FastMCP migrations expanded into an uncurated mega-milestone containing **196 issues and pull requests**.

### Empirical Telemetry: Creation Velocity Over Time

The snapshot below was taken on 2026-09-28; GitHub's issues API returns pull requests too, so "issues" here means issues and pull requests. The milestone closed later that day. On 2026-11-19 it holds 171 items (102 issues, 69 pull requests), created 1, 17, 19, 74, 30, 4, 10 and 16 per day over the same dates; the others moved to later milestones.

```text
Milestone 36 (v0.2.23) Issue Creation Timeline:
- 2026-09-19:   1 issue  (Milestone initialized)
- 2026-09-22:  20 issues (Initial feature requirements)
- 2026-09-23:  19 issues (Subagent architecture decomposition)
- 2026-09-24:  81 issues (Peak scope injection cascade: reviews, refactors, secondary tooling)
- 2026-09-25:  46 issues (Follow-up hardening and secondary feature expansions)
- 2026-09-26:   4 issues (Stabilization attempts)
- 2026-09-27:  10 issues (Edge-case and documentation additions)
- 2026-09-28:  15 issues (Late-cycle feature and telemetry tasks)
--------------------------------------------------------------------------------------
Total Tracked: 196 issues (167 closed, 29 open) — Completion Rate: 85.2%
```

```mermaid
flowchart TD
    subgraph AutonomousCycle ["Autonomous Agent Cycle (Unbounded Intake)"]
        PR["Agent Merges Pull Request"]
        Scan["CI & Invariant Scanners Run"]
        Disc["Scanner/Review Discovers 3 New Issues"]
        Inject["Agent Injects 3 Issues into Active Milestone"]
        PR --> Scan --> Disc --> Inject --> PR
    end

    subgraph MilestoneState ["Milestone 36 State Dynamics"]
        Closed["167 Issues Closed\n(Huge Burn Velocity)"]
        Total["196 Total Issues\n(Continually Inflating Denominator)"]
        Remaining["29 Open Issues\n(Horizon Recedes Indefinitely)"]
    end

    Inject -.-> Total
    PR -.-> Closed
    Total --> Remaining

    style Inject fill:#b3261e,color:#fff
    style Remaining fill:#f2b705,color:#000
    style Closed fill:#1b5e20,color:#fff
```

Despite closing 167 items in just 9 days—a burn-down velocity $V_{\text{burn}} \approx 18.6$ items/day ($167 / 9$)—the milestone remained incomplete because new issues were admitted at peak rates exceeding 80 issues/day.

---

## 3. The Underlying Failure Mode: The Horizon Inflation Dynamics

The root cause of milestone horizon expansion in autonomous systems is the absence of an **Air-Lock Boundary** during the stabilization and release phases of the milestone lifecycle.

### Mathematical Formulation of Milestone Convergence

Let $I_{\text{total}}(t)$ be total issues in milestone at time $t$, $I_{\text{closed}}(t)$ be closed issues, and $I_{\text{open}}(t)$ be open issues, such that:

$$I_{\text{total}}(t) = I_{\text{open}}(t) + I_{\text{closed}}(t)$$

We define:
- **Scope Injection Velocity**: $V_{\text{scope}} = \frac{d I_{\text{total}}}{dt} \approx \frac{\Delta I_{\text{created}}}{\Delta t}$
- **Burn-Down Velocity**: $V_{\text{burn}} = \frac{d I_{\text{closed}}}{dt} \approx \frac{\Delta I_{\text{closed}}}{\Delta t}$
- **Net Velocity**: $V_{\text{net}} = V_{\text{burn}} - V_{\text{scope}}$
- **Convergence Ratio**: $C_R = \frac{V_{\text{burn}}}{\max(V_{\text{scope}}, \epsilon)}$

The rate of change of open issues is governed by:

$$\frac{d I_{\text{open}}}{dt} = V_{\text{scope}} - V_{\text{burn}} = -V_{\text{net}}$$

### The Failure Regimes

1. **Release Live-Lock ($C_R \le 1.0$)**: When $V_{\text{scope}} \ge V_{\text{burn}}$, the net velocity is negative or zero ($V_{\text{net}} \le 0$). The open backlog never diminishes, and projected days to release convergence $T_{\text{conv}} \to \infty$.
2. **The Stagnant Horizon Trap ($0.70 \le \frac{I_{\text{closed}}}{I_{\text{total}}} \le 0.90$ with $I_{\text{open}} \ge 15$)**: Even when $V_{\text{burn}} > V_{\text{scope}}$, injecting new issues into the active milestone inflates the denominator $I_{\text{total}}$. The team perceives high productivity ("85% complete!"), yet the absolute remaining work (29 open issues) remains high enough to delay release indefinitely.
3. **Mega-Milestone Degradation ($I_{\text{total}} > 100$)**: Cognitive load and triage friction scale super-linearly with milestone size. Slicing, auditing, and changelog synthesis become unwieldy.

---

## 4. Remediation & Architectural Pattern: The Milestone Scope Air-Lock

To restore determinism to release cycles without throttling autonomous agent exploration, we propose the **Milestone Scope Air-Lock Pattern**. It is implemented in `vibes` as `tools/milestone_governor.py`, which reads a local JSON export of milestone issues and prints a plan; it does not move issues. `devops-cli` did not adopt it: its roadmap later marked v0.2.24 "Air-Locked", but "no code reads that word, and six PRs (#727, #731, #733, #734, #736, #738) merged into `release/v0.2.24` after it" (`docs/ROADMAP.md` at commit `ef2178563`, 2026-10-01, roadmap item for issue #739). Its glossary (`CONTEXT.md`) now names the event a "cut" and lists "air-lock" under _Avoid_.

```mermaid
stateDiagram-v2
    [*] --> INTAKE: Milestone Bootstrapped
    INTAKE --> AIR_LOCKED: Core Epics Complete / Feature Freeze Target
    AIR_LOCKED --> FROZEN: Release Candidate Tagged
    FROZEN --> CLOSED: Release Published

    state INTAKE {
        [*] --> UnrestrictedIntake
        UnrestrictedIntake: Features, bugs, refactors accepted
        UnrestrictedIntake: Sizing cap monitored (<= 50 issues)
    }

    state AIR_LOCKED {
        [*] --> BlockerOnlyGate
        BlockerOnlyGate: P0 Critical Regressions ONLY
        BlockerOnlyGate: Non-P0 items routed to vNext
    }

    state FROZEN {
        [*] --> ZeroOpenGate
        ZeroOpenGate: 0 open items allowed
        ZeroOpenGate: Unfinished tasks forcibly rolled over
    }
```

### The Three Operational Invariants

1. **Milestone Phase Ratchet**:
   - `INTAKE`: Open admission for planned features and epics. Warning threshold at 50 issues; critical ceiling at 100 issues.
   - `AIR_LOCKED`: Activated when release stabilization begins. **Zero new features or secondary enhancements may enter.** Only confirmed `P0` release-blocking regressions are admitted. All other findings must be assigned to `vNext` (e.g., `v0.2.24`).
   - `FROZEN`: Release Candidate candidate cut. Milestone is locked. Exactly zero open issues are permitted. Any open non-blocking issues are rolled over immediately.
2. **Convergence Ratio Sentinel ($C_R \ge 1.2$)**:
   - Automated governance tools calculate $C_R$ over a rolling 7-day window.
   - If $C_R < 1.0$, a blocking diagnostic is raised: *Release Live-Lock detected*. Scope intake must be frozen immediately.
3. **Automated Partitioning and Rollover**:
   - Rather than manually reviewing dozens of open issues, the `milestone_governor.py` tool partitions open issues into:
     - **Retain**: Issues with `priority/p0-critical`, `p0`, `blocker`, or active release epics.
     - **Rollover**: All secondary features, technical debt, and documentation items, automatically re-targeted to `vNext`.

---

## 5. Verifiable Impact & Key Takeaways

- **Clear Release Boundary**: Partitioning `v0.2.23`'s 29 open items separates release blockers from non-essential work that can move to `v0.2.24`. No run of `milestone_governor.py` on that milestone is recorded, and no record names which items were blockers; the milestone closed on 2026-09-28 with 0 open items.
- **Sizing & Convergence Alerts**: If $C_R < 1.2$ or the item count exceeds 50, the governor warns, early enough to act before a 196-item mega-milestone forms. The warning does not bound the milestone; a person or job must act on it.
- **Agent Intake Grounding**: Autonomous agents continue to record discoveries and file issues, but routing is decoupled from the active release vehicle:

> Discovery of new work is an asset; admitting unbounded discovery into an active release vehicle is a project management failure. Separate *discovery velocity* from *milestone admission*.
