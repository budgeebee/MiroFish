"""
LLM客户端封装
统一使用OpenAI格式调用
"""

import json
import re
from typing import Optional, Dict, Any, List
from openai import OpenAI

from ..config import Config


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
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> str:
        """
        发送聊天请求
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            response_format: 响应格式（如JSON模式）
            
        Returns:
            模型响应文本
        """
        # Some models (e.g. Kimi K2.5) only accept temperature=1
        if "kimi" in self.model.lower() or "k2" in self.model.lower():
            temperature = 1.0

        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        if response_format:
            kwargs["response_format"] = response_format
        
        response = self.client.chat.completions.create(**kwargs)
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
            max_tokens: 最大token数
            
        Returns:
            解析后的JSON对象
        """
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            response = self.chat(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"}
            )
            if response is None:
                import logging
                logger = logging.getLogger('mirofish.llm_client')
                logger.warning(
                    f"JSON chat attempt {attempt + 1}/{max_retries}: LLM returned None, retrying..."
                )
                last_error = "LLM returned None response"
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
                import logging
                logger = logging.getLogger('mirofish.llm_client')
                logger.warning(
                    f"JSON parse failed (attempt {attempt + 1}/{max_retries}), retrying..."
                )
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

