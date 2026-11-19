# Infrastructure & Observability Resources

This directory contains example infrastructure manifests, observability configurations, and a Docker Compose stack for isolating and monitoring AI coding agents. They pass static checks (`kubeconform -strict`, `promtool check rules`, `docker compose config`). Nothing in this repository deploys them, and some wiring is incomplete: see the README in each subdirectory.

---

## Key Engineering Invariants Enforced

1. **Zero-Trust Egress Sanitization**:
   - The Kubernetes NetworkPolicy allows public egress only on TCP 80 and 443, to `0.0.0.0/0` minus the RFC 1918 ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) and cloud metadata (`169.254.169.254/32`). The Docker Compose stack has no egress filter.
   - Mock endpoints standardize strictly on `example.com` without subdomains.
2. **Hardened Runtime Sandboxing**:
   - The Kubernetes sandbox pod and the OTel Collector run as UID `10001` with a read-only root filesystem and drop all Linux capabilities (`drop: ["ALL"]`). The Compose `agent-sandbox` runs as `10001` with a read-only root filesystem but keeps the Docker default capabilities. The Jaeger and Valkey workloads set neither a read-only root filesystem nor a capability drop.
   - CPU and memory limits (cgroups) cap runaway CPU and memory use. They do not stop a fork bomb: that needs a PID limit (Compose `pids_limit`, kubelet `podPidsLimit`), and these files set none.
3. **Continuous Agent Telemetry**:
   - OTLP trace ingestion through the OpenTelemetry Collector. W3C trace context (`traceparent`) propagation between subagents must come from the agent code; these files do not add it.
   - Alert rules and a Grafana dashboard for token spend, P50/P90/P99 tool latency and invariant gate results. They read `vibes_agent_*` metrics that no code in this repository emits, so they show data only if your agent exports matching metrics.

---

## Directory Organization

```text
├── observability/
│   ├── README.md                           # Guide to agent semantic conventions and trace topology
│   ├── otel-collector-config.yaml          # OpenTelemetry Collector configuration with batching & scrubbing
│   ├── prometheus-agent-alerts.yaml        # Alerting rules for token burn spikes, invariant gates, and CEGIS stalls
│   └── grafana/
│       └── agent-telemetry-dashboard.json  # Grafana dashboard for agent token spend & latency
├── k8s/
│   ├── README.md                           # Kubernetes security invariants and deployment guide
│   ├── agent-sandbox/
│   │   ├── sandbox-pod.yaml                # Hardened Pod Security Standard ('Restricted') worker pod
│   │   ├── network-policy.yaml             # Zero-trust egress policy: blocks private RFC 1918 IPs & metadata
│   │   └── resource-quota.yaml             # Namespace ResourceQuota and LimitRange protection
│   └── observability/
│       ├── otel-collector-deployment.yaml  # In-cluster OpenTelemetry Collector deployment and service
│       ├── jaeger-deployment.yaml          # Distributed tracing collector and UI deployment
│       └── valkey-statefulset.yaml         # Valkey StatefulSet and Service for an L2 cache
└── docker-compose/
    ├── README.md                           # Local developer evaluation and smoke-testing guide
    ├── docker-compose.yml                  # 6-service local observability and sandbox stack
    └── .env.example                        # Environment variables (Compose reads only the Grafana admin pair)
```
