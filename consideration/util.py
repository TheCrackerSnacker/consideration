"""Small shared helpers."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import uuid
from typing import Any


def now_iso() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex


def new_namespace_id() -> str:
    return "ns_" + uuid.uuid4().hex


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_hash(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def deep_copy(value: Any) -> Any:
    """Copy JSON-shaped data without sharing mutable structures."""
    return json.loads(json.dumps(value, ensure_ascii=False))


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def truncate_bytes(text: str, limit: int) -> tuple[str, bool]:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", errors="replace"), True
