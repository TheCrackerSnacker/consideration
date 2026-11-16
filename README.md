# consideration v0.2.0

`consideration` is an OpenAI-compatible passthrough proxy with ordered,
host-configured plugin pipelines:

- **server-bound** stages inspect or transform client chat-completion requests;
- **client-bound** stages inspect or transform upstream chat-completion responses;
- retryable client-bound violations can trigger a host-authored repair request.

## v0.2.0 plugin contract

This release is a breaking change from v0.1: **plugin hooks no longer receive
HTTP headers**, including filtered request headers and upstream response
headers. Headers are also omitted from nested hook context. The normative hook
input is [`docs/plugin-hook.schema.json`](docs/plugin-hook.schema.json).
Manifests and runtime `plugin/describe` responses must agree on version,
contract, hooks, and read-only status.

## Run

```text
set OPENAI_BASE_URL=https://api.openai.com/v1
python -m consideration --config examples/config.yaml --check
python -m consideration --config examples/config.yaml
```

Only `POST /v1/chat/completions` enters the plugin pipelines. Every other path
and method is transparently passed upstream; there is no passthrough-path
allowlist. See [`docs/configuration.md`](docs/configuration.md) and
[`docs/header-handling.md`](docs/header-handling.md) for limits and forwarding
rules.

The client's `Authorization` header is forwarded verbatim. The host does not
store or supply an API key and does not authenticate clients. Plugins are
trusted local processes with the host user's filesystem and network
privileges; there is no sandbox. Do not install untrusted plugins. Plugin
source artifacts are hashed into the config hash, and optional `artifact` pins
must match the launched plugin script/package digest.

Upstream responses are buffered when a client-bound pipeline is configured.
Streaming clients then receive synthesized SSE after validation and repair;
all choices are preserved, but token timing and exact provider chunk layout
are not. Without a client-bound pipeline, upstream streaming bytes are relayed
unchanged and no response checks run. See the configuration reference for the
supported response-content forms.

## Journal privacy and replay

Journals redact credential/cookie headers. Request bodies and plaintext state
values can contain sensitive data, so enabling `journal.include_bodies`
requires `journal.acknowledge_sensitive: true`. Body entries are bounded by
`journal.max_body_bytes`; state keys and commit status are always recorded,
while exact state write values appear only when body recording is enabled.
Journal I/O failures do not fail requests, but are logged and reported as
`journal.write_failed` events.

Replay requires body recording and the same config hash, including plugin
source artifacts:

```text
python -m consideration.replay --config config.yaml --journal journal/journal-YYYY-MM-DD.jsonl
```

## Tests

```text
python -m unittest discover -s tests -t .
```
