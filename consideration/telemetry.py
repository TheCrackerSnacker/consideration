"""Host observer telemetry with a JSONL exporter."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .config import TelemetryConfig
from .util import now_iso


class Telemetry:
    def __init__(self, config: TelemetryConfig):
        self.config = config
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        record = {"ts": now_iso(), "event": event}
        record.update(fields)
        for exporter in self.config.exporters:
            if exporter.get("type") != "jsonl":
                continue
            path = Path(exporter["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            with self._lock, path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
