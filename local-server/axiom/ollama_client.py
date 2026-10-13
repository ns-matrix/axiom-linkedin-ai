"""Thin Ollama client with local-first defaults and clear failure modes."""

from __future__ import annotations

import os
from typing import Any

import httpx

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

VISION_TOKENS = ("moondream", "llava", "vision", "clip")


class OllamaError(Exception):
    """Raised when Ollama cannot be reached or returns something unusable."""


def _timeout() -> float:
    try:
        return float(os.getenv("OLLAMA_TIMEOUT", "180"))
    except ValueError:
        return 180.0


async def list_models() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{OLLAMA_URL}/api/tags")
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # httpx errors, bad JSON, etc.
        raise OllamaError(f"Ollama unavailable at {OLLAMA_URL}: {exc}") from exc
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


async def pick_model(preferred: str | None = None) -> str:
    models = await list_models()
    if not models:
        raise OllamaError("Ollama is online but no model is installed.")
    if preferred and preferred in models:
        return preferred
    text_models = [m for m in models if not any(t in m.lower() for t in VISION_TOKENS)]
    if not text_models:
        raise OllamaError("Only vision models are installed; a text model is required.")
    return text_models[0]


async def chat(messages: list[dict[str, str]], model: str | None = None) -> tuple[str, str]:
    """Run one non-streaming chat completion. Returns (content, model_used)."""
    chosen = await pick_model(model)
    payload: dict[str, Any] = {"model": chosen, "messages": messages, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        raise OllamaError(f"Ollama chat failed: {exc}") from exc
    content = data.get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise OllamaError("Ollama returned an empty response.")
    return content, chosen


def extract_json(text: str) -> Any | None:
    """Pull the first JSON object/array out of a model reply (with or without fences)."""
    import json

    text = text.strip()
    if not text:
        return None
    fence_start = text.find("```")
    if fence_start != -1:
        end = text.find("```", fence_start + 3)
        block = text[fence_start + 3 : end if end != -1 else len(text)]
        block = block.lstrip()
        if block.lower().startswith("json"):
            block = block[4:]
        text = block.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = min([i for i in (text.find("{"), text.find("[")) if i != -1], default=-1)
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    for end in range(len(text), start, -1):
        try:
            obj, _ = decoder.raw_decode(text[start:end])
            return obj
        except ValueError:
            continue
    return None
