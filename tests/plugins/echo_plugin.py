"""A configurable protocol-conforming plugin used by the host tests."""

from __future__ import annotations

import json
import os
import sys
import time

CONFIG: dict = {}
STATE: dict = {}


def read_frame():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    if length is None:
        return None
    return json.loads(sys.stdin.buffer.read(length))


def write_frame(obj):
    data = json.dumps(obj).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    sys.stdout.buffer.flush()


def main() -> None:
    global CONFIG
    for frame in iter(read_frame, None):
        method = frame.get("method")
        rid = frame.get("id")
        if method == "plugin/describe":
            result = {
                "id": CONFIG.get("describe_id", "acme/echo"),
                "version": "0.2.0",
                "contract": "0.2",
                "hooks": {
                    "server_bound": ["request"],
                    "client_bound": ["response"],
                },
                "read_only": os.environ.get("TEST_PLUGIN_READ_ONLY") == "true",
            }
        elif method == "plugin/init":
            CONFIG = (frame.get("params") or {}).get("config") or {}
            result = {"ok": True}
        elif method == "plugin/shutdown":
            write_frame({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})
            return
        elif method == "$/ping":
            write_frame({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})
            continue
        elif method == "hook":
            mode = CONFIG.get("mode", "continue")
            if mode == "sleep":
                time.sleep(float(CONFIG.get("delay_ms", 500)) / 1000.0)
                result = {"action": "continue"}
            elif mode == "crash":
                os._exit(3)
            elif mode == "invalid":
                result = {"action": "bogus"}
            elif mode == "patch":
                result = {"action": "patch", "patch": CONFIG.get("patch", {})}
            elif mode == "state_patch":
                params = frame.get("params") or {}
                result = {
                    "action": "patch",
                    "patch": {"x_state": params.get("state", {})},
                }
            elif mode == "context_patch":
                params = frame.get("params") or {}
                result = {
                    "action": "patch",
                    "patch": {"x_context": params.get("context")},
                }
            elif mode == "meta_patch":
                params = frame.get("params") or {}
                result = {
                    "action": "patch",
                    "patch": {"x_meta": (params.get("envelope") or {}).get("meta")},
                }
            elif mode == "attempt_gate":
                params = frame.get("params") or {}
                delay = float(CONFIG.get("delay_ms", 0)) / 1000.0
                if delay:
                    time.sleep(delay)
                if int(params.get("attempt", 0)) < int(CONFIG.get("fail_attempts", 1)):
                    result = {
                        "action": "terminate",
                        "violations": CONFIG.get("violations", []),
                    }
                else:
                    result = {"action": "continue"}
            elif mode == "direction_gate":
                params = frame.get("params") or {}
                delay = float(CONFIG.get("delay_ms", 0)) / 1000.0
                if delay:
                    time.sleep(delay)
                failing = params.get("direction") == CONFIG.get(
                    "fail_direction", "client_bound"
                )
                if failing and int(params.get("attempt", 0)) < int(
                    CONFIG.get("fail_attempts", 1)
                ):
                    result = {
                        "action": "terminate",
                        "violations": CONFIG.get("violations", []),
                    }
                else:
                    result = {"action": "continue"}
            elif mode == "attempt_patch":
                params = frame.get("params") or {}
                result = {
                    "action": "patch",
                    "patch": {
                        "x_attempt": params.get("attempt"),
                        "x_context": params.get("context"),
                    },
                }
            elif mode == "replace":
                result = {"action": "replace", "body": CONFIG.get("body", {})}
            elif mode == "terminate":
                result = {
                    "action": "terminate",
                    "violations": CONFIG.get("violations", []),
                }
            elif mode == "error":
                write_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32603, "message": "boom"},
                    }
                )
                continue
            else:
                result = {"action": "continue"}
            if CONFIG.get("state_set") or CONFIG.get("state_delete"):
                result["state"] = {}
                if CONFIG.get("state_set"):
                    result["state"]["set"] = CONFIG["state_set"]
                if CONFIG.get("state_delete"):
                    result["state"]["delete"] = CONFIG["state_delete"]
            if CONFIG.get("echo_state"):
                result["details"] = STATE
        else:
            write_frame(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": "method not found"},
                }
            )
            continue
        write_frame({"jsonrpc": "2.0", "id": rid, "result": result})


if __name__ == "__main__":
    main()
