# Docker Compose: Local Agent Observability & Sandbox Environment

A turnkey, multi-container local evaluation environment providing distributed tracing, metric aggregation, dashboard visualization, in-memory caching, and a hardened sandbox container.

---

## Service Architecture

| Service | Image | Local Port | Role |
|---|---|---|---|
| **`jaeger`** | `jaegertracing/all-in-one:latest` | `:16686` (UI), `:4317` (gRPC), `:4318` (HTTP) | Distributed trace collector and timeline waterfall UI. |
| **`otel-collector`** | `otel/opentelemetry-collector-contrib:latest` | `:8889` (Prometheus), `:13133` (Health) | Ingestion pipeline with batching, memory limiter, and scrubbing. |
| **`prometheus`** | `prom/prometheus:latest` | `:9090` (UI) | Metrics database with agent alerting rules. |
| **`valkey`** | `valkey/valkey:latest` | `:6379` | High-throughput L2 AST cache and tool memoization. |
| **`grafana`** | `grafana/grafana:latest` | `:3000` (UI) | Visual dashboards for token spend, latency, and invariant gates. |
| **`agent-sandbox`** | `python:3.14-slim` | *Internal only* | Non-root (`10001`), read-only container with cgroups caps. |

---

## Quickstart

### 1. Initialize Environment
```bash
cp .env.example .env
```

### 2. Start the Stack
```bash
docker compose up -d
```

### 3. Verify Health & Service Endpoints
- **Jaeger Tracing Waterfall**: [http://localhost:16686](http://localhost:16686)
- **Prometheus Metrics & Alerts**: [http://localhost:9090](http://localhost:9090)
- **Grafana Dashboards**: [http://localhost:3000](http://localhost:3000) (Credentials: `admin` / `admin`)
- **Valkey L2 Cache**: `valkey-cli -p 6379 ping` -> `PONG`

### 4. Pipe Agent Waterfall Traces into Live Jaeger
You can pipe spans directly from the `agent-telemetry-trace-generator` reference app:
```bash
# In another terminal, run trace generator pointing to local OTLP collector
python3 ../../examples/agent-telemetry-trace-generator/generator.py --otlp-endpoint http://localhost:4318/v1/traces
```

### 5. Teardown
```bash
docker compose down
```
