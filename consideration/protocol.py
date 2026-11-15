"""LSP-style Content-Length framing and JSON-RPC helpers."""

from __future__ import annotations

import json
from typing import Any, BinaryIO

from .errors import PluginProtocolError


def write_frame(stream: BinaryIO, obj: dict[str, Any]) -> None:
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(b"Content-Length: %d\r\n\r\n" % len(data))
    stream.write(data)
    stream.flush()


def read_frame(stream: BinaryIO, max_bytes: int) -> dict[str, Any] | None:
    length: int | None = None
    while True:
        line = stream.readline()
        if line == b"":
            if length is None:
                return None
            raise PluginProtocolError("unexpected EOF while reading frame headers")
        if line in (b"\r\n", b"\n"):
            break
        if b":" not in line:
            raise PluginProtocolError("malformed frame header")
        name, value = line.split(b":", 1)
        if name.strip().lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError as exc:
                raise PluginProtocolError("invalid Content-Length") from exc
    if length is None:
        raise PluginProtocolError("frame is missing Content-Length")
    if length < 0 or length > max_bytes:
        raise PluginProtocolError(
            f"frame length {length} exceeds the configured limit of {max_bytes}"
        )
    data = stream.read(length)
    if len(data) != length:
        raise PluginProtocolError("short frame body")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginProtocolError(f"frame is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PluginProtocolError("frame must contain a JSON object")
    return value
