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
