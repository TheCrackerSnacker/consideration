"""Append-only JSONL journal with daily rotation."""

from __future__ import annotations

import datetime as _dt
import json
import threading
from pathlib import Path
from typing import Any

from .config import JournalConfig


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
