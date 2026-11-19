# Kubernetes Manifests for Hardened Agent Sandboxes & Observability

Example Kubernetes manifests for isolated agent execution, egress network policies, and in-cluster telemetry collection. All 11 resources pass `kubeconform -strict`. The Jaeger and Valkey workloads use `:latest` image tags.

---

## Security Invariants Enforced

1. **Pod Security Standard (`Restricted`)**: the sandbox pod spec meets the Restricted profile. The `kubectl create namespace` step below adds no `pod-security.kubernetes.io/enforce` label, so the API server does not enforce the profile until you add one.
   - `runAsNonRoot: true` with non-zero UID/GID (`10001`).
   - `readOnlyRootFilesystem: true` with ephemeral, size-bounded `emptyDir` scratch volumes.
   - `capabilities.drop: ["ALL"]` removes all capabilities, including `CAP_NET_RAW` (raw sockets). `allowPrivilegeEscalation: false` stops privilege gain through setuid binaries.
   - `seccompProfile.type: RuntimeDefault` filtering dangerous system calls.
2. **SSRF & Network Egress Mitigation (`NetworkPolicy`)**:
   - Denies all inbound traffic to sandbox pods.
   - Restricts egress to cluster DNS (`kube-dns:53`), in-cluster Valkey (`:6379`), and in-cluster OTel Collector (`:4317`/`:4318`).
   - The Valkey and OTel Collector rules have a `podSelector` and no `namespaceSelector`, so they match pods in `agent-workloads` only. Valkey runs there. The collector runs in `agent-observability`, so the sandbox cannot reach it until the rule adds `namespaceSelector` with `kubernetes.io/metadata.name: agent-observability`.
   - External egress is allowed only on TCP 80 and 443, and excludes private RFC 1918 CIDRs (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) and cloud metadata (`169.254.169.254/32`).
   - A NetworkPolicy has effect only if the cluster network plugin enforces NetworkPolicy.
3. **Resource Starvation Protection (`ResourceQuota` & `LimitRange`)**:
   - The ResourceQuota caps the `agent-workloads` namespace at 10 pods and fixed CPU and memory totals. The LimitRange sets per-container defaults and maxima.
   - Neither limits the process count inside a container. A fork bomb needs a PID limit (kubelet `podPidsLimit`), which these manifests do not set.

---

## Deployment Instructions

### 1. Create Namespaces
```bash
kubectl create namespace agent-workloads
kubectl create namespace agent-observability
```

### 2. Apply Observability Stack
```bash
kubectl apply -f resources/k8s/observability/
```

### 3. Apply Hardened Sandbox Policies
```bash
kubectl apply -f resources/k8s/agent-sandbox/
```

### 4. Verify Pod Sandbox Execution
```bash
kubectl get pods -n agent-workloads
kubectl logs agent-worker-sandbox -n agent-workloads
```

> [!WARNING]
> The Valkey StatefulSet runs `valkey-server --protected-mode yes` with no password. In that mode Valkey accepts only loopback and Unix socket clients, so it refuses connections from other pods until you configure a password (`--requirepass` from a Secret) or an ACL user.

---

## Directory Structure

```text
├── agent-sandbox/
│   ├── sandbox-pod.yaml        # Non-root, read-only rootfs, dropped capabilities, seccomp RuntimeDefault
│   ├── network-policy.yaml     # Zero-trust egress policy: blocks private RFC 1918 IPs & cloud metadata
│   └── resource-quota.yaml     # Namespace ResourceQuota and LimitRange caps
└── observability/
    ├── otel-collector-deployment.yaml  # OTel Collector Deployment, ConfigMap, and Service
    ├── jaeger-deployment.yaml          # Distributed tracing UI & OTLP endpoint
    └── valkey-statefulset.yaml         # In-cluster Valkey L2 AST & embedding cache
```
