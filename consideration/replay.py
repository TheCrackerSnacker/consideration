"""Offline replay for journal entries that include request/response bodies."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .app import App, InboundRequest
from .config import ConfigError, load_config
from .plugin import PluginManager
from .upstream import UpstreamResponse


class ReplayUpstream:
    def __init__(self, record: dict[str, Any]):
        bodies = record.get("bodies") or {}
        responses = bodies.get("server_responses") or []
        self.responses = [item.get("body") for item in responses if isinstance(item, dict)]
        # Older records with a single accepted response remain replayable.
        if not self.responses and isinstance(bodies.get("response"), dict):
            self.responses = [bodies["response"].get("body")]
        self.index = 0

    def request(self, method, path, client_headers, body, timeout_ms):
        if self.index >= len(self.responses):
            raise RuntimeError("journal does not contain enough upstream response bodies")
        value = self.responses[self.index]
        self.index += 1
        if not isinstance(value, dict):
            raise RuntimeError("journal upstream response body is not an object")
        return UpstreamResponse(200, [("content-type", "application/json")], json.dumps(value).encode())


def load_entry(path: str | Path, line: int = -1) -> dict[str, Any]:
    lines = [item for item in Path(path).read_text(encoding="utf-8").splitlines() if item.strip()]
    if not lines:
        raise ValueError("journal is empty")
    try:
        value = json.loads(lines[line])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError("invalid journal entry") from exc
    if not isinstance(value, dict):
        raise ValueError("journal entry must be an object")
    return value


def replay(config_path: str | Path, record_path: str | Path, line: int = -1) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_config(config_path)
    original = load_entry(record_path, line)
    if original.get("config_hash") != config.config_hash:
        raise ValueError("journal config_hash does not match the supplied configuration")
    bodies = original.get("bodies")
    if not isinstance(bodies, dict) or not isinstance(bodies.get("request"), dict):
        raise ValueError("journal entry must have include_bodies request data")
    request_body = bodies["request"].get("body")
    if not isinstance(request_body, dict):
        raise ValueError("journal request body is unavailable or truncated")
    request_meta = bodies.get("request_meta") or {}
    response_meta = bodies.get("response_meta") or {}

    manager = PluginManager(config, telemetry=None)
    try:
        manager.start_all()
        sink = _MemoryJournal()
        app = App(config, manager, telemetry=None, journal=sink, upstream=ReplayUpstream(original))
        app.handle(InboundRequest(
            method="POST", path="/v1/chat/completions",
            body=json.dumps(request_body).encode(),
            request_id=original.get("request_id"),
            replay_started_at=original.get("started_at"),
            replay_response_received_at=response_meta.get("received_at"),
            headers=dict(request_meta.get("headers") or {"content-type": "application/json"}),
            remote_addr=str(request_meta.get("remote_addr", "replay")),
        ))
        if sink.record is None:
            raise ValueError("replay did not produce a journal record")
        return original, sink.record
    finally:
        manager.shutdown_all()


class _MemoryJournal:
    def __init__(self):
        self.record = None

    def write(self, record):
        self.record = record


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items() if key not in {"request_id", "started_at", "duration_ms", "response_bytes"}}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="consideration-replay")
    parser.add_argument("--config", "-c", required=True)
    parser.add_argument("--journal", "-j", required=True)
    parser.add_argument("--line", type=int, default=-1, help="zero-based JSONL line (default: last)")
    args = parser.parse_args(argv)
    try:
        original, replayed = replay(args.config, args.journal, args.line)
    except (ConfigError, OSError, ValueError, RuntimeError) as exc:
        print(f"consideration-replay: {exc}", file=sys.stderr)
        return 2
    if _stable(original) != _stable(replayed):
        print(json.dumps({"match": False, "record": replayed}, ensure_ascii=False))
        return 1
    print(json.dumps({"match": True, "request_id": original.get("request_id")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
