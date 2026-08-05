"""Thin transport wrapper around ai_backend /callAI for MiroFish.

Single concern: build the /callAI payload, POST it, return the text. All policy
(failover, JSON repair, kimi temp override, retry) lives in app.utils.llm_client.
"""
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0


def _post_call(
    messages: list[dict],
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str | None,
    temperature: float,
    max_tokens: int | None,
    response_format: dict | None,
    thinking_disabled: bool,
    timeout: float,
) -> dict:
    metadata: dict[str, Any] = {"model": model, "temperature": temperature}
    if max_tokens is not None:
        metadata["max_tokens"] = max_tokens
    if response_format is not None:
        metadata["response_format"] = response_format
    if thinking_disabled:
        metadata["thinking"] = {"type": "disabled"}

    body = {
        "provider": provider,
        "prompt": messages,
        "task": "chat",
        "metadata": metadata,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    resp = httpx.post(
        f"{base_url.rstrip('/')}/callAI",
        json=body,
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def chat(
    messages: list[dict],
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str | None = None,
    temperature: float = 1.0,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    thinking_disabled: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Single chat turn against ai_backend. Returns stripped text content.

    `provider` + `model` are passed through to ai_backend, which dispatches to
    that upstream. e.g. provider=minimax + model=MiniMax-M3 → MiniMax;
    provider=deepseek + model=deepseek-v4-flash → DeepSeek.
    """
    data = _post_call(
        messages,
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        thinking_disabled=thinking_disabled,
        timeout=timeout,
    )
    text = (
        data.get("result", {}).get("text")
        or data.get("text")
        or ""
    )
    return text.strip()