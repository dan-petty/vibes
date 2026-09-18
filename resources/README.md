# Infrastructure & Observability Resources

This directory contains production-grade infrastructure manifests, observability configurations, and container topologies designed specifically for orchestrating, isolating, and monitoring autonomous AI coding agents.

---

## Directory Organization

```mermaid
flowchart TD
    Res["resources/"]

    subgraph Obs["1. observability/ (Agent Telemetry & Mesh)"]
        direction TB
        O_Readme["README.md<br><sub>Guide to agent semantic conventions & trace topology</sub>"]
        O_Collector["otel-collector-config.yaml<br><sub>OpenTelemetry Collector configuration with batching & scrubbing</sub>"]
        O_Alerts["prometheus-agent-alerts.yaml<br><sub>Alerting rules for token burn spikes, invariant gates, & CEGIS stalls</sub>"]
        O_Dashboard["grafana/agent-telemetry-dashboard.json<br><sub>Grafana dashboard tracking agent token spend & latency</sub>"]
    end

    subgraph K8s["2. k8s/ (Cluster Sandboxes & Isolation)"]
        direction TB
        K_Readme["README.md<br><sub>Kubernetes security invariants & deployment guide</sub>"]
        K_Sandbox["agent-sandbox/<br><sub>Restricted pods, NetworkPolicies, & ResourceQuotas</sub>"]
        K_Obs["observability/<br><sub>In-cluster OTel Collector, Jaeger UI, & Valkey StatefulSet</sub>"]
    end

    subgraph Compose["3. docker-compose/ (Local Evaluation Stack)"]
        direction TB
        C_Readme["README.md<br><sub>Local developer evaluation & smoke-testing guide</sub>"]
        C_Yaml["docker-compose.yml<br><sub>Turnkey 6-service local observability & sandbox stack</sub>"]
        C_Env[".env.example<br><sub>Standardized environment variables (Zero-Trust compliant)</sub>"]
    end

    Res --> Obs
    Obs --> K8s
    K8s --> Compose
```

---

## Key Engineering Invariants Enforced

1. **Zero-Trust Egress Sanitization**:
   - Manifests strictly reject internal RFC 1918 addresses (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) and cloud metadata (`169.254.169.254/32`).
   - Mock endpoints standardize strictly on `example.com` without subdomains.
2. **Hardened Runtime Sandboxing**:
   - Container processes run as non-root (`10001`), with read-only root filesystems and all Linux capabilities dropped (`drop: ["ALL"]`).
   - Hard cgroups v2 caps on CPU and memory prevent fork bombs and runaway subprocesses.
3. **Continuous Agent Telemetry**:
   - W3C distributed trace context propagation across multi-tier subagents.
   - Real-time token spend tracking, P50/P90/P99 tool latency measurement, and AST invariant gate auditing.
