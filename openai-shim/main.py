"""OpenAI ChatCompletion → ai_backend /callAI shim.

camel-ai (via camel-oasis) speaks the OpenAI ChatCompletion API. ai_backend uses
its own /callAI schema. This shim bridges them: accepts OpenAI-format requests,
forwards to ai_backend, returns OpenAI-format responses.

The model field is forwarded verbatim to ai_backend's `metadata.model`; the
provider is fixed at startup via SHIM_PROVIDER env (default: minimax).
"""
import json
import logging
import os
import time
import uuid
from typing import Any, List, Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("openai-shim")

AI_BACKEND_URL = os.getenv("AI_BACKEND_URL", "http://host.docker.internal:9400").rstrip("/")
AI_BACKEND_API_KEY = os.getenv("AI_BACKEND_API_KEY", "")
SHIM_PROVIDER = os.getenv("SHIM_PROVIDER", "minimax")
SHIM_MODEL = os.getenv("SHIM_MODEL", "MiniMax-M3")
SHIM_LISTEN_PORT = int(os.getenv("SHIM_LISTEN_PORT", "9401"))

app = FastAPI(title="openai-ai-backend-shim", version="1.0.0")


class ChatMessage(BaseModel):
    role: str
    content: Optional[str] = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = 1.0
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[Any] = None
    stream: Optional[bool] = False
    tools: Optional[List[dict]] = None
    tool_choice: Optional[Any] = None
    response_format: Optional[dict] = None
    user: Optional[str] = None


def _to_ai_backend_payload(req: ChatCompletionRequest) -> dict:
    metadata: dict[str, Any] = {
        "model": req.model or SHIM_MODEL,
        "temperature": req.temperature if req.temperature is not None else 1.0,
    }
    if req.max_tokens is not None:
        metadata["max_tokens"] = req.max_tokens
    if req.top_p is not None:
        metadata["top_p"] = req.top_p
    if req.frequency_penalty is not None:
        metadata["frequency_penalty"] = req.frequency_penalty
    if req.presence_penalty is not None:
        metadata["presence_penalty"] = req.presence_penalty
    if req.stop is not None:
        metadata["stop"] = req.stop
    if req.response_format is not None:
        metadata["response_format"] = req.response_format
    if req.tools is not None:
        metadata["tools"] = req.tools
    if req.tool_choice is not None:
        metadata["tool_choice"] = req.tool_choice
    metadata["thinking"] = {"type": "disabled"}

    return {
        "provider": SHIM_PROVIDER,
        "prompt": [m.model_dump(exclude_none=True) for m in req.messages],
        "task": "chat",
        "metadata": metadata,
    }


def _to_openai_response(payload: dict, model: str) -> dict:
    """Translate ai_backend /callAI response → OpenAI ChatCompletion shape."""
    result = payload.get("result") or {}
    text = result.get("text") or payload.get("text") or ""
    tool_calls = result.get("tool_calls") or payload.get("tool_calls") or []
    usage = payload.get("metadata", {}).get("usage") or payload.get("usage") or {}
    upstream_model = payload.get("model") or model

    openai_tool_calls = []
    for tc in tool_calls:
        openai_tool_calls.append({
            "id": tc.get("id", f"call_{uuid.uuid4().hex[:24]}"),
            "type": "function",
            "function": {
                "name": tc.get("name", ""),
                "arguments": json.dumps(tc.get("input", {}))
                if not isinstance(tc.get("input"), str)
                else tc["input"],
            },
        })

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": upstream_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text if not openai_tool_calls else None,
                    "tool_calls": openai_tool_calls or None,
                },
                "finish_reason": "tool_calls" if openai_tool_calls else "stop",
                "logprobs": None,
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "ai_backend_url": AI_BACKEND_URL,
        "provider": SHIM_PROVIDER,
        "model": SHIM_MODEL,
        "listen_port": SHIM_LISTEN_PORT,
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    if req.stream:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "streaming not supported by this shim", "type": "unsupported"}},
        )

    body = _to_ai_backend_payload(req)
    headers = {"Content-Type": "application/json"}
    if AI_BACKEND_API_KEY:
        headers["X-API-Key"] = AI_BACKEND_API_KEY

    try:
        upstream = httpx.post(
            f"{AI_BACKEND_URL}/callAI",
            json=body,
            headers=headers,
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        logger.error(f"ai_backend unreachable: {exc}")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"ai_backend unreachable: {exc}", "type": "upstream_error"}},
        )

    if upstream.status_code >= 400:
        detail = upstream.text[:500]
        logger.error(f"ai_backend returned {upstream.status_code}: {detail}")
        return JSONResponse(
            status_code=upstream.status_code,
            content={"error": {"message": detail, "type": "upstream_error"}},
        )

    try:
        payload = upstream.json()
    except json.JSONDecodeError as exc:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"ai_backend returned non-JSON: {exc}", "type": "upstream_error"}},
        )

    return _to_openai_response(payload, req.model)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SHIM_LISTEN_PORT, log_level="info")