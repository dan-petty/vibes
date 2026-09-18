# Infrastructure & Observability Resources

This directory contains production-grade infrastructure manifests, observability configurations, and container topologies designed specifically for orchestrating, isolating, and monitoring autonomous AI coding agents.

---

## Directory Organization

```mermaid
flowchart LR
    Res["resources/"]

    Res --> Obs["observability/"]
    Obs --> O1["README.md"]
    Obs --> O2["otel-collector-config.yaml"]
    Obs --> O3["prometheus-agent-alerts.yaml"]
    Obs --> Grafana["grafana/"]
    Grafana --> G1["agent-telemetry-dashboard.json"]

    Res --> K8s["k8s/"]
    K8s --> K1["README.md"]
    K8s --> Sandbox["agent-sandbox/"]
    Sandbox --> S1["sandbox-pod.yaml"]
    Sandbox --> S2["network-policy.yaml"]
    Sandbox --> S3["resource-quota.yaml"]
    K8s --> KObs["observability/"]
    KObs --> KO1["otel-collector-deployment.yaml"]
    KObs --> KO2["jaeger-deployment.yaml"]
    KObs --> KO3["valkey-statefulset.yaml"]

    Res --> Compose["docker-compose/"]
    Compose --> C1["README.md"]
    Compose --> C2["docker-compose.yml"]
    Compose --> C3[".env.example"]
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
