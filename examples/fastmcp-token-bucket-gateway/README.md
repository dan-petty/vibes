# Sample App: FastMCP Token-Bucket Gateway

An asynchronous, client-side token-bucket rate limiter and tool gateway for Model Context Protocol (FastMCP) AI agents.

---

## Why This Exists

Autonomous AI agents executing iterative tasks (such as mass triage of pull requests or scanning dozens of repository files) will trigger dozens of API requests per second if unconstrained. This quickly leads to:
- HTTP 429 (`Too Many Requests`) lockouts from GitHub, OpenAI, Anthropic, or cloud providers.
- Cascading retries that worsen secondary rate limits.
- Stalled agent workflows.

The **FastMCP Token-Bucket Gateway** solves this by inserting a local, async-safe token bucket between the agent's decision loop and downstream network APIs, incorporating:
- Token-bucket refill pacing.
- Burst capacity handling.
- Bounded exponential backoff with randomized jitter.
- Detailed telemetry on throttled requests and wait durations.

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent loop
    participant B as Token bucket
    participant API as Downstream API

    A->>B: acquire (burst of 5)
    B-->>A: 5 tokens, bucket empty
    A->>API: 5 concurrent calls
    API-->>A: 200 OK

    A->>B: acquire (6th call)
    B-->>A: wait 0.4s for refill
    Note over B: Pacing happens locally, before<br/>the request is ever sent.

    A->>API: 6th call
    API-->>A: 429 Too Many Requests
    A->>B: register backoff
    B-->>A: sleep 1s + jitter

    Note over A,API: Jitter decorrelates retries across<br/>concurrent agents. Without it, every<br/>client retries on the same tick and<br/>rebuilds the spike that caused the 429.

    A->>API: retry
    API-->>A: 200 OK
```

---

## Quick Start

### Running the Interactive Demo
```bash
python3 gateway.py
```

### Running the Test Suite
```bash
pytest test_gateway.py -v
```

---

## Integration Pattern

```python
from gateway import FastMCPGateway, RateLimitConfig

gateway = FastMCPGateway(RateLimitConfig(tokens_per_second=5.0, burst_capacity=10.0))

# Register external tools
gateway.register_tool("github_pr_get", my_gh_tool)

# Execute via paced gateway
result = await gateway.execute_tool("github_pr_get", pr_number=42)
```
