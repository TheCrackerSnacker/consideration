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
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first.get("message")
    if not isinstance(message, dict):
        message = first.get("delta") if isinstance(first.get("delta"), dict) else {}

    base = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }
    opening = dict(base)
    opening["choices"] = [
        {
            "index": 0,
            "delta": {"role": "assistant", "content": ""},
            "finish_reason": None,
        }
    ]
    delta: dict[str, Any] = {"content": _text(message.get("content"))}
    if message.get("tool_calls"):
        delta["tool_calls"] = message["tool_calls"]
    content_chunk = dict(base)
    content_chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": None}]
    closing = dict(base)
    closing["choices"] = [
        {
            "index": 0,
            "delta": {},
            "finish_reason": first.get("finish_reason"),
        }
    ]

    chunks = [opening, content_chunk, closing]
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
