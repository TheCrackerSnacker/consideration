"""Buffered streaming: synthesize a conforming SSE stream (section 12)."""

from __future__ import annotations

import json
from typing import Any


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


def synthesize_sse(body: dict[str, Any], include_usage: bool = False) -> bytes:
    chunk_id = body.get("id", "")
    created = body.get("created", 0)
    model = body.get("model", "")
    choices = body.get("choices") or []
    base = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }
    openings = []
    contents = []
    closings = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        index = choice.get("index", 0)
        message = choice.get("message")
        is_message = isinstance(message, dict)
        if not is_message:
            message = choice.get("delta")
        if not isinstance(message, dict):
            continue
        openings.append(
            {
                "index": index,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }
        )
        delta: dict[str, Any] = {"content": _text(message.get("content"))}
        if message.get("tool_calls"):
            delta["tool_calls"] = message["tool_calls"]
        if message.get("function_call"):
            delta["function_call"] = message["function_call"]
        if message.get("refusal"):
            delta["refusal"] = message["refusal"]
        contents.append({"index": index, "delta": delta, "finish_reason": None})
        closings.append(
            {
                "index": index,
                "delta": {},
                "finish_reason": choice.get("finish_reason"),
            }
        )

    chunks = []
    for choice_chunks in (openings, contents, closings):
        if choice_chunks:
            chunk = dict(base)
            chunk["choices"] = choice_chunks
            chunks.append(chunk)
    if include_usage:
        usage_chunk = dict(base)
        usage_chunk["choices"] = []
        usage_chunk["usage"] = body.get("usage")
        chunks.append(usage_chunk)

    output = bytearray()
    for chunk in chunks:
        output += (
            b"data: "
            + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n\n"
        )
    output += b"data: [DONE]\n\n"
    return bytes(output)
