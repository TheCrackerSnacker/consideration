# Configuration reference (v0.2.0)

Configuration is YAML, read once at startup. Unknown keys are errors. Values
of the form `env:NAME` are resolved from the environment.

## Host and routing

- `host.listen`: `HOST:PORT`; bracket IPv6 literals, for example `[::1]:4000`.
- `host.max_body_bytes`: maximum buffered request size for
  `POST /v1/chat/completions`, which enters the plugin pipeline (default 32
  MiB).
- `host.max_transport_body_bytes`: hard inbound buffering limit for all
  routes (default 128 MiB). It is separate from the pipeline body limit.
- `host.read_timeout_ms`: socket read timeout for request headers/body
  (default 10000 ms).
- `host.header_allowlist`: client headers eligible for server-bound forwarding.
  The default is documented in [header handling](header-handling.md).

Only `POST /v1/chat/completions` is handled by the chat pipeline. All other
methods and paths, including nested paths and query strings, are transparent
passthrough. There is no `server.passthrough_paths` allowlist. Passthrough
bypasses plugins and `host.max_body_bytes`, but still uses the transport cap
and request read deadline.

`server` supports `base_url`, `organization`, `timeout_ms`, and host-authored
`headers`. Credentials cannot be set in `server.headers`. The client's
`Authorization` header is passed through unchanged. `server.timeout_ms`
(default 120000 ms) bounds each upstream generation. Each pipeline stage's
required `timeout_ms` is its hook deadline. Plugin startup, shutdown, health
interval, worker count, and maximum frame size are controlled under `plugins`.

## Plugins and pipeline

Each plugin entry names its JSON manifest, optional config, `required` flag,
and optional `artifact` digest. `artifact` pins the launched plugin script or
package digest, not the manifest digest. Opaque module commands that cannot be
hashed directly require an externally supplied SHA-256 pin. The digest is part
of `config_hash`, so replay detects changed plugin code even if the manifest
is unchanged. Hashable local sources are checked again before each plugin
spawn/restart; an external pin is the operator's assertion for opaque packages.
Runtime `plugin/describe` metadata must match the manifest's version, contract,
hooks, and `read_only` flag. A read-only plugin may not write state.

The former unused `plugins.directory` option has been removed; manifest paths
are resolved relative to the config file and commands/cwd relative to the
manifest. Plugin processes are trusted code, not sandboxed.

Each pipeline stage configures a plugin/hook, positive `timeout_ms`, and an
`on_error` policy (`fail_open`, `fail_closed`, or `fail_verdict`). Plugin
workers, frame size, startup/shutdown timeouts, and health-check interval are
configured under `plugins`.

`pipeline.repair.max_attempts` counts repair generations (default 2),
`on_exhaustion` is `violation` or `http_502`, and `total_timeout_ms: 0` means no
host-imposed cap. Repair is triggered only by retryable client-bound plugin
violations. Server-bound violations, infrastructure errors, and host-generated
terminals are not repaired.

## Journal and telemetry

- `journal.enabled`, `path`, `max_body_bytes` (default 256 KiB), and
  `include_bodies` control JSONL request records.
- `include_bodies: true` requires `acknowledge_sensitive: true`. Bodies and
  plaintext state values can be sensitive; recorded bodies are bounded and
  explicitly marked when truncated.
- State keys and commit status are always journaled. When body recording is
  enabled, each staged state write also contains its exact set/delete values;
  when disabled, values and hashes are omitted.
- Credential and cookie headers are redacted from request metadata. Journal
  write failures do not fail requests and emit `journal.write_failed` plus a
  diagnostic log entry.
- Telemetry is an observer with a JSONL exporter. Plugin log events retain
  their level and message length, not message text, so prompt/body content is
  not forwarded through logs by default.

## Streaming and replay

When a client-bound pipeline is configured, upstream responses are buffered
with `stream: false` (including repair generations). Accepted responses are
synthesized as SSE. The synthesizer preserves all choices, choice indices,
finish reasons, usage chunks, text content (including text-part arrays),
`tool_calls`, `function_call`, and refusal fields. Non-text content parts are
not reconstructed. The final content is emitted as one delta, so token timing
and provider chunk layout are not preserved. With no client-bound stages,
streaming bytes pass through unchanged and no response checks run.

Replay requires `journal.include_bodies: true`, the original config, and the
same plugin artifact hashes. Since state values are included only with body
recording, replay can reproduce request-scoped state writes deterministically.
