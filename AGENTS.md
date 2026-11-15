# Repository guidance

## Purpose and boundaries

- `consideration` is an OpenAI-compatible passthrough proxy with two ordered, host-configured pipelines: `server_bound` transforms client requests and `client_bound` transforms upstream responses.
- Retryable client-bound violations use the host-authored repair loop; server-bound violations and non-retryable violations do not get repaired.
- Credentials are passthrough-only: the client's `Authorization` header is forwarded verbatim; the host does not hold an API key.
- v0 deliberately has no sandbox for plugins, no cross-plugin state sharing, no session cache, no per-chunk hooks, and no server-bound rerun during repair. Plugins are trusted local processes.

## Bootstrap and commands

- Requires Python `>=3.11`; runtime dependencies are empty. Ruff is configured in `pyproject.toml` for linting and formatting.
- Run the complete suite from the repository root with:
  `python -m unittest discover -s tests -t .`
- Run a focused test with, for example:
  `python -m unittest tests.test_app.AppTests.test_journal_replay_reproduces_repairs -v`
- Run `ruff check .` and `ruff format --check .`; use `ruff format .` to apply formatting fixes.
- Compile-check the tree with `python -m compileall -q consideration plugins tests`; use `git diff --check` for whitespace validation.
- The example config resolves `env:OPENAI_BASE_URL`; set it before checking or starting it (PowerShell: `$env:OPENAI_BASE_URL='https://api.openai.com/v1'`).
- Validate or start the proxy with `python -m consideration --config examples/config.yaml --check` or `python -m consideration --config examples/config.yaml`.
- Replay requires a journal created with `journal.include_bodies: true` and the same config/artifacts: `python -m consideration.replay --config <config> --journal <journal.jsonl>`.

## Architecture

- `consideration/server.py` owns the HTTP listener, request framing/size limits, routing, and CLI; `consideration/app.py` owns the request lifecycle and journal record creation.
- `consideration/pipeline.py` runs ordered stages; `envelope.py`, `patch.py`, `repair.py`, and `terminal.py` define the transformation, repair, and failure contracts.
- `consideration/upstream.py` is the only upstream HTTP client; preserve its credential/header filtering when changing egress behavior.
- `consideration/plugin.py` supervises one subprocess per plugin using JSON-RPC 2.0 with LSP `Content-Length` framing over stdio. It owns handshake, timeouts, cancellation, health checks, restart/backoff, circuit breaking, and shutdown.
- Request state is scoped to one request and staged per pipeline attempt; rejected/terminal attempts must not commit writes.
- When a client-bound pipeline exists, upstream responses are buffered and streaming output is synthesized as SSE; without one, upstream streaming bytes are relayed unchanged.

## Plugins and configuration

- Plugin manifests are JSON and must declare `id`, `version`, `contract`, `hooks`, `read_only`, and `command`; the manifest ID must match the config entry ID.
- Manifest-relative `cwd` defaults to the manifest directory, and command paths are interpreted from that cwd. The example manifests therefore launch `plugin.py` directly.
- Plugins must implement `plugin/describe`, `plugin/init`, `plugin/shutdown`, `$/ping`, and `hook` using the repository's framed JSON-RPC protocol. Reference implementations are under `plugins/`.
- Plugin namespaces are private. A plugin receives its envelope and its own state through hook parameters; it cannot read another plugin's state.
- Keep reference/test plugins stdlib-only and preserve deterministic behavior: replay depends on pure stage behavior and recorded response bodies.

## Project layout

- `consideration/`: production host, protocol, config parser, pipelines, upstream client, journaling, telemetry, HTTP server, and replay.
- `plugins/`: reference plugin manifests and stdio implementations (`prompt-guard`, `style-lint`, `pii-mask`).
- `examples/`: runnable configuration; paths are relative to `examples/config.yaml`.
- `tests/`: stdlib `unittest` suite, fake upstream, test plugin, protocol/pipeline/app/server/replay coverage, and assertion helpers.
