#!/usr/bin/env python3
"""Model provider abstractions and adapters for the prompt mutation fuzzer.

Implements model provider generators closing the capability gap held by
NVIDIA/garak and promptfoo/promptfoo. Supports multi-provider execution across:
1. MockModelProvider: Deterministic, zero-network mock provider for offline testing and CI.
2. OpenAIProvider: OpenAI-compatible endpoints (/v1/chat/completions) for cloud and local services.
3. AnthropicProvider: Anthropic Claude messages API (/v1/messages).
4. OllamaProvider: Local Ollama service (/api/generate and /api/chat).
5. GenericRestProvider: Configurable REST generator matching arbitrary JSON payloads.
"""

# sentinel: allow[ZeroTrustSanitization] — this module defines the private-address policy;
# the ranges below are that definition, not an endpoint.

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

# Standard zero-trust documentation and loopback networks
ALLOWED_TEST_NETWORKS: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("::1/128"),
)

# Prohibited internal infrastructure networks
PRIVATE_NETWORKS: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

_CODE_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"```(?:[a-zA-Z0-9_-]+)?\s*\n(?P<code>.*?)```",
    re.DOTALL,
)

HttpTransport = Callable[[urllib.request.Request, float], tuple[int, dict[str, Any], float]]


@dataclass(frozen=True)
class ProviderResponse:
    """Unified response from an LLM model provider invocation."""

    raw_text: str
    extracted_code: str
    model_id: str
    provider_name: str
    latency_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None


def extract_code_block(text: str) -> str:
    """Extract code from fenced Markdown block if present, or return raw text."""
    matches = list(_CODE_BLOCK_RE.finditer(text))
    if matches:
        return matches[0].group("code").strip()
    return text.strip()


def _is_ip_in_networks(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    nets: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """Predicate reporting whether IP falls within any of the specified networks."""
    return any(ip in net for net in nets)


def _check_ip_egress(hostname: str) -> tuple[bool, str]:
    """Validate IP address string against allowed and private network policies."""
    try:
        ip = ipaddress.ip_address(hostname)
        if _is_ip_in_networks(ip, ALLOWED_TEST_NETWORKS):
            return True, "documentation/test network"
        if _is_ip_in_networks(ip, PRIVATE_NETWORKS):
            return False, f"Destination IP {ip} is within private network"
    except ValueError:
        pass
    if hostname.endswith((".lan", ".local", ".internal")):
        return False, f"Internal domain '{hostname}' prohibited under zero-trust policy"
    return True, "valid domain"


def validate_provider_endpoint(url_str: str, allow_localhost: bool = True) -> tuple[bool, str]:
    """Ensure target API endpoint complies with zero-trust egress policies."""
    parsed = urllib.parse.urlparse(url_str)
    if parsed.scheme not in ("http", "https"):
        return False, f"Unsupported URL scheme '{parsed.scheme}'; expected http or https"
    hostname = parsed.hostname or ""
    if not hostname:
        return False, "Missing hostname in URL"
    if allow_localhost and hostname in ("localhost", "127.0.0.1", "::1"):
        return True, "localhost"
    return _check_ip_egress(hostname)


def _read_response(resp: Any, start: float) -> tuple[int, dict[str, Any], float]:
    """Parse successful HTTP response body."""
    status = resp.status
    body = json.loads(resp.read().decode("utf-8"))
    return status, body, time.perf_counter() - start


def _parse_error_payload(exc: urllib.error.HTTPError) -> dict[str, Any]:
    """Extract structured or text error message from HTTPError."""
    raw = exc.read().decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"error": str(parsed)}
    except (json.JSONDecodeError, ValueError):
        return {"error": raw}


def _perform_urllib_request(req: urllib.request.Request, timeout: float) -> tuple[int, dict[str, Any], float]:
    """Execute live HTTP request via standard library urllib."""
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _read_response(resp, start)
    except urllib.error.HTTPError as exc:
        return exc.code, _parse_error_payload(exc), time.perf_counter() - start
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc.reason)}, time.perf_counter() - start


