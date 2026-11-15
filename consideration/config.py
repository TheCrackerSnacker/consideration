"""Typed host configuration with boot-time validation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import CONTRACT_VERSION
from .util import canonical_json, sha256_bytes
from .yamlmini import YAMLError, loads as yaml_loads


class ConfigError(ValueError):
    """A configuration or manifest problem that must stop the host at boot."""


@dataclass(frozen=True)
class StageConfig:
    stage_id: str
    plugin: str
    hook: str
    timeout_ms: int
    on_error: str
    fallback_violations: tuple[dict[str, Any], ...] = ()
    plugin_version: str = ""
    paired: bool = False


@dataclass(frozen=True)
class RepairConfig:
    max_attempts: int = 2
    on_exhaustion: str = "violation"
    total_timeout_ms: int = 0


@dataclass(frozen=True)
class PipelineConfig:
    server_bound: tuple[StageConfig, ...] = ()
    client_bound: tuple[StageConfig, ...] = ()
    repair: RepairConfig = field(default_factory=RepairConfig)


@dataclass(frozen=True)
class PluginEntry:
    id: str
    manifest_path: str
    manifest: dict[str, Any]
    required: bool
    config: dict[str, Any]
    artifact: str | None
    command: tuple[str, ...]
    cwd: str
    env: dict[str, str]


@dataclass(frozen=True)
class PluginsConfig:
    directory: str
    startup_timeout_ms: int
    workers: int
    max_frame_bytes: int
    shutdown_timeout_ms: int
    health_interval_ms: int
    entries: dict[str, PluginEntry]


@dataclass(frozen=True)
class ServerConfig:
    base_url: str
    organization: str | None
    timeout_ms: int
    headers: dict[str, str]
    passthrough_paths: tuple[str, ...]


@dataclass(frozen=True)
class JournalConfig:
    enabled: bool
    path: str
    include_bodies: bool
    max_body_bytes: int
    state_values: str
    acknowledge_sensitive: bool


@dataclass(frozen=True)
class TelemetryConfig:
    mode: str
    exporters: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Config:
    source_path: str
    raw: dict[str, Any]
    listen_host: str
    listen_port: int
    max_body_bytes: int
    header_allowlist: tuple[str, ...]
    server: ServerConfig
    plugins: PluginsConfig
    pipeline: PipelineConfig
    journal: JournalConfig
    telemetry: TelemetryConfig
    config_hash: str


DEFAULT_HEADER_ALLOWLIST = (
    "content-type",
    "user-agent",
    "openai-organization",
    "openai-project",
    "x-request-id",
    "x-stainless-*",
    "x-client-*",
)

STAGE_KEYS = {
    "stage_id",
    "plugin",
    "hook",
    "timeout_ms",
    "on_error",
    "fallback_violations",
}
ON_ERROR = {"fail_open", "fail_closed", "fail_verdict"}
SEVERITIES = {"info", "warning", "error", "critical"}


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping")
    return value


def _check_keys(value: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(unknown)}")


def _require_int(value: Any, where: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where} must be >= {minimum}")
    return value


def _require_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be a boolean")
    return value


def _require_str(value: Any, where: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ConfigError(f"{where} must be a non-empty string")
    return value


def _resolve_env(value: Any, where: str) -> Any:
    if isinstance(value, dict):
        return {k: _resolve_env(v, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, str) and value.startswith("env:"):
        name = value[4:]
        if not name:
            raise ConfigError(f"{where}: empty environment reference")
        if name not in os.environ:
            raise ConfigError(f"{where}: environment variable {name} is not set")
        return os.environ[name]
    return value


def contract_compatible(host: str, plugin: str) -> bool:
    def parse(text: str) -> tuple[int, int]:
        match = re.match(r"^(\d+)\.(\d+)$", text or "")
        if not match:
            raise ConfigError(f"invalid contract version {text!r}")
        return int(match.group(1)), int(match.group(2))

    host_major, host_minor = parse(host)
    plugin_major, plugin_minor = parse(plugin)
    return plugin_major == host_major and plugin_minor <= host_minor


def _load_manifest(path: Path, entry_id: str) -> tuple[dict[str, Any], str, str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"plugin {entry_id}: cannot read manifest {path}: {exc}") from exc
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"plugin {entry_id}: invalid manifest JSON {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ConfigError(f"plugin {entry_id}: manifest must be a JSON object")
    for key in ("id", "version", "contract", "hooks", "read_only"):
        if key not in manifest:
            raise ConfigError(f"plugin {entry_id}: manifest missing {key}")
    if manifest["id"] != entry_id:
        raise ConfigError(
            f"plugin {entry_id}: manifest id {manifest['id']!r} does not match the install record"
        )
    if not contract_compatible(CONTRACT_VERSION, manifest["contract"]):
        raise ConfigError(
            f"plugin {entry_id}: contract {manifest['contract']} is incompatible with {CONTRACT_VERSION}"
        )
    hooks = manifest["hooks"]
    if not isinstance(hooks, dict):
        raise ConfigError(f"plugin {entry_id}: hooks must be an object")
    for direction in ("server_bound", "client_bound"):
        values = hooks.get(direction, [])
        if not isinstance(values, list) or any(v not in ("request", "response") for v in values):
            raise ConfigError(f"plugin {entry_id}: hooks.{direction} must be a list of hook names")
    return manifest, sha256_bytes(raw), str(path)


def _validate_stage(
    raw: dict[str, Any],
    direction: str,
    where: str,
    entries: dict[str, PluginEntry],
) -> StageConfig:
    _check_keys(raw, STAGE_KEYS, where)
    stage_id = _require_str(raw.get("stage_id"), f"{where}.stage_id")
    plugin_id = _require_str(raw.get("plugin"), f"{where}.plugin")
    hook = raw.get("hook")
    expected_hook = "request" if direction == "server_bound" else "response"
    if hook != expected_hook:
        raise ConfigError(f"{where}.hook must be {expected_hook!r} for {direction}")
    entry = entries.get(plugin_id)
    if entry is None:
        raise ConfigError(f"{where}: unknown plugin {plugin_id!r}")
    exported = entry.manifest["hooks"].get(direction, [])
    if hook not in exported:
        raise ConfigError(
            f"{where}: plugin {plugin_id} does not export hook {hook!r} for {direction}"
        )
    if "on_error" not in raw:
        raise ConfigError(f"{where}: on_error is required")
    on_error = raw["on_error"]
    if on_error not in ON_ERROR:
        raise ConfigError(f"{where}.on_error must be one of {sorted(ON_ERROR)}")
    timeout_ms = _require_int(raw.get("timeout_ms"), f"{where}.timeout_ms", 1)
    fallback = raw.get("fallback_violations") or []
    if not isinstance(fallback, list):
        raise ConfigError(f"{where}.fallback_violations must be a list")
    if on_error == "fail_verdict" and not fallback:
        raise ConfigError(f"{where}: fail_verdict requires fallback_violations")
    for index, violation in enumerate(fallback):
        _validate_violation(violation, f"{where}.fallback_violations[{index}]")
    return StageConfig(
        stage_id=stage_id,
        plugin=plugin_id,
        hook=hook,
        timeout_ms=timeout_ms,
        on_error=on_error,
        fallback_violations=tuple(dict(v) for v in fallback),
        plugin_version=str(entry.manifest.get("version", "")),
        paired=bool(entry.manifest.get("paired", False)),
    )


def _validate_violation(value: Any, where: str) -> None:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be an object")
    for key in ("code", "severity", "message", "retryable"):
        if key not in value:
            raise ConfigError(f"{where}: missing {key}")
    if not isinstance(value["code"], str) or not value["code"]:
        raise ConfigError(f"{where}.code must be a non-empty string")
    if value["severity"] not in SEVERITIES:
        raise ConfigError(f"{where}.severity must be one of {sorted(SEVERITIES)}")
    if not isinstance(value["message"], str):
        raise ConfigError(f"{where}.message must be a string")
    if not isinstance(value["retryable"], bool):
        raise ConfigError(f"{where}.retryable must be a boolean")


def _parse_host(raw: dict[str, Any]) -> tuple[str, int, int, tuple[str, ...]]:
    host = _require_mapping(raw.get("host"), "host")
    _check_keys(host, {"listen", "max_body_bytes", "header_allowlist"}, "host")
    listen = host.get("listen", "127.0.0.1:4000")
    listen = _require_str(listen, "host.listen")
    if listen.startswith("["):
        host_part, _, port_part = listen.rpartition("]:")
    else:
        host_part, _, port_part = listen.rpartition(":")
    if not host_part or not port_part:
        raise ConfigError("host.listen must be HOST:PORT")
    try:
        port = int(port_part)
    except ValueError as exc:
        raise ConfigError("host.listen port must be an integer") from exc
    if not 0 <= port <= 65535:
        raise ConfigError("host.listen port must be between 0 and 65535")
    max_body = _require_int(host.get("max_body_bytes", 32 * 1024 * 1024), "host.max_body_bytes", 1)
    allowlist = host.get("header_allowlist", list(DEFAULT_HEADER_ALLOWLIST))
    if not isinstance(allowlist, list) or any(not isinstance(v, str) for v in allowlist):
        raise ConfigError("host.header_allowlist must be a list of strings")
    return host_part, port, max_body, tuple(allowlist)


def _parse_server(raw: dict[str, Any]) -> ServerConfig:
    server = _require_mapping(raw.get("server"), "server")
    _check_keys(
        server,
        {"base_url", "organization", "timeout_ms", "headers", "passthrough_paths"},
        "server",
    )
    base_url = _require_str(server.get("base_url"), "server.base_url").rstrip("/")
    if not re.match(r"^https?://", base_url):
        raise ConfigError("server.base_url must start with http:// or https://")
    organization = server.get("organization")
    if organization is not None:
        organization = _require_str(organization, "server.organization")
    timeout_ms = _require_int(server.get("timeout_ms", 120000), "server.timeout_ms", 1)
    headers_raw = _require_mapping(server.get("headers"), "server.headers")
    headers: dict[str, str] = {}
    for name, value in headers_raw.items():
        lowered = str(name).lower()
        if (
            lowered in {"authorization", "cookie", "proxy-authorization"}
            or "key" in lowered
            or "token" in lowered
            or "secret" in lowered
        ):
            raise ConfigError(f"server.headers may not set credential header {name!r}")
        if not isinstance(value, str):
            raise ConfigError(f"server.headers.{name} must be a string")
        headers[str(name)] = value
    paths = server.get("passthrough_paths", [])
    if not isinstance(paths, list) or any(not isinstance(p, str) or not p.startswith("/") for p in paths):
        raise ConfigError("server.passthrough_paths must be a list of absolute paths")
    return ServerConfig(base_url, organization, timeout_ms, headers, tuple(paths))


def _parse_plugins(raw: dict[str, Any], config_dir: Path) -> PluginsConfig:
    plugins = _require_mapping(raw.get("plugins"), "plugins")
    _check_keys(
        plugins,
        {
            "directory",
            "startup_timeout_ms",
            "workers",
            "max_frame_bytes",
            "shutdown_timeout_ms",
            "health_interval_ms",
            "entries",
        },
        "plugins",
    )
    directory = _require_str(plugins.get("directory", "./plugins"), "plugins.directory")
    startup = _require_int(plugins.get("startup_timeout_ms", 5000), "plugins.startup_timeout_ms", 1)
    workers = _require_int(plugins.get("workers", 1), "plugins.workers", 1)
    max_frame = _require_int(
        plugins.get("max_frame_bytes", 32 * 1024 * 1024), "plugins.max_frame_bytes", 1
    )
    shutdown = _require_int(
        plugins.get("shutdown_timeout_ms", 2000), "plugins.shutdown_timeout_ms", 1
    )
    health = _require_int(
        plugins.get("health_interval_ms", 10000), "plugins.health_interval_ms", 1
    )
    raw_entries = plugins.get("entries", [])
    if not isinstance(raw_entries, list):
        raise ConfigError("plugins.entries must be a list")
    entries: dict[str, PluginEntry] = {}
    manifest_hashes: dict[str, str] = {}
    for index, raw_entry in enumerate(raw_entries):
        where = f"plugins.entries[{index}]"
        entry = _require_mapping(raw_entry, where)
        _check_keys(
            entry, {"id", "manifest", "required", "config", "artifact"}, where
        )
        entry_id = _require_str(entry.get("id"), f"{where}.id")
        if entry_id in entries:
            raise ConfigError(f"{where}: duplicate plugin id {entry_id!r}")
        manifest_name = _require_str(entry.get("manifest"), f"{where}.manifest")
        manifest_path = (config_dir / manifest_name).resolve()
        manifest, digest, manifest_path_str = _load_manifest(manifest_path, entry_id)
        artifact = entry.get("artifact")
        if artifact is not None:
            artifact = _require_str(artifact, f"{where}.artifact")
            if artifact != digest:
                raise ConfigError(
                    f"{where}: artifact {artifact} does not match manifest hash {digest}"
                )
        required = entry.get("required", True)
        required = _require_bool(required, f"{where}.required")
        plugin_config = _require_mapping(entry.get("config"), f"{where}.config")
        command = manifest.get("command")
        if isinstance(command, str):
            command = [command]
        if not isinstance(command, list) or not command or any(
            not isinstance(part, str) for part in command
        ):
            raise ConfigError(f"{where}: manifest must declare command as a string or list")
        cwd_value = manifest.get("cwd", ".")
        cwd = (manifest_path.parent / _require_str(cwd_value, f"{where}.cwd")).resolve()
        env_raw = _require_mapping(manifest.get("env"), f"{where}.env")
        env = {str(k): str(v) for k, v in env_raw.items()}
        entries[entry_id] = PluginEntry(
            id=entry_id,
            manifest_path=manifest_path_str,
            manifest=manifest,
            required=required,
            config=dict(plugin_config),
            artifact=artifact,
            command=tuple(command),
            cwd=str(cwd),
            env=env,
        )
        manifest_hashes[entry_id] = digest
    return PluginsConfig(
        directory=str((config_dir / directory).resolve()),
        startup_timeout_ms=startup,
        workers=workers,
        max_frame_bytes=max_frame,
        shutdown_timeout_ms=shutdown,
        health_interval_ms=health,
        entries=entries,
    )


def _parse_pipeline(raw: dict[str, Any], entries: dict[str, PluginEntry]) -> PipelineConfig:
    pipeline = _require_mapping(raw.get("pipeline"), "pipeline")
    _check_keys(pipeline, {"server_bound", "client_bound", "repair"}, "pipeline")
    seen_stage_ids: set[str] = set()
    parsed: dict[str, tuple[StageConfig, ...]] = {}
    for direction in ("server_bound", "client_bound"):
        values = pipeline.get(direction, [])
        if not isinstance(values, list):
            raise ConfigError(f"pipeline.{direction} must be a list")
        stages: list[StageConfig] = []
        seen_plugins: set[str] = set()
        for index, raw_stage in enumerate(values):
            stage = _validate_stage(
                _require_mapping(raw_stage, f"pipeline.{direction}[{index}]"),
                direction,
                f"pipeline.{direction}[{index}]",
                entries,
            )
            if stage.stage_id in seen_stage_ids:
                raise ConfigError(f"duplicate stage_id {stage.stage_id!r}")
            seen_stage_ids.add(stage.stage_id)
            if stage.plugin in seen_plugins:
                raise ConfigError(
                    f"pipeline.{direction}: plugin {stage.plugin!r} appears more than once"
                )
            seen_plugins.add(stage.plugin)
            stages.append(stage)
        parsed[direction] = tuple(stages)

    for entry_id, entry in entries.items():
        if entry.manifest.get("paired"):
            in_server = any(s.plugin == entry_id for s in parsed["server_bound"])
            in_client = any(s.plugin == entry_id for s in parsed["client_bound"])
            if in_server != in_client:
                raise ConfigError(
                    f"plugin {entry_id!r} is paired and must be configured in both pipelines"
                )

    repair_raw = _require_mapping(pipeline.get("repair"), "pipeline.repair")
    _check_keys(repair_raw, {"max_attempts", "on_exhaustion", "total_timeout_ms"}, "pipeline.repair")
    repair = RepairConfig(
        max_attempts=_require_int(
            repair_raw.get("max_attempts", 2), "pipeline.repair.max_attempts", 0
        ),
        on_exhaustion=repair_raw.get("on_exhaustion", "violation"),
        total_timeout_ms=_require_int(
            repair_raw.get("total_timeout_ms", 0), "pipeline.repair.total_timeout_ms", 0
        ),
    )
    if repair.on_exhaustion not in {"violation", "http_502"}:
        raise ConfigError("pipeline.repair.on_exhaustion must be 'violation' or 'http_502'")
    return PipelineConfig(parsed["server_bound"], parsed["client_bound"], repair)


def _parse_journal(raw: dict[str, Any], config_dir: Path) -> JournalConfig:
    journal = _require_mapping(raw.get("journal"), "journal")
    _check_keys(
        journal,
        {
            "enabled",
            "path",
            "include_bodies",
            "max_body_bytes",
            "state_values",
            "acknowledge_sensitive",
        },
        "journal",
    )
    enabled = _require_bool(journal.get("enabled", True), "journal.enabled")
    path = _require_str(journal.get("path", "./journal"), "journal.path")
    include_bodies = _require_bool(
        journal.get("include_bodies", False), "journal.include_bodies"
    )
    max_body = _require_int(
        journal.get("max_body_bytes", 262144), "journal.max_body_bytes", 1
    )
    state_values = journal.get("state_values", "hash")
    if state_values not in {"hash", "plaintext"}:
        raise ConfigError("journal.state_values must be 'hash' or 'plaintext'")
    acknowledge = _require_bool(
        journal.get("acknowledge_sensitive", False), "journal.acknowledge_sensitive"
    )
    if enabled and include_bodies and state_values == "plaintext" and not acknowledge:
        raise ConfigError(
            "journal.include_bodies with state_values: plaintext requires "
            "journal.acknowledge_sensitive: true"
        )
    return JournalConfig(
        enabled,
        str((config_dir / path).resolve()),
        include_bodies,
        max_body,
        state_values,
        acknowledge,
    )


def _parse_telemetry(raw: dict[str, Any], config_dir: Path) -> TelemetryConfig:
    telemetry = _require_mapping(raw.get("telemetry"), "telemetry")
    _check_keys(telemetry, {"mode", "exporters"}, "telemetry")
    mode = telemetry.get("mode", "observer")
    if mode != "observer":
        raise ConfigError("telemetry.mode must be 'observer'")
    exporters_raw = telemetry.get("exporters", [])
    if not isinstance(exporters_raw, list):
        raise ConfigError("telemetry.exporters must be a list")
    exporters: list[dict[str, Any]] = []
    for index, raw_exporter in enumerate(exporters_raw):
        where = f"telemetry.exporters[{index}]"
        exporter = _require_mapping(raw_exporter, where)
        _check_keys(exporter, {"type", "path"}, where)
        if exporter.get("type") != "jsonl":
            raise ConfigError(f"{where}.type must be 'jsonl'")
        path = _require_str(exporter.get("path"), f"{where}.path")
        exporters.append({"type": "jsonl", "path": str((config_dir / path).resolve())})
    return TelemetryConfig(mode, tuple(exporters))


def load_config(path: str | Path) -> Config:
    source = Path(path).resolve()
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {source}: {exc}") from exc
    try:
        raw = yaml_loads(text)
    except YAMLError as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: configuration must be a mapping")
    raw = _resolve_env(raw, "config")
    _check_keys(
        raw,
        {"host", "server", "plugins", "pipeline", "journal", "telemetry"},
        "config",
    )
    config_dir = source.parent

    listen_host, listen_port, max_body, allowlist = _parse_host(raw)
    server = _parse_server(raw)
    plugins = _parse_plugins(raw, config_dir)
    pipeline = _parse_pipeline(raw, plugins.entries)
    journal = _parse_journal(raw, config_dir)
    telemetry = _parse_telemetry(raw, config_dir)

    for passthrough in server.passthrough_paths:
        normalized = passthrough.rstrip("/") or "/"
        if (
            normalized == "/v1/chat/completions"
            or "/v1/chat/completions".startswith(normalized + "/")
            or normalized.startswith("/v1/chat/completions/")
        ):
            raise ConfigError(
                f"server.passthrough_paths entry {passthrough!r} overlaps /v1/chat/completions"
            )

    manifest_hashes = {
        entry_id: sha256_bytes(Path(entry.manifest_path).read_bytes())
        for entry_id, entry in plugins.entries.items()
    }
    hash_material = {
        "config": raw,
        "manifests": {key: entry.manifest for key, entry in plugins.entries.items()},
        "artifacts": manifest_hashes,
    }
    config_hash = sha256_bytes(canonical_json(hash_material).encode("utf-8"))

    return Config(
        source_path=str(source),
        raw=raw,
        listen_host=listen_host,
        listen_port=listen_port,
        max_body_bytes=max_body,
        header_allowlist=allowlist,
        server=server,
        plugins=plugins,
        pipeline=pipeline,
        journal=journal,
        telemetry=telemetry,
        config_hash=config_hash,
    )
