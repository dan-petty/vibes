"""Unit and concurrency tests for FastMCP Token-Bucket Gateway."""

import time

import pytest
from gateway import (
    FastMCPGateway,
    RateLimitConfig,
    RateLimitExceededException,
    TokenBucket,
)


@pytest.mark.asyncio
async def test_token_bucket_burst_capacity():
    config = RateLimitConfig(tokens_per_second=2.0, burst_capacity=5.0)
    bucket = TokenBucket(config)

    # Immediately consuming burst capacity should incur 0 wait
    wait_time = await bucket.acquire(3.0)
    assert wait_time == 0.0
    assert bucket.tokens == pytest.approx(2.0, abs=0.1)


@pytest.mark.asyncio
async def test_token_bucket_throttling_delay():
    config = RateLimitConfig(tokens_per_second=10.0, burst_capacity=1.0)
    bucket = TokenBucket(config)

    # First token immediate
    await bucket.acquire(1.0)

    # Second token requires refill (at 10 tokens/sec, 1 token takes ~0.1s)
    start = time.monotonic()
    wait_time = await bucket.acquire(1.0)
    elapsed = time.monotonic() - start

    assert wait_time > 0.05
    assert elapsed >= 0.08


@pytest.mark.asyncio
async def test_gateway_tool_dispatch_and_telemetry():
    config = RateLimitConfig(tokens_per_second=50.0, burst_capacity=50.0)
    gateway = FastMCPGateway(config)

    async def echo_tool(msg: str) -> str:
        return f"Echo: {msg}"

    gateway.register_tool("echo", echo_tool)

    res = await gateway.execute_tool("echo", msg="hello")
    assert res == "Echo: hello"
    assert gateway.telemetry.total_requests == 1
    assert gateway.telemetry.successful_invocations == 1


@pytest.mark.asyncio
async def test_gateway_automatic_retry_on_rate_limit():
    config = RateLimitConfig(tokens_per_second=50.0, burst_capacity=50.0, base_backoff_seconds=0.05)
    gateway = FastMCPGateway(config)

    attempts = 0

    async def transient_failing_tool() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RateLimitExceededException("Transient 429", retry_after=0.05)
        return "success"

    gateway.register_tool("retry_tool", transient_failing_tool)

    res = await gateway.execute_tool("retry_tool")
    assert res == "success"
    assert attempts == 3
    assert gateway.telemetry.successful_invocations == 1


@pytest.mark.asyncio
async def test_gateway_raises_on_unregistered_tool():
    gateway = FastMCPGateway()
    with pytest.raises(KeyError, match="Tool 'missing' is not registered"):
        await gateway.execute_tool("missing")


def test_a_server_supplied_retry_after_is_a_floor_not_a_ceiling() -> None:
    """RFC 9110 §10.2.3 defines Retry-After as the time after which a client *may* retry.

    Taking `min(max_backoff_seconds, retry_after)` retried *earlier* than permitted: a 429
    carrying `Retry-After: 60` slept 5 seconds under the default config and re-issued the
    request 55 seconds early, which is how a primary rate limit becomes a secondary one.
    `max_backoff_seconds` bounds this client's own exponential growth, not the server's
    instruction.
    """
    gateway = FastMCPGateway(RateLimitConfig(max_backoff_seconds=5.0))
    assert gateway._calculate_jittered_backoff(attempt=0, retry_after=60.0) == 60.0


def test_the_local_cap_still_bounds_exponential_growth() -> None:
    """Honouring the server must not remove the ceiling on backoff we chose ourselves."""
    gateway = FastMCPGateway(RateLimitConfig(max_backoff_seconds=5.0))
    assert gateway._calculate_jittered_backoff(attempt=10, retry_after=None) <= 5.0
