"""Small JSON-RPC/LSP framing helpers shared by reference plugins."""

from __future__ import annotations

import json
import sys


def read_frame():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    if length is None:
        return None
    return json.loads(sys.stdin.buffer.read(length))


def write_frame(value):
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    sys.stdout.buffer.flush()


def serve(plugin_id, hooks, handler, *, read_only=False):
    config = {}
    for frame in iter(read_frame, None):
        method = frame.get("method")
        request_id = frame.get("id")
        if method == "plugin/describe":
            result = {
                "id": plugin_id,
                "version": "0.2.0",
                "contract": "0.2",
                "hooks": hooks,
                "read_only": read_only,
            }
        elif method == "plugin/init":
            config = (frame.get("params") or {}).get("config") or {}
            result = {"ok": True}
        elif method == "plugin/shutdown":
            write_frame({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})
            return
        elif method == "$/ping":
            write_frame({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})
            continue
        elif method == "hook":
            result = handler(frame.get("params") or {}, config)
        else:
            write_frame(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "method not found"},
                }
            )
            continue
        write_frame({"jsonrpc": "2.0", "id": request_id, "result": result})
