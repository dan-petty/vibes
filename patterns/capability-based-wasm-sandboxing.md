# Pattern: Capability-Based Wasm Sandboxing

> **Pattern Class**: Systems Architecture & Runtime Isolation  
> **Problem**: Ephemeral agent-generated tool execution suffers from ambient UNIX authority vulnerabilities (CWE-250) or container start-up latency  
> **Solution**: Execute untrusted guest code inside WebAssembly components governed by object-capability handles, deterministic instruction gas budgets, and linear memory limits  
> **TLDR**: Isolate ephemeral agent tools inside WASI 0.2 components with capability handles, deterministic gas metering, and fast instantiation.
> **ELI:7b**: Run AI tools in tiny, super-fast WebAssembly playpens where they only have permission to touch the exact files you handed them.
> **Reference Implementation**: [`examples/wasm-capability-sandbox/`](../examples/wasm-capability-sandbox/)

---

## 1. Problem Statement: The Sandboxing Dilemma

Autonomous AI agents generate ephemeral tools (format converters, AST transformers, data validators) during multi-step development tasks. Traditional execution approaches face a difficult tradeoff:

- **Ambient Execution**: Running tools directly on the host grants full ambient UNIX authority, exposing filesystems, environment credentials, and network sockets to unverified LLM-generated code.
- **Container Execution**: Spawning containers (`docker run`, cgroups) enforces security but adds start-up latency and memory to every run, stalling iterative feedback loops.

---

## 2. The Architectural Pattern: Object-Capability Perimeter

The reference implementation is a pure-Python simulation: it runs no WebAssembly. It checks the 8-byte module header and section IDs, reads `import`/`export` lines from WIT text, and models gas and memory pages as counters over a Python list of `(op, cost)` pairs. Its capability IDs are the SHA-256 of the kind, the scope and a fixed salt, so anyone who knows the kind and scope can compute them, and the access checks compare only kind and scope. Real isolation needs a Wasm runtime, for example Wasmtime with fuel metering, store memory limits, and WASI preopened directories.

The **Capability-Based Wasm Sandboxing** pattern decouples code execution from host authority using the WebAssembly Component Model (WASI 0.2):

```mermaid
sequenceDiagram
    autonumber
    participant Host as Agent Host Process
    participant Broker as Capability Token Broker
    participant Gateway as WASI 0.2 Sandbox Gateway
    participant Guest as Wasm Guest Component

    Host->>Broker: Request capability (e.g. read /workspace/data)
    Broker-->>Host: Mint capability handle (runtime-held reference)
    Host->>Gateway: Instantiate guest with capability handle table
    Gateway->>Guest: Execute with gas metering (CAP003 defense)
    alt Guest invokes ungranted resource
        Guest-->>Gateway: Trap: Ambient Resource Denied (CAP001/CAP002)
        Gateway-->>Host: Emit structured SARIF error
    else Guest completes within gas budget
        Guest-->>Gateway: Return computation result
        Gateway-->>Host: Deliver sanitized execution telemetry
    end
```

---

## 3. Core Principles

1. **Zero Ambient Authority**:
   - The guest module cannot access any filesystem path, network socket, or environment variable unless explicitly provided as a capability handle. In a real runtime the handle is unforgeable because it is an index into a table that only the host can fill; a hash string is not.
2. **Deterministic Gas Metering**:
   - Every instruction decrements an instruction counter, deterministically neutralizing infinite loops and CPU exhaustion (CWE-400) without relying on asynchronous OS signals.
3. **Linear Memory Ceilings**:
   - Memory growth is strictly capped at page allocation boundaries (e.g., maximum 16 pages / 1 MiB), preventing memory exhaustion.
4. **Declarative WIT Contracts**:
   - WebAssembly Interface Type (`wit`) declarations explicitly define required imports and provided exports, verified prior to instantiation (`CAP005`).

---

## 4. Invariant Rules Summary

- **`CAP001`**: Ambient Filesystem Access Violation.
- **`CAP002`**: Ambient Network Egress Violation.
- **`CAP003`**: Gas Budget Exhaustion (bounds guests that do not terminate).
- **`CAP004`**: Linear Memory Ceiling Breach.
- **`CAP005`**: WIT Interface Contract Mismatch.
