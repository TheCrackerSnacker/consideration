# consideration

`consideration` is an OpenAI-compatible passthrough proxy with ordered,
host-configured plugin pipelines:

- **server-bound** stages inspect or transform client requests;
- **client-bound** stages inspect or transform upstream responses;
- retryable client-bound violations can trigger a host-authored repair request.

## Run

```text
set OPENAI_BASE_URL=https://api.openai.com/v1
python -m consideration --config examples/config.yaml --check
python -m consideration --config examples/config.yaml
```

The client `Authorization` header is forwarded verbatim. The host does not
store or supply an API key. Plugins are trusted local processes and can see
only the envelopes and their own request-scoped state; do not install
untrusted plugins.

v0 buffers upstream responses when a client-bound pipeline is configured, then
synthesizes a downstream SSE response for streaming clients. Without a
client-bound pipeline, upstream streaming bytes are relayed unchanged.

Reference plugins are in `plugins/`: `acme/prompt-guard`,
`acme/style-lint`, and the paired `acme/pii-mask`.

## Tests

```text
python -m unittest discover -s tests -t .
```

## Replay

Enable `journal.include_bodies`, then replay a JSONL entry against the same
configuration:

```text
python -m consideration.replay --config config.yaml --journal journal/journal-YYYY-MM-DD.jsonl
```

Replay verifies the configuration hash and reproduces the recorded pipeline,
including repair attempts. Volatile request IDs, timestamps, durations, and
response byte counts are excluded from the equality check.
