#!/usr/bin/env python3
"""FastMCP Token-Bucket Rate Limiter & Tool Gateway.

Protects external APIs (GitHub REST/GraphQL, LLM inference endpoints, cloud APIs)
from unthrottled agent bursts using client-side token-bucket pacing and bounded exponential
backoff with jitter.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


@dataclass
class RateLimitConfig:
    """Configuration for token bucket rate limiter."""

    tokens_per_second: float = 5.0
    burst_capacity: float = 10.0
    max_retries: int = 4
    base_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 5.0


@dataclass
class GatewayTelemetry:
    """Telemetry metrics tracking tool invocations and throttling."""

    total_requests: int = 0
    throttled_requests: int = 0
    total_wait_seconds: float = 0.0
    successful_invocations: int = 0
    failed_invocations: int = 0


class TokenBucket:
    """Thread-safe and async-safe token bucket rate limiter."""

    def __init__(self, config: RateLimitConfig) -> None:
        self.config = config
        self.tokens = config.burst_capacity
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens_needed: float = 1.0) -> float:
        """Acquire tokens, sleeping asynchronously if bucket is empty.

        Returns total wait duration in seconds.
        """
        async with self._lock:
            wait_duration = 0.0
            self._refill()

            while self.tokens < tokens_needed:
                deficit = tokens_needed - self.tokens
                sleep_time = deficit / self.config.tokens_per_second
                wait_duration += sleep_time
                await asyncio.sleep(sleep_time)
                self._refill()

            self.tokens -= tokens_needed
            return wait_duration

    def _refill(self) -> None:
        """Refill tokens based on elapsed monotonic time."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        refill_amount = elapsed * self.config.tokens_per_second
        self.tokens = min(self.config.burst_capacity, self.tokens + refill_amount)
        self.last_refill = now


class FastMCPGateway:
    """Gateway dispatching tool calls through token bucket pacing and backoff retry loops."""

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self.config = config or RateLimitConfig()
        self.limiter = TokenBucket(self.config)
        self.telemetry = GatewayTelemetry()
        self._tools: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}

    def register_tool(self, name: str, handler: Callable[..., Coroutine[Any, Any, Any]]) -> None:
        """Register an async tool handler."""
        self._tools[name] = handler

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> Any:
        """Execute a registered tool under token bucket pacing and exponential backoff."""
        handler = self._tools.get(tool_name)
        if not handler:
            raise KeyError(f"Tool '{tool_name}' is not registered with this gateway.")

        self.telemetry.total_requests += 1

        # Acquire token budget
        wait_time = await self.limiter.acquire(1.0)
        if wait_time > 0.0:
            self.telemetry.throttled_requests += 1
            self.telemetry.total_wait_seconds += wait_time

        return await self._invoke_with_backoff(handler, **kwargs)

    async def _invoke_with_backoff(
        self, handler: Callable[..., Coroutine[Any, Any, Any]], **kwargs: Any
    ) -> Any:
        """Execute handler with bounded exponential backoff and jitter on rate limits."""
        for attempt in range(self.config.max_retries):
            try:
                result = await handler(**kwargs)
                self.telemetry.successful_invocations += 1
                return result
            except RateLimitExceededException as err:
                if attempt == self.config.max_retries - 1:
                    self.telemetry.failed_invocations += 1
                    raise

                sleep_duration = self._calculate_jittered_backoff(attempt, err.retry_after)
                await asyncio.sleep(sleep_duration)

        raise RuntimeError("Exceeded maximum retry attempts without resolution.")

    def _calculate_jittered_backoff(self, attempt: int, retry_after: float | None) -> float:
        """Calculate exponential backoff with full jitter, honoring retry_after if given."""
        if retry_after is not None and retry_after > 0:
            return min(self.config.max_backoff_seconds, retry_after)

        raw_backoff = self.config.base_backoff_seconds * (2**attempt)
        capped_backoff = min(self.config.max_backoff_seconds, raw_backoff)
        # Full jitter between 0 and capped_backoff
        return random.uniform(0.1, capped_backoff)


class RateLimitExceededException(Exception):
    """Raised when external downstream service returns HTTP 429."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# Demonstration CLI
async def demo() -> None:
    print("🚀 FastMCP Token-Bucket Gateway Demo")
    config = RateLimitConfig(tokens_per_second=10.0, burst_capacity=5.0)
    gateway = FastMCPGateway(config)

    # Register mock GitHub PR fetcher tool
    call_counter = 0

    async def mock_gh_pr_get(pr_number: int) -> dict[str, Any]:
        nonlocal call_counter
        call_counter += 1
        # Simulate transient 429 on every 4th call
        if call_counter % 4 == 0:
            raise RateLimitExceededException("GitHub API Secondary Rate Limit Exceeded", retry_after=0.2)
        return {"pr": pr_number, "title": f"Feature #{pr_number}", "state": "open"}

    gateway.register_tool("gh_pr_get", mock_gh_pr_get)

    print("Launching 10 concurrent agent tool calls...")
    tasks = [gateway.execute_tool("gh_pr_get", pr_number=i) for i in range(1, 11)]
    results = await asyncio.gather(*tasks)

    print(f"Successfully processed {len(results)} tool invocations!")
    print(
        f"Telemetry: {gateway.telemetry.total_requests} requests, "
        f"{gateway.telemetry.throttled_requests} throttled, "
        f"{gateway.telemetry.total_wait_seconds:.2f}s total wait time."
    )


if __name__ == "__main__":
    asyncio.run(demo())
