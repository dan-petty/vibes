# Infrastructure & Observability Resources

This directory contains production-grade infrastructure manifests, observability configurations, and container topologies designed specifically for orchestrating, isolating, and monitoring autonomous AI coding agents.

---

## Directory Organization

```mermaid
flowchart TD
    Res["resources/"]

    subgraph Obs["observability/"]
        O_Readme["README.md<br><sub>Guide to agent semantic conventions & trace topology</sub>"]
        O_Collector["otel-collector-config.yaml<br><sub>OpenTelemetry Collector configuration with batching & scrubbing</sub>"]
        O_Alerts["prometheus-agent-alerts.yaml<br><sub>Alerting rules for token burn spikes, invariant gates, & CEGIS stalls</sub>"]
        subgraph Grafana["grafana/"]
            O_Dashboard["agent-telemetry-dashboard.json<br><sub>Grafana dashboard tracking agent token spend & latency</sub>"]
        end
    end

    subgraph K8s["k8s/"]
        K_Readme["README.md<br><sub>Kubernetes security invariants & deployment guide</sub>"]
        subgraph K_Sandbox["agent-sandbox/"]
            K_Pod["sandbox-pod.yaml<br><sub>Hardened Pod Security Standard worker pod</sub>"]
            K_Net["network-policy.yaml<br><sub>Zero-trust egress: blocks RFC 1918 & metadata</sub>"]
            K_Quota["resource-quota.yaml<br><sub>Namespace ResourceQuota & LimitRange protection</sub>"]
        end
        subgraph K_Obs["observability/"]
            K_OTel["otel-collector-deployment.yaml<br><sub>In-cluster OTel Collector deployment & service</sub>"]
            K_Jaeger["jaeger-deployment.yaml<br><sub>Distributed tracing collector & UI deployment</sub>"]
            K_Valkey["valkey-statefulset.yaml<br><sub>High-performance L2 AST cache StatefulSet</sub>"]
        end
    end

    subgraph Compose["docker-compose/"]
        C_Readme["README.md<br><sub>Local developer evaluation & smoke-testing guide</sub>"]
        C_Yaml["docker-compose.yml<br><sub>Turnkey 6-service local observability & sandbox stack</sub>"]
        C_Env[".env.example<br><sub>Standardized environment variables (Zero-Trust compliant)</sub>"]
    end

    Res --> Obs
    Res --> K8s
    Res --> Compose
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
