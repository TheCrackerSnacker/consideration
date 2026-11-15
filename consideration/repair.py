"""Host-authored repair requests (section 10.3)."""

from __future__ import annotations

from typing import Any

from .util import deep_copy


def all_retryable(violations: list[dict[str, Any]]) -> bool:
    return bool(violations) and all(bool(v.get("retryable")) for v in violations)


def resolve_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        return None
    current = document
    for raw in pointer.split("/")[1:]:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def span_excerpt(body: dict[str, Any], span: dict[str, Any]) -> str | None:
    value = resolve_pointer(body, str(span.get("path", "")))
    if not isinstance(value, str):
        return None
    start = span.get("start")
    end = span.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    data = value.encode("utf-8")
    if start < 0 or end < start or start > len(data):
        return None
    return data[start:end].decode("utf-8", errors="replace")


def render_instruction(rejected_body: dict[str, Any], violations: list[dict[str, Any]]) -> str:
    lines = ["Your previous response was rejected by policy.", "", "Violations:"]
    for violation in violations:
        lines.append(
            f"- {violation['code']} ({violation['severity']}): {violation['message']}"
        )
        for span in violation.get("spans") or []:
            excerpt = span_excerpt(rejected_body, span)
            if excerpt is None:
                continue
            start = span.get("start")
            end = span.get("end")
            import json

            lines.append(
                f"  At {span['path']} [{start},{end}): "
                f"{json.dumps(excerpt, ensure_ascii=False)}"
            )
        hint = violation.get("hint")
        if hint:
            lines.append(f"  Hint: {hint}")
    lines += [
        "",
        "Return a complete replacement assistant turn. Do not describe the rejection.",
    ]
    return "\n".join(lines)


def rejected_message(response_body: dict[str, Any]) -> dict[str, Any]:
    choices = response_body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return {"role": "assistant", "content": None}
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        message = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
    content = message.get("content")
    result: dict[str, Any] = {"role": "assistant", "content": content}
    if "tool_calls" in message:
        result["tool_calls"] = deep_copy(message["tool_calls"])
    return result


def build_repair_request(
    server_request: dict[str, Any],
    rejected_body: dict[str, Any],
    violations: list[dict[str, Any]],
) -> dict[str, Any]:
    request = deep_copy(server_request)
    request["stream"] = False
    request.pop("stream_options", None)
    messages = request.get("messages")
    if not isinstance(messages, list):
        messages = []
        request["messages"] = messages
    messages.append(rejected_message(rejected_body))
    messages.append(
        {"role": "user", "content": render_instruction(rejected_body, violations)}
    )
    return request
