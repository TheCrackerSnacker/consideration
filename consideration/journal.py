"""Append-only JSONL journal with daily rotation."""

from __future__ import annotations

import datetime as _dt
import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import JournalConfig


class JournalRecord:
    """Mutable request journal assembled during processing and serialized once."""

    def __init__(
        self,
        *,
        request_id: str,
        config_hash: str,
        started_at: str,
        started_monotonic: float,
        client_streamed: bool,
    ) -> None:
        self.request_id = request_id
        self.config_hash = config_hash
        self.started_at = started_at
        self.started_monotonic = started_monotonic
        self.client_streamed = client_streamed
        self.direction_results: dict[str, Any] = {}
        self.server_requests: list[dict[str, Any]] = []
        self.repairs: list[dict[str, Any]] = []
        self.state_writes: list[dict[str, Any]] = []
        self.bodies: dict[str, Any] | None = None
        self.passthrough: dict[str, Any] | None = None

    def set_bodies(self, bodies: dict[str, Any]) -> None:
        self.bodies = bodies

    def set_direction_result(self, direction: str, result: dict[str, Any]) -> None:
        self.direction_results[direction] = result

    def add_server_request(self, request: dict[str, Any]) -> None:
        self.server_requests.append(request)

    def add_state_writes(self, writes: list[dict[str, Any]]) -> None:
        self.state_writes.extend(writes)

    def set_repairs(self, repairs: list[dict[str, Any]]) -> None:
        self.repairs = repairs

    def set_passthrough(self, method: str, path: str) -> None:
        self.passthrough = {"method": method, "path": path}

    def set_passthrough_status(self, status: int) -> None:
        if self.passthrough is None:
            raise RuntimeError("passthrough metadata has not been initialized")
        self.passthrough["status"] = status

    def serialize(self, response_bytes: int) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema": 1,
            "request_id": self.request_id,
            "config_hash": self.config_hash,
            "started_at": self.started_at,
            "duration_ms": int((time.monotonic() - self.started_monotonic) * 1000),
            "client_streamed": self.client_streamed,
            "direction_results": self.direction_results,
            "server": {"requests": self.server_requests},
            "repairs": self.repairs,
            "state_writes": self.state_writes,
            "response_bytes": response_bytes,
        }
        if self.bodies is not None:
            record["bodies"] = self.bodies
        if self.passthrough is not None:
            record["passthrough"] = self.passthrough
        return record


class Journal:
    def __init__(self, config: JournalConfig):
        self.config = config
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        if not self.config.enabled:
            return
        path = Path(self.config.path)
        path.mkdir(parents=True, exist_ok=True)
        day = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")
        target = path / f"journal-{day}.jsonl"
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock, target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
