"""
LLM客户端封装
统一使用OpenAI格式调用
"""

import json
import logging
import re
from typing import Optional, Dict, Any, List
from openai import OpenAI

from ..config import Config

logger = logging.getLogger('mirofish.llm_client')


# Errors that should trigger a failover to LLM_BOOST. These are all
# upstream/provider-side issues — not bugs in the request itself.
# Errors that should trigger a failover to LLM_BOOST. Transport errors
# (can't reach the API) AND content errors (LLM reachable but returning
# unusable output — e.g. invalid JSON, empty response). Both indicate the
# primary LLM is degraded enough that the boost LLM might do better.
_FAILOVER_ERRORS = (
    "APIConnectionError",
    "APITimeoutError",
    "RateLimitError",
    "InternalServerError",
    "ServiceUnavailableError",
    # Content errors — LLM reachable but output unusable
    "BadRequestError",  # 400 — often malformed requests after model updates
)


class LLMClient:
    """LLM客户端"""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None
    ):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model = model or Config.LLM_MODEL_NAME
        
        if not self.api_key:
            raise ValueError("LLM_API_KEY 未配置")
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )

        # Lazy-init boost client. Built only on first failover so a misconfigured
        # boost env doesn't block the primary path. Boost is a separate OpenAI
        # client using LLM_BOOST_* env vars (already wired in config.py).
        self._boost_client = None
        self._boost_model = None
    
    def _ensure_boost(self):
        """Lazily build the boost (failover) client. Returns True if available."""
        if self._boost_client is not None:
            return True
        boost_key = getattr(Config, 'LLM_BOOST_API_KEY', None)
        boost_url = getattr(Config, 'LLM_BOOST_BASE_URL', None)
        boost_model = getattr(Config, 'LLM_BOOST_MODEL_NAME', None)
        if not boost_key or boost_key == Config.LLM_API_KEY:
            # No separate boost configured — can't failover.
            return False
        self._boost_model = boost_model or Config.LLM_MODEL_NAME
        self._boost_client = OpenAI(api_key=boost_key, base_url=boost_url)
        logger.info(
            f"LLM_BOOST failover armed: model={self._boost_model} url={boost_url}"
        )
        return True
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> str:
        """
        发送聊天请求 — primary LLM with automatic failover to LLM_BOOST on
        upstream errors (rate-limit, timeout, connection, 5xx).
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token限制
            response_format: 响应格式（如JSON模式）
            
        Returns:
            模型响应文本
        """
        try:
            return self._do_chat(self.client, self.model, messages, temperature, max_tokens, response_format)
        except Exception as primary_err:
            err_name = type(primary_err).__name__
            if err_name not in _FAILOVER_ERRORS:
                raise
            if not self._ensure_boost():
                logger.warning(f"Primary LLM failed ({err_name}) but no LLM_BOOST configured — raising")
                raise
            logger.warning(
                f"Primary LLM failed ({err_name}: {primary_err}); failing over to LLM_BOOST ({self._boost_model})"
            )
            return self._do_chat(
                self._boost_client, self._boost_model, messages, temperature, max_tokens, response_format
            )
    
    def _do_chat_json_boost(self, messages, temperature, max_tokens):
        """Boost LLM chat_json attempt — same logic as chat_json but using boost client."""
        import logging
        logger = logging.getLogger('mirofish.llm_client')
        max_retries = 2
        for attempt in range(max_retries):
            response = self._do_chat(
                self._boost_client, self._boost_model,
                messages, temperature, max_tokens, {"type": "json_object"}
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
        raise ValueError(f"LLM_BOOST chat_json also failed after {max_retries} attempts")

    def _do_chat(self, client, model, messages, temperature, max_tokens, response_format):
        """Single chat attempt against a specific client/model."""
        # Some models (e.g. Kimi K2.5) only accept temperature=1
        if "kimi" in model.lower() or "k2" in model.lower():
            temperature = 1.0

        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        if response_format:
            kwargs["response_format"] = response_format
        
        response = client.chat.completions.create(**kwargs)
        if not response.choices:
            return None
        content = response.choices[0].message.content
        if content is None:
            return None
        # 部分模型（如MiniMax M2.5）会在content中包含<think>思考内容，需要移除
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        return content
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """
        发送聊天请求并返回JSON

        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token限制

        Returns:
            解析后的JSON对象

        Failover: if the primary LLM produces unusable output (None response,
        empty string, un-parseable JSON after repair), retry with the LLM_BOOST
        fallback if configured. This catches the silent-failure mode where the
        primary LLM is reachable but degraded.
        """
        import logging
        logger = logging.getLogger('mirofish.llm_client')
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                response = self.chat(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"}
                )
            except Exception as primary_err:
                err_name = type(primary_err).__name__
                if err_name in _FAILOVER_ERRORS and self._ensure_boost():
                    logger.warning(
                        f"chat_json: primary LLM failed ({err_name}: {primary_err}); "
                        f"failing over to LLM_BOOST ({self._boost_model})"
                    )
                    try:
                        return self._do_chat_json_boost(messages, temperature, max_tokens)
                    except Exception as boost_err:
                        logger.error(f"chat_json: boost also failed: {boost_err}; re-raising primary")
                        raise primary_err
                raise

            if response is None or not response.strip():
                logger.warning(
                    f"JSON chat attempt {attempt + 1}/{max_retries}: LLM returned empty response"
                )
                last_error = "LLM returned empty/None response"
                continue
            # 清理markdown代码块标记
            cleaned_response = response.strip()
            cleaned_response = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_response, flags=re.IGNORECASE)
            cleaned_response = re.sub(r'\n?```\s*$', '', cleaned_response)
            cleaned_response = cleaned_response.strip()

            try:
                return json.loads(cleaned_response)
            except json.JSONDecodeError:
                # Attempt to repair common LLM JSON mistakes
                repaired = self._repair_json(cleaned_response)
                if repaired is not None:
                    return repaired
                last_error = cleaned_response
                logger.warning(
                    f"JSON parse failed (attempt {attempt + 1}/{max_retries}), retrying..."
                )

        # All retries exhausted on primary. Try boost LLM as last resort.
        if self._ensure_boost():
            logger.warning(
                f"chat_json: primary LLM produced un-parseable output after {max_retries} attempts; "
                f"failing over to LLM_BOOST ({self._boost_model})"
            )
            try:
                return self._do_chat_json_boost(messages, temperature, max_tokens)
            except Exception as boost_err:
                logger.error(f"chat_json: boost also failed: {boost_err}")
        raise ValueError(f"LLM返回的JSON格式无效 (after {max_retries} attempts): {last_error}")

    @staticmethod
    def _repair_json(text: str):
        """Attempt to fix common JSON errors produced by LLMs.

        Applies repairs iteratively until JSON parses or no more fixes apply.
        """
        import logging
        logger = logging.getLogger('mirofish.llm_client')

        repaired = text

        # Fix 1: ["key": value] -> "key": value  (stray [ before a key)
        # e.g. ["examples": [...]] -> "examples": [...]
        repaired = re.sub(
            r'\[("[\w]+")\s*:',
            r'\1:',
            repaired,
        )

        # Fix 1b: "type":"text":"description": -> "type":"text","description":
        # LLM uses colon instead of comma between key-value pairs
        repaired = re.sub(
            r'"text"\s*:\s*"description"\s*:',
            '"text","description":',
            repaired,
        )

        # Fix 2: missing key before bare array value: ],\n  [...] -> ],\n  "examples": [...]
        repaired = re.sub(
            r'(\],)\s*\n(\s*)\[',
            r'\1\n\2"examples": [',
            repaired,
        )

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
            except json.JSONDecodeError as e:
                logger.warning(f"First repair pass failed: {e}, trying aggressive repair")

        # Aggressive: line-by-line repair for remaining issues
        lines = repaired.split('\n')
        fixed_lines = []
        for line in lines:
            # Fix orphaned brackets: a line that is just `[` or starts with `[" `
            # after a line ending with `],`
            stripped = line.strip()
            if stripped.startswith('["') and '":' in stripped:
                # ["key": value] pattern on its own line
                line = line.replace('["', '"', 1)
                # Remove the matching trailing ] if it closes this malformed bracket
                if stripped.endswith(']') and stripped.count('[') < stripped.count(']'):
                    line = line.rstrip()
                    if line.endswith(']'):
                        line = line[:-1]
            fixed_lines.append(line)

        aggressive = '\n'.join(fixed_lines)
        # Re-apply trailing comma fix
        aggressive = re.sub(r',\s*([}\]])', r'\1', aggressive)

        if aggressive != text:
            try:
                result = json.loads(aggressive)
                logger.info("Successfully repaired malformed LLM JSON (aggressive pass)")
                return result
            except json.JSONDecodeError:
                pass

        return None

