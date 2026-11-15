"""Structural validation for the two envelope kinds."""

from __future__ import annotations

from typing import Any

from .errors import EgressError, RequestError


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_chat_request(body: Any) -> None:
    if not isinstance(body, dict):
        raise RequestError("request body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise RequestError("model must be a non-empty string")
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise RequestError("messages must be an array")
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            raise RequestError(f"messages[{index}] must be an object with a string role")


def validate_chat_response(body: Any) -> None:
    if not isinstance(body, dict):
        raise EgressError("response body must be a JSON object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise EgressError("choices must be a non-empty array")
    for index, choice in enumerate(choices):
        if not isinstance(choice, dict) or not _is_int(choice.get("index")):
            raise EgressError(f"choices[{index}] must have an integer index")
        if not isinstance(choice.get("message"), dict) and not isinstance(
            choice.get("delta"), dict
        ):
            raise EgressError(f"choices[{index}] must have a message or delta object")


def validate_body(kind: str, body: Any) -> None:
    if kind == "chat.request":
        validate_chat_request(body)
    elif kind == "chat.response":
        validate_chat_response(body)
    else:
        raise EgressError(f"unknown envelope kind {kind!r}")
