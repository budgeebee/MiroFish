"""
LLM client — routes through ai_backend /callAI.

Two-provider setup: AI_BACKEND_PROVIDER + AI_BACKEND_MODEL is the primary path,
AI_BACKEND_BOOST_PROVIDER + AI_BACKEND_BOOST_MODEL is the failover target.
Both flow through the same ai_backend; the shim is only needed for camel-oasis
which speaks OpenAI ChatCompletion API (see openai-shim/).
"""
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# Wrapper lives in scripts/ (sibling to crucix_to_mirofish.py); make it importable
# from the backend package without duplicating the transport logic.
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[3] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from ai_backend_client import chat as _ai_backend_chat  # noqa: E402

from ..config import Config

logger = logging.getLogger("mirofish.llm_client")


# Errors that should trigger a failover to the boost provider. These are all
# upstream/provider-side issues — not bugs in the request itself. Transport
# errors (can't reach the API) AND content errors (LLM reachable but returning
# unusable output). Both indicate the primary path is degraded enough that
# the boost provider might do better.
_FAILOVER_ERRORS = (
    "APIConnectionError",
    "APITimeoutError",
    "RateLimitError",
    "InternalServerError",
    "ServiceUnavailableError",
    "BadRequestError",
)


class LLMClient:
    """LLM client with primary→boost failover, JSON repair, kimi temp override."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        # api_key / base_url / model are accepted for legacy callers but
        # ignored when AI_BACKEND_URL is set — the whole point is that we
        # route through ai_backend, not directly to providers.
        self.base_url = Config.AI_BACKEND_URL
        self.api_key = Config.AI_BACKEND_API_KEY
        self.provider_main = Config.AI_BACKEND_PROVIDER
        self.model_main = model or Config.AI_BACKEND_MODEL
        self.provider_boost = Config.AI_BACKEND_BOOST_PROVIDER
        self.model_boost = Config.AI_BACKEND_BOOST_MODEL

        if not self.base_url:
            raise ValueError("AI_BACKEND_URL 未配置")

    def _normalize_messages(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Strip empty assistant messages — CAMEL's re-formatting loop sometimes
        leaves an empty assistant message at position N, and the upstream
        rejects with 400. Triggers an infinite retry loop → round-0 hang.
        """
        cleaned = [m for m in messages if m.get("content") or m.get("role") == "system"]
        if len(cleaned) < len(messages):
            logger.debug(f"stripped {len(messages) - len(cleaned)} empty message(s) before sending")
        return cleaned

    def _temperature_override(self, model: str, temperature: float) -> float:
        """Some models (e.g. Kimi K2.5) only accept temperature=1."""
        if "kimi" in model.lower() or "k2" in model.lower():
            return 1.0
        return temperature

    def _do_chat(
        self,
        provider: str,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        response_format: Optional[Dict],
    ) -> Optional[str]:
        cleaned = self._normalize_messages(messages)
        temperature = self._temperature_override(model, temperature)
        try:
            return _ai_backend_chat(
                cleaned,
                provider=provider,
                model=model,
                base_url=self.base_url,
                api_key=self.api_key,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
            )
        except httpx.HTTPStatusError as exc:
            # ai_backend surfaces upstream errors verbatim. If we got 400 for an
            # empty message, retry once after aggressive strip (race with another
            # component injecting an empty message between filter and POST).
            err_str = str(exc)
            if "must not be empty" in err_str or "empty" in err_str.lower():
                logger.warning(f"got 400 for empty message on {model}, retrying after aggressive strip")
                aggressive = [m for m in cleaned if m.get("content", "").strip()]
                return _ai_backend_chat(
                    aggressive,
                    provider=provider,
                    model=model,
                    base_url=self.base_url,
                    api_key=self.api_key,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
            raise

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 8192,
        response_format: Optional[Dict] = None,
    ) -> Optional[str]:
        """Send chat request — primary provider with automatic failover to boost."""
        try:
            return self._do_chat(
                self.provider_main, self.model_main,
                messages, temperature, max_tokens, response_format,
            )
        except Exception as primary_err:
            err_name = type(primary_err).__name__
            if err_name not in _FAILOVER_ERRORS:
                raise
            logger.warning(
                f"Primary LLM failed ({err_name}: {primary_err}); "
                f"failing over to boost ({self.provider_boost}/{self.model_boost})"
            )
            return self._do_chat(
                self.provider_boost, self.model_boost,
                messages, temperature, max_tokens, response_format,
            )

    def _do_chat_json_boost(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        """Boost chat_json attempt — same logic as chat_json but using boost provider."""
        max_retries = 2
        for attempt in range(max_retries):
            response = self._do_chat(
                self.provider_boost, self.model_boost,
                messages, temperature, max_tokens, {"type": "json_object"},
            )
            if response is None or not response.strip():
                logger.warning(f"boost chat_json attempt {attempt+1}: empty response")
                continue
            cleaned = response.strip()
            cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r'\n?```\s*$', '', cleaned)
            cleaned = cleaned.strip()
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                repaired = self._repair_json(cleaned)
                if repaired is not None:
                    return repaired
        raise ValueError(f"Boost chat_json also failed after {max_retries} attempts")

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 8192,
    ) -> Dict[str, Any]:
        """Send chat request and return parsed JSON.

        Failover: if primary produces unusable output (None, empty, un-parseable
        after repair), retry with the boost provider if configured.
        """
        max_retries = 3
        last_error: Any = None
        for attempt in range(max_retries):
            try:
                response = self.chat(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                )
            except Exception as primary_err:
                err_name = type(primary_err).__name__
                if err_name in _FAILOVER_ERRORS:
                    logger.warning(
                        f"chat_json: primary LLM failed ({err_name}: {primary_err}); "
                        f"failing over to boost ({self.provider_boost}/{self.model_boost})"
                    )
                    try:
                        return self._do_chat_json_boost(messages, temperature, max_tokens)
                    except Exception as boost_err:
                        logger.error(f"chat_json: boost also failed: {boost_err}; re-raising primary")
                        raise primary_err
                raise

            if response is None or not response.strip():
                logger.warning(f"JSON chat attempt {attempt + 1}/{max_retries}: empty response")
                last_error = "LLM returned empty/None response"
                continue

            cleaned_response = response.strip()
            cleaned_response = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_response, flags=re.IGNORECASE)
            cleaned_response = re.sub(r'\n?```\s*$', '', cleaned_response)
            cleaned_response = cleaned_response.strip()

            try:
                return json.loads(cleaned_response)
            except json.JSONDecodeError:
                repaired = self._repair_json(cleaned_response)
                if repaired is not None:
                    return repaired
                last_error = cleaned_response
                logger.warning(f"JSON parse failed (attempt {attempt + 1}/{max_retries}), retrying...")

        if self.provider_boost:
            logger.warning(
                f"chat_json: primary LLM produced un-parseable output after {max_retries} attempts; "
                f"failing over to boost ({self.provider_boost}/{self.model_boost})"
            )
            try:
                return self._do_chat_json_boost(messages, temperature, max_tokens)
            except Exception as boost_err:
                logger.error(f"chat_json: boost also failed: {boost_err}")
        raise ValueError(f"LLM returned invalid JSON (after {max_retries} attempts): {last_error}")

    @staticmethod
    def _repair_json(text: str) -> Optional[Dict[str, Any]]:
        """Attempt to fix common JSON errors produced by LLMs (MiniMax M2.5/M2.7)."""
        repaired = text
        # Fix 1: ["key": value] -> "key": value  (stray [ before a key)
        repaired = re.sub(r'\[("[\w]+")\s*:', r'\1:', repaired)
        # Fix 1b: "text":"description": -> "text","description":
        repaired = re.sub(r'"text"\s*:\s*"description"\s*:', '"text","description":', repaired)
        # Fix 2: missing key before bare array value: ],\n  [...] -> ],\n  "examples": [...]
        repaired = re.sub(r'(\],)\s*\n(\s*)\[', r'\1\n\2"examples": [', repaired)
        # Fix 3: trailing commas before closing braces/brackets
        repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
        # Fix 4: single quotes used as string delimiters
        if repaired.count("'") > repaired.count('"'):
            repaired = repaired.replace("'", '"')

        if repaired != text:
            try:
                result = json.loads(repaired)
                logger.info("Successfully repaired malformed LLM JSON output")
                return result
            except json.JSONDecodeError as exc:
                logger.warning(f"First repair pass failed: {exc}, trying aggressive repair")

        lines = repaired.split('\n')
        fixed_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('["') and '":' in stripped:
                line = line.replace('["', '"', 1)
                if stripped.endswith(']') and stripped.count('[') < stripped.count(']'):
                    line = line.rstrip()
                    if line.endswith(']'):
                        line = line[:-1]
            fixed_lines.append(line)

        aggressive = '\n'.join(fixed_lines)
        aggressive = re.sub(r',\s*([}\]])', r'\1', aggressive)

        if aggressive != text:
            try:
                result = json.loads(aggressive)
                logger.info("Successfully repaired malformed LLM JSON (aggressive pass)")
                return result
            except json.JSONDecodeError:
                pass

        return None