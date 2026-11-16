# HTTP header handling (v0.2.0)

HTTP headers are host-owned transport data. They are never included in plugin
hook inputs, envelope metadata, or `context.server_request`. The hook contract
schema rejects unknown envelope metadata fields and has no `headers` property.

## Client to host

- Request framing accepts a single `Transfer-Encoding: chunked` or one or more
  identical decimal `Content-Length` fields. `Transfer-Encoding` together with
  `Content-Length`, conflicting lengths, malformed chunks/trailers, and
  truncated bodies are rejected.
- Header and body reads share an absolute per-request deadline of
  `host.read_timeout_ms` (default 10 seconds); trickling bytes does not extend
  it. Expiry while reading the body returns `408 request_timeout`. Downstream
  socket writes are also bounded by this timeout.
- `host.max_body_bytes` limits chat requests that enter the plugin pipeline.
  Other requests bypass that pipeline limit but remain bounded by
  `host.max_transport_body_bytes` (default 128 MiB). These limits do not make
  large passthrough buffering free of resource cost.

## Server-bound: host to upstream

The proxy removes the fixed hop-by-hop fields (`Connection`, `Keep-Alive`,
`Proxy-Authenticate`, `Proxy-Authorization`, `TE`, `Trailer`,
`Transfer-Encoding`, `Upgrade`, `Host`, and `Content-Length`) and every field
named by `Connection`. `Accept-Encoding` is also removed. The client's
`Authorization` value is forwarded verbatim; other credential-like headers
are removed, including `Cookie`, `Proxy-Authorization`, names containing
`key`, `token`, or `secret`, and `Set-Cookie`. Remaining client fields are
filtered through `host.header_allowlist` (default: `content-type`,
`user-agent`, `openai-organization`, `openai-project`, `x-request-id`,
`x-stainless-*`, and `x-client-*`). Host-configured headers and the default
organization are then applied. `Content-Length` is set from the actual body;
the inbound value is never trusted.

## Upstream to host

The same fixed and `Connection`-nominated hop-by-hop fields are removed.
`Content-Length` is omitted and recalculated at the client boundary. Other
end-to-end response headers are preserved by default, including `Server`,
`Via`, `Alt-Svc`, `Location`, `Refresh`, `WWW-Authenticate`, CSP, HSTS, CORS,
and other browser security/policy fields. There is no broad filter based on a
header merely sounding sensitive.

Repeated response fields remain separate in the host response representation.
In particular, every upstream `Set-Cookie` field is forwarded separately; it
is never comma-joined and the proxy keeps no cookie jar or cookie state. Cookie
storage and later use belong to the client.

## Host to client and diagnostics

Filtered end-to-end response fields are relayed; proxy-generated responses add
the applicable `x-consideration-*` fields. `Content-Length` is generated from
the actual downstream body. Duplicate upstream `Set-Cookie` fields are emitted
as separate HTTP fields.

The proxy does not journal response headers. When bodies are journaled,
request metadata may contain non-credential request headers; a centralized
redactor removes `Authorization`, `Cookie`, `Proxy-Authorization`,
`Set-Cookie`, and any header name containing `key`, `token`, or `secret`.
Body journaling remains sensitive regardless of header redaction. Internal
diagnostic events contain request/plugin identifiers and error types, not
credentials or request/response bodies.