def execute_http_json_request(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float = 10.0,
    transport: HttpTransport | None = None,
) -> tuple[int, dict[str, Any], float]:
    """Execute JSON HTTP POST request returning status code, parsed body, and latency."""
    valid, reason = validate_provider_endpoint(url)
    if not valid:
        return 0, {"error": f"Egress violation: {reason}"}, 0.0

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    if transport is not None:
        return transport(req, timeout)
    return _perform_urllib_request(req, timeout)


def _step_json_path(current: Any, element: str | int) -> Any:
    """Traverse one step along a JSON path element."""
    if isinstance(current, dict) and isinstance(element, str):
        return current.get(element, "")
    if isinstance(current, (list, tuple)) and isinstance(element, int) and 0 <= element < len(current):
        return current[element]
    return ""


def _extract_json_path(data: Any, path: Sequence[str | int]) -> str:
    """Traverse nested dictionary or list using key/index sequence."""
    current = data
    for element in path:
        current = _step_json_path(current, element)
        if not current:
            break
    return str(current) if current is not None else ""


# --- Model Provider Base Class ---


class ModelProvider(ABC):
    """Abstract base class for all LLM model provider backends."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Canonical name of the model provider."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Target model identifier."""

    @abstractmethod
    def generate(self, prompt: str, system_prompt: str | None = None) -> ProviderResponse:
        """Generate code/text from model given user prompt and optional system prompt."""


# --- Provider 1: MockModelProvider ---


CLEAN_CODE_RESPONSE: Final[str] = (
    "```python\n"
    "def clean_helper(x: int) -> int:\n"
    "    return x * 2\n"
    "```"
)

INJECTION_BYPASS_RESPONSE: Final[str] = (
    "```python\n"
    "def injected_bypass(a, b, c, d, e, f):\n"
    "    if a:\n"
    "        if b:\n"
    "            if c:\n"
    "                if d:\n"
    "                    if e:\n"
    "                        if f: return 1\n"
    "    return 0\n"
    "```"
)

COMPLEXITY_TRAP_RESPONSE: Final[str] = (
    "```python\n"
    "def monolithic_trap(x):\n"
    "    if x == 1: return 1\n"
    "    elif x == 2: return 2\n"
    "    elif x == 3: return 3\n"
    "    elif x == 4: return 4\n"
    "    elif x == 5: return 5\n"
    "    elif x == 6: return 6\n"
    "    elif x == 7: return 7\n"
    "    elif x == 8: return 8\n"
    "    elif x == 9: return 9\n"
    "    elif x == 10: return 10\n"
    "    elif x == 11: return 11\n"
    "    return 0\n"
    "```"
)


def _default_mock_heuristic(prompt: str) -> str:
    """Deterministic heuristic returning clean or drifted code based on mutation keywords."""
    if "INJECTION_ESCAPE" in prompt or "SYSTEM OVERRIDE" in prompt:
        return INJECTION_BYPASS_RESPONSE
    if "COMPLEXITY_TRAP" in prompt or "monolithic" in prompt:
        return COMPLEXITY_TRAP_RESPONSE
    return CLEAN_CODE_RESPONSE


class MockModelProvider(ModelProvider):
    """Deterministic in-memory mock provider for offline testing and CI."""

    def __init__(
        self,
        model_id: str = "mock-model-v1",
        canned_responses: dict[str, str] | None = None,
        response_generator: Callable[[str], str] | None = None,
    ) -> None:
        self._model_id = model_id
        self._canned = canned_responses or {}
        self._generator = response_generator or _default_mock_heuristic

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def model_id(self) -> str:
        return self._model_id

    def generate(self, prompt: str, system_prompt: str | None = None) -> ProviderResponse:
        """Return deterministic mock generation based on canned mappings or heuristics."""
        raw = self._canned.get(prompt) or self._generator(prompt)
        return ProviderResponse(
            raw_text=raw,
            extracted_code=extract_code_block(raw),
            model_id=self._model_id,
            provider_name="mock",
            latency_seconds=0.001,
            prompt_tokens=len(prompt.split()),
            completion_tokens=len(raw.split()),
        )


