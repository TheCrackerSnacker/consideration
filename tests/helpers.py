"""Helpers for building temporary configs and plugin installs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from consideration.config import load_config

ECHO = Path(__file__).parent / "plugins" / "echo_plugin.py"


def write_plugin(
    root: Path,
    plugin_id: str = "acme/echo",
    *,
    hooks: dict | None = None,
    paired: bool = False,
    read_only: bool = False,
    command: list[str] | None = None,
) -> Path:
    hooks = hooks or {
        "server_bound": ["request"],
        "client_bound": ["response"],
    }
    manifest = {
        "id": plugin_id,
        "version": "0.2.0",
        "contract": "0.2",
        "hooks": hooks,
        "read_only": read_only,
        "command": command or [sys.executable, "-u", str(ECHO)],
        "env": {"TEST_PLUGIN_READ_ONLY": str(read_only).lower()},
    }
    if paired:
        manifest["paired"] = True
    path = root / "plugins" / plugin_id.split("/")[-1] / "plugin.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def config_text(
    *,
    plugin_config: dict | None = None,
    server_bound: bool = True,
    client_bound: bool = True,
    plugin_id: str = "acme/echo",
    timeout_ms: int = 500,
    on_error: str = "fail_closed",
    repair: dict | None = None,
    extra: str = "",
    stage_extra: dict | None = None,
    host_extra: str = "",
    base_url: str = "http://127.0.0.1:9/v1",
) -> str:
    stage_extra = stage_extra or {}
    extra_lines = "".join(
        f"      {key}: {json.dumps(value)}\n" for key, value in stage_extra.items()
    )
    plugin_config = plugin_config or {}
    lines = [
        "host:",
        "  listen: 127.0.0.1:0",
    ]
    if host_extra:
        lines.append(host_extra.rstrip("\n"))
    lines += [
        "server:",
        f"  base_url: {base_url}",
        "  timeout_ms: 5000",
        "plugins:",
        "  entries:",
        f"    - id: {plugin_id}",
        f"      manifest: ./plugins/{plugin_id.split('/')[-1]}/plugin.json",
        "      required: true",
        f"      config: {json.dumps(plugin_config)}",
        "pipeline:",
    ]
    if server_bound:
        lines += [
            "  server_bound:",
            "    - stage_id: in",
            f"      plugin: {plugin_id}",
            "      hook: request",
            f"      timeout_ms: {timeout_ms}",
            f"      on_error: {on_error}",
        ]
        if extra_lines:
            lines.append(extra_lines.rstrip("\n"))
    if client_bound:
        lines += [
            "  client_bound:",
            "    - stage_id: out",
            f"      plugin: {plugin_id}",
            "      hook: response",
            f"      timeout_ms: {timeout_ms}",
            f"      on_error: {on_error}",
        ]
        if extra_lines:
            lines.append(extra_lines.rstrip("\n"))
    if repair is not None:
        lines.append("  repair:")
        for key, value in repair.items():
            lines.append(f"    {key}: {json.dumps(value)}")
    if extra:
        lines.append(extra)
    return "\n".join(lines) + "\n"


def make_config(
    root: Path,
    *,
    plugin_config: dict | None = None,
    server_bound: bool = True,
    client_bound: bool = True,
    timeout_ms: int = 500,
    on_error: str = "fail_closed",
    repair: dict | None = None,
    extra: str = "",
    stage_extra: dict | None = None,
    hooks: dict | None = None,
    paired: bool = False,
    read_only: bool = False,
    host_extra: str = "",
    base_url: str = "http://127.0.0.1:9/v1",
):
    write_plugin(root, hooks=hooks, paired=paired, read_only=read_only)
    path = root / "config.yaml"
    path.write_text(
        config_text(
            plugin_config=plugin_config,
            server_bound=server_bound,
            client_bound=client_bound,
            timeout_ms=timeout_ms,
            on_error=on_error,
            repair=repair,
            extra=extra,
            stage_extra=stage_extra,
            host_extra=host_extra,
            base_url=base_url,
        ),
        encoding="utf-8",
    )
    return load_config(path)
