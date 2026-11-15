from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from common import serve


DEFAULT_PATTERNS = [r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", r"\b\d{3}[-.]\d{3}[-.]\d{4}\b"]


def token(value: str) -> str:
    # A short deterministic digest is stable across turns without persistence.
    number = int(hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()[:10], 16)
    return f"[[P{number}]]"


def transform_text(text: str, patterns, replacements):
    for pattern in patterns:
        text = re.sub(pattern, lambda match: replacements.setdefault(token(match.group(0)), match.group(0)) and token(match.group(0)), text, flags=re.IGNORECASE)
    return text


def handler(params, config):
    direction = params.get("direction")
    envelope = params.get("envelope") or {}
    body = envelope.get("body") or {}
    patterns = config.get("patterns", DEFAULT_PATTERNS)
    state = params.get("state") or {}
    mapping = dict(state.get("entities") or {})
    if direction == "server_bound":
        for message in body.get("messages", []):
            if isinstance(message.get("content"), str):
                message["content"] = transform_text(message["content"], patterns, mapping)
        return {"action": "replace", "body": body, "state": {"set": {"entities": mapping}}}
    if direction == "client_bound":
        reverse = {placeholder: value for placeholder, value in mapping.items()}
        for choice in body.get("choices", []):
            message = choice.get("message") or {}
            if isinstance(message.get("content"), str):
                for placeholder, value in reverse.items():
                    message["content"] = message["content"].replace(placeholder, value)
        return {"action": "replace", "body": body}
    return {"action": "continue"}


serve("acme/pii-mask", {"server_bound": ["request"], "client_bound": ["response"]}, handler)