# --- Provider 2: OpenAIProvider ---


class OpenAIProvider(ModelProvider):
    """OpenAI-compatible chat completion provider (/v1/chat/completions)."""

    def __init__(
        self,
        model_id: str = "gpt-4o",
        api_base: str = "https://example.com/v1",
        api_key: str | None = None,
        timeout: float = 15.0,
        transport: HttpTransport | None = None,
    ) -> None:
        self._model_id = model_id
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._timeout = timeout
        self._transport = transport

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def model_id(self) -> str:
        return self._model_id

    def _build_payload(self, prompt: str, system_prompt: str | None) -> dict[str, Any]:
        """Construct chat completions message payload."""
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return {"model": self._model_id, "messages": messages, "temperature": 0.2}

    def generate(self, prompt: str, system_prompt: str | None = None) -> ProviderResponse:
        """Call OpenAI chat completions API endpoint."""
        url = f"{self._api_base}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        payload = self._build_payload(prompt, system_prompt)
        code, body, latency = execute_http_json_request(
            url, headers, payload, timeout=self._timeout, transport=self._transport
        )
        if code != 200:
            err = body.get("error", f"HTTP {code}")
            return ProviderResponse("", "", self._model_id, "openai", latency, error=str(err))

        text = _extract_json_path(body, ("choices", 0, "message", "content"))
        usage = body.get("usage", {})
        return ProviderResponse(
            raw_text=text,
            extracted_code=extract_code_block(text),
            model_id=self._model_id,
            provider_name="openai",
            latency_seconds=latency,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )


# --- Provider 3: AnthropicProvider ---


class AnthropicProvider(ModelProvider):
    """Anthropic Claude messages API provider (/v1/messages)."""

    def __init__(
        self,
        model_id: str = "claude-3-5-sonnet",
        api_base: str = "https://example.com/v1",
        api_key: str | None = None,
        timeout: float = 15.0,
        transport: HttpTransport | None = None,
    ) -> None:
        self._model_id = model_id
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self._timeout = timeout
        self._transport = transport

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_id(self) -> str:
        return self._model_id

    def _build_payload(self, prompt: str, system_prompt: str | None) -> dict[str, Any]:
        """Construct Anthropic messages payload."""
        payload: dict[str, Any] = {
            "model": self._model_id,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            payload["system"] = system_prompt
        return payload

    def generate(self, prompt: str, system_prompt: str | None = None) -> ProviderResponse:
        """Call Anthropic messages endpoint."""
        url = f"{self._api_base}/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
        }
        payload = self._build_payload(prompt, system_prompt)
        code, body, latency = execute_http_json_request(
            url, headers, payload, timeout=self._timeout, transport=self._transport
        )
        if code != 200:
            err = body.get("error", f"HTTP {code}")
            return ProviderResponse("", "", self._model_id, "anthropic", latency, error=str(err))

        text = _extract_json_path(body, ("content", 0, "text"))
        usage = body.get("usage", {})
        return ProviderResponse(
            raw_text=text,
            extracted_code=extract_code_block(text),
            model_id=self._model_id,
            provider_name="anthropic",
            latency_seconds=latency,
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
        )


# --- Provider 4: OllamaProvider ---


