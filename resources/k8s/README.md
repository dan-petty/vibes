# Kubernetes Manifests for Hardened Agent Sandboxes & Observability

Example Kubernetes manifests for isolated agent execution, egress network policies, and in-cluster telemetry collection. All 11 resources pass `kubeconform -strict`. Each image has a pinned version tag, the same tag that the [Docker Compose stack](../docker-compose/README.md) uses for that service.

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
   - The Valkey rule has a `podSelector` only, so it matches pods in `agent-workloads`, where Valkey runs. The collector runs in `agent-observability`, so its rule also has a `namespaceSelector` on `kubernetes.io/metadata.name: agent-observability`. Kubernetes 1.22 and later set this label on each namespace.
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

### 2. Create the Valkey Password Secret
The Valkey StatefulSet reads its password from the Secret `valkey-auth` (key `password`) in `agent-workloads`. The repository holds no Secret manifest, so no password is committed. Create the Secret with a new random value:
```bash
kubectl create secret generic valkey-auth -n agent-workloads \
  --from-literal=password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

### 3. Apply Observability Stack
```bash
kubectl apply -f resources/k8s/observability/
```

### 4. Apply Hardened Sandbox Policies
```bash
kubectl apply -f resources/k8s/agent-sandbox/
```

### 5. Verify Pod Sandbox Execution
```bash
kubectl get pods -n agent-workloads
kubectl logs agent-worker-sandbox -n agent-workloads
```

> [!NOTE]
> The Valkey StatefulSet runs `valkey-server --protected-mode yes --requirepass $(VALKEY_PASSWORD)`, and `VALKEY_PASSWORD` comes from the `valkey-auth` Secret. With a password set, protected mode accepts clients from other pods, and each client must authenticate. A client pod reads the same Secret, for example as an environment variable through `secretKeyRef`. If the Secret does not exist, the Valkey pod does not start (`CreateContainerConfigError`).

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