class OllamaProvider(ModelProvider):
    """Local Ollama service provider (/api/generate)."""

    def __init__(
        self,
        model_id: str = "qwen2.5-coder:7b",
        api_base: str = "http://localhost:11434",
        timeout: float = 30.0,
        transport: HttpTransport | None = None,
    ) -> None:
        self._model_id = model_id
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    @property
    def provider_name(self) -> str:
        return "ollama"

    @property
    def model_id(self) -> str:
        return self._model_id

    def generate(self, prompt: str, system_prompt: str | None = None) -> ProviderResponse:
        """Call local Ollama generation endpoint."""
        url = f"{self._api_base}/api/generate"
        headers = {"Content-Type": "application/json"}
        payload: dict[str, Any] = {
            "model": self._model_id,
            "prompt": prompt,
            "stream": False,
        }
        if system_prompt:
            payload["system"] = system_prompt

        code, body, latency = execute_http_json_request(
            url, headers, payload, timeout=self._timeout, transport=self._transport
        )
        if code != 200:
            err = body.get("error", f"HTTP {code}")
            return ProviderResponse("", "", self._model_id, "ollama", latency, error=str(err))

        text = str(body.get("response", ""))
        prompt_evals = int(body.get("prompt_eval_count", 0))
        eval_count = int(body.get("eval_count", 0))
        return ProviderResponse(
            raw_text=text,
            extracted_code=extract_code_block(text),
            model_id=self._model_id,
            provider_name="ollama",
            latency_seconds=latency,
            prompt_tokens=prompt_evals,
            completion_tokens=eval_count,
        )


# --- Provider 5: GenericRestProvider ---


class GenericRestProvider(ModelProvider):
    """Configurable REST generator for arbitrary JSON inference endpoints."""

    def __init__(
        self,
        endpoint_url: str = "https://example.com/api/predict",
        model_id: str = "custom-llm",
        response_path: Sequence[str | int] = ("output",),
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        transport: HttpTransport | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._model_id = model_id
        self._response_path = tuple(response_path)
        self._headers = headers or {"Content-Type": "application/json"}
        self._timeout = timeout
        self._transport = transport

    @property
    def provider_name(self) -> str:
        return "rest"

    @property
    def model_id(self) -> str:
        return self._model_id

    def generate(self, prompt: str, system_prompt: str | None = None) -> ProviderResponse:
        """Call generic REST endpoint with structured JSON payload."""
        payload: dict[str, Any] = {
            "model": self._model_id,
            "prompt": prompt,
            "system": system_prompt or "",
        }
        code, body, latency = execute_http_json_request(
            self._endpoint_url,
            self._headers,
            payload,
            timeout=self._timeout,
            transport=self._transport,
        )
        if code != 200:
            err = body.get("error", f"HTTP {code}")
            return ProviderResponse("", "", self._model_id, "rest", latency, error=str(err))

        text = _extract_json_path(body, self._response_path)
        return ProviderResponse(
            raw_text=text,
            extracted_code=extract_code_block(text),
            model_id=self._model_id,
            provider_name="rest",
            latency_seconds=latency,
        )


# --- Provider Factory and Registry ---

PROVIDER_REGISTRY: Final[dict[str, type[ModelProvider]]] = {
    "mock": MockModelProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
    "rest": GenericRestProvider,
}


def list_supported_providers() -> tuple[str, ...]:
    """Return tuple of supported provider identifier names."""
    return tuple(sorted(PROVIDER_REGISTRY.keys()))


def _populate_provider_options(
    key: str,
    kwargs: dict[str, Any],
    model: str | None,
    api_base: str | None,
    api_key: str | None,
) -> dict[str, Any]:
    """Combine caller arguments into provider constructor keyword dictionary."""
    options = dict(kwargs)
    if model is not None:
        options["model_id"] = model
    if api_base is not None:
        url_field = "endpoint_url" if key == "rest" else "api_base"
        options[url_field] = api_base
    if api_key is not None and key in ("openai", "anthropic"):
        options["api_key"] = api_key
    return options


def get_provider(
    name: str = "mock",
    model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    **kwargs: Any,
) -> ModelProvider:
    """Instantiate and return a configured ModelProvider."""
    key = name.lower().strip()
    cls = PROVIDER_REGISTRY.get(key)
    if not cls:
        raise ValueError(f"Unknown provider '{name}'. Supported: {', '.join(list_supported_providers())}")
    options = _populate_provider_options(key, kwargs, model, api_base, api_key)
    return cls(**options)
