"""Request orchestration: the request lifecycle of section 4."""

from __future__ import annotations

import fnmatch
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .envelope import validate_chat_request, validate_chat_response
from .errors import EgressError, RequestError
from .journal import Journal
from .pipeline import PipelineRunner
from .plugin import PluginManager
from .repair import all_retryable, build_repair_request
from .state import StateStore
from .streaming import synthesize_sse
from .telemetry import Telemetry
from .terminal import (
    Terminal,
    exhaustion_terminal,
    host_failure,
    passthrough_error,
    request_error,
    server_failure,
)
from .upstream import (
    UpstreamClient,
    UpstreamTimeout,
    UpstreamUnreachable,
    filter_response_headers,
    is_credential_header,
)
from .util import deep_copy, dumps, json_hash, new_request_id, now_iso, truncate_bytes

CHAT_PATH = "/v1/chat/completions"


@dataclass
class InboundRequest:
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    remote_addr: str = ""
    request_id: str | None = None
    replay_started_at: str | None = None
    replay_response_received_at: str | None = None

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass
class ServerCall:
    status: int | None
    duration_ms: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    raw: bytes | None = None
    body: dict[str, Any] | None = None
    terminal: Terminal | None = None


def exposed_headers(config: Config, headers: dict[str, str]) -> dict[str, str]:
    """The allowlisted, credential-free view plugins receive (6.2)."""
    result: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if is_credential_header(lowered):
            continue
        if any(
            fnmatch.fnmatch(lowered, pattern.lower())
            for pattern in config.header_allowlist
        ):
            result[lowered] = value
    return result


class App:
    def __init__(
        self,
        config: Config,
        plugins: PluginManager,
        telemetry: Telemetry | None = None,
        journal: Journal | None = None,
        upstream: Any | None = None,
    ):
        self.config = config
        self.plugins = plugins
        self.telemetry = telemetry
        self.journal = journal if journal is not None else Journal(config.journal)
        self.upstream = upstream if upstream is not None else UpstreamClient(config)
        self.runner = PipelineRunner(config, plugins, telemetry)

    # -- entry point ---------------------------------------------------

    def handle(self, request: InboundRequest) -> Response:
        request_id = request.request_id or new_request_id()
        started_at = request.replay_started_at or now_iso()
        started = time.monotonic()
        try:
            if (
                request.method.upper() == "POST"
                and request.path.split("?", 1)[0] == CHAT_PATH
            ):
                response, record = self._chat(request, request_id, started_at, started)
            else:
                response, record = self._passthrough(
                    request, request_id, started_at, started
                )
        except Exception:
            terminal = host_failure(
                request_id, "internal", "Internal host error.", error_type="consideration_internal"
            )
            response = self._terminal_response(terminal, request_id, 0)
            record = {
                "schema": 1,
                "request_id": request_id,
                "config_hash": self.config.config_hash,
                "started_at": started_at,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "client_streamed": False,
                "direction_results": {},
                "server": {"requests": []},
                "repairs": [],
                "state_writes": [],
                "response_bytes": len(response.body),
            }
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        record["response_bytes"] = len(response.body)
        try:
            self.journal.write(record)
        except Exception:
            pass
        self._emit(
            "request.end",
            request_id=request_id,
            status=response.status,
            duration_ms=record["duration_ms"],
        )
        return response

    # -- chat completions ---------------------------------------------

    def _chat(
        self,
        request: InboundRequest,
        request_id: str,
        started_at: str,
        started: float,
    ) -> tuple[Response, dict[str, Any]]:
        try:
            raw = request.body.decode("utf-8")
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            terminal = request_error(request_id, f"Request body is not valid JSON: {exc}")
            return self._terminal_response(terminal, request_id, 0), self._record(
                request_id, started_at, started, False
            )

        try:
            validate_chat_request(body)
        except RequestError as exc:
            terminal = request_error(request_id, str(exc))
            return self._terminal_response(terminal, request_id, 0), self._record(
                request_id, started_at, started, False
            )

        client_streamed = body.get("stream") is True
        include_usage = False
        options = body.get("stream_options")
        if isinstance(options, dict):
            include_usage = options.get("include_usage") is True

        self._emit("request.start", request_id=request_id, path=request.path)

        record = self._record(request_id, started_at, started, client_streamed)
        bodies: dict[str, Any] = {}
        if self.config.journal.include_bodies:
            bodies["request"] = self._body_entry(body)
            bodies["request_meta"] = {
                "remote_addr": request.remote_addr,
                "headers": dict(request.headers),
            }
            record["bodies"] = bodies

        meta = {
            "request_id": request_id,
            "direction": "server_bound",
            "path": request.path,
            "client": {"remote_addr": request.remote_addr},
            "headers": exposed_headers(self.config, request.headers),
            "received_at": started_at,
        }
        envelope = {"kind": "chat.request", "body": body, "meta": meta}
        state = StateStore()

        # 4-5: server-bound pipeline.
        server_result = self.runner.run(
            "server_bound", envelope, request_id, 0, state
        )
        record["direction_results"]["server_bound"] = server_result.journal()
        record["state_writes"].extend(
            write.journal() for write in server_result.state_writes
        )
        if server_result.terminal is not None:
            self._emit(
                "pipeline.terminate",
                request_id=request_id,
                direction="server_bound",
                attempt=0,
                codes=[
                    violation.get("code")
                    for violation in (server_result.terminal.violations or [])
                ],
            )
            return (
                self._terminal_response(server_result.terminal, request_id, 0),
                record,
            )

        final_request = deep_copy(server_result.envelope["body"])
        client_bound = bool(self.config.pipeline.client_bound)

        # 12.1: buffering forces a non-streaming upstream call.
        upstream_request = deep_copy(final_request)
        if client_bound:
            upstream_request["stream"] = False
            upstream_request.pop("stream_options", None)

        try:
            validate_chat_request(upstream_request)
        except RequestError as exc:
            terminal = host_failure(
                request_id, "internal", f"Egress validation failed: {exc}"
            )
            return self._terminal_response(terminal, request_id, 0), record
        if self.config.journal.include_bodies:
            bodies["final_request"] = self._body_entry(upstream_request)

        # 6-8: the one upstream call the host authors.
        call = self._call_server(
            request_id,
            upstream_request,
            request.headers,
            self.config.server.timeout_ms,
            attempt=0,
            validate_json=not (client_streamed and not client_bound),
        )
        record["server"]["requests"].append(self._server_entry(0, call))
        if self.config.journal.include_bodies and call.body is not None:
            bodies.setdefault("server_responses", []).append(self._body_entry(call.body))
        if call.terminal is not None:
            return self._terminal_response(call.terminal, request_id, 0), record

        if not client_bound:
            # No client-bound checks: relay the server response byte for byte.
            response = self._success_response(
                request_id,
                0,
                call.raw if call.raw is not None else b"",
                call.status or 200,
                filter_response_headers(call.headers),
                stream=False,
            )
            if self.config.journal.include_bodies:
                bodies["response"] = (
                    self._body_entry(call.body) if call.body is not None else None
                )
            return response, record

        response_body = call.body
        assert response_body is not None
        response_meta = {
            "request_id": request_id,
            "direction": "client_bound",
            "received_at": request.replay_response_received_at or now_iso(),
        }
        if self.config.journal.include_bodies:
            bodies["response_meta"] = dict(response_meta)

        # 10-13: client-bound attempts and the repair loop.
        attempts: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        accepted: dict[str, Any] | None = None
        last_violations: list[dict[str, Any]] = []
        cause: str | None = None
        repair_started = time.monotonic()
        attempt = 0
        last_call = call

        while True:
            attempt_envelope = {
                "kind": "chat.response",
                "body": response_body,
                "meta": dict(response_meta),
            }
            result = self.runner.run(
                "client_bound",
                attempt_envelope,
                request_id,
                attempt,
                state,
                server_request=upstream_request,
            )
            attempt_record = {"attempt": attempt}
            attempt_record.update(result.journal())
            attempts.append(attempt_record)
            record["state_writes"].extend(
                write.journal() for write in result.state_writes
            )

            if result.terminal is None:
                accepted = result.envelope
                if repairs:
                    repairs[-1]["outcome"] = "accepted"
                break

            last_violations = result.terminal.violations or []
            self._emit(
                "pipeline.terminate",
                request_id=request_id,
                direction="client_bound",
                attempt=attempt,
                codes=[violation.get("code") for violation in last_violations],
            )

            # Host-generated terminals are never repaired (7.4, 10.2).
            if result.terminal.kind != "violation":
                if repairs:
                    repairs[-1]["outcome"] = "exhausted"
                record["direction_results"]["client_bound"] = {
                    "attempts": attempts,
                    "terminal": attempt_record["terminal"],
                }
                record["repairs"] = repairs
                return (
                    self._terminal_response(result.terminal, request_id, attempt),
                    record,
                )

            if not all_retryable(last_violations):
                cause = "non_retryable"
            elif attempt >= self.config.pipeline.repair.max_attempts:
                cause = "violation"
            elif self.config.pipeline.repair.total_timeout_ms > 0 and (
                (time.monotonic() - repair_started) * 1000
                >= self.config.pipeline.repair.total_timeout_ms
            ):
                cause = "timeout"

            if cause is not None:
                if repairs:
                    repairs[-1]["outcome"] = "exhausted"
                break

            next_attempt = attempt + 1
            repair_body = build_repair_request(
                upstream_request, result.envelope["body"], last_violations
            )
            repair_entry = {
                "attempt": next_attempt,
                "trigger": {
                    "stage_id": result.terminal_stage_id,
                    "codes": [violation.get("code") for violation in last_violations],
                },
                "request_hash": json_hash(repair_body),
                "server_bound_rerun": False,
                "outcome": "rejected",
            }
            repairs.append(repair_entry)
            if self.config.journal.include_bodies:
                bodies.setdefault("repair_requests", []).append(
                    self._body_entry(repair_body)
                )

            self._emit(
                "repair.start",
                request_id=request_id,
                attempt=next_attempt,
            )
            repair_call = self._call_server(
                request_id,
                repair_body,
                request.headers,
                self.config.server.timeout_ms,
                attempt=next_attempt,
                validate_json=True,
                repair={
                    "attempts": next_attempt,
                    "max_attempts": self.config.pipeline.repair.max_attempts,
                    "exhausted": False,
                },
            )
            record["server"]["requests"].append(
                self._server_entry(next_attempt, repair_call)
            )
            if self.config.journal.include_bodies and repair_call.body is not None:
                bodies.setdefault("server_responses", []).append(
                    self._body_entry(repair_call.body)
                )
            if repair_call.terminal is not None:
                repair_entry["outcome"] = (
                    "timeout"
                    if repair_call.terminal.host_code == "server_timeout"
                    else "server_error"
                )
                self._emit(
                    "repair.end",
                    request_id=request_id,
                    attempt=next_attempt,
                    outcome=repair_entry["outcome"],
                )
                record["direction_results"]["client_bound"] = {
                    "attempts": attempts,
                    "terminal": attempt_record["terminal"],
                }
                record["repairs"] = repairs
                return (
                    self._terminal_response(
                        repair_call.terminal, request_id, next_attempt
                    ),
                    record,
                )
            self._emit(
                "repair.end",
                request_id=request_id,
                attempt=next_attempt,
                outcome="rejected",
            )
            assert repair_call.body is not None
            response_body = repair_call.body
            last_call = repair_call
            attempt = next_attempt

        if accepted is not None:
            try:
                validate_chat_response(accepted["body"])
            except EgressError:
                terminal = server_failure(
                    request_id,
                    "server_invalid_response",
                    "Server returned an invalid response.",
                )
                return self._terminal_response(terminal, request_id, attempt), record
            if self.config.journal.include_bodies:
                bodies["response"] = self._body_entry(accepted["body"])
            record["direction_results"]["client_bound"] = {
                "attempts": attempts,
                "terminal": None,
            }
            record["repairs"] = repairs
            if client_streamed:
                response = self._success_response(
                    request_id,
                    attempt,
                    b"",
                    last_call.status or 200,
                    filter_response_headers(last_call.headers),
                    stream=True,
                    include_usage=include_usage,
                    parsed=accepted["body"],
                )
            else:
                response = self._success_response(
                    request_id,
                    attempt,
                    dumps(accepted["body"]).encode("utf-8"),
                    last_call.status or 200,
                    filter_response_headers(last_call.headers),
                    stream=False,
                )
            return response, record

        # Exhaustion.
        repair_block = {
            "attempts": attempt,
            "max_attempts": self.config.pipeline.repair.max_attempts,
            "exhausted": True,
            "cause": cause or "violation",
        }
        self._emit(
            "repair.exhausted",
            request_id=request_id,
            attempts=repair_block["attempts"],
            cause=repair_block["cause"],
        )
        record["direction_results"]["client_bound"] = {
            "attempts": attempts,
            "terminal": attempts[-1]["terminal"],
        }
        record["repairs"] = repairs
        terminal = exhaustion_terminal(
            request_id,
            last_violations,
            repair_block,
            self.config.pipeline.repair.on_exhaustion,
        )
        return self._terminal_response(terminal, request_id, attempt), record

    # -- upstream ------------------------------------------------------

    def _call_server(
        self,
        request_id: str,
        body: dict[str, Any],
        client_headers: dict[str, str],
        timeout_ms: int,
        *,
        attempt: int,
        validate_json: bool,
        repair: dict[str, Any] | None = None,
    ) -> ServerCall:
        payload = dumps(body).encode("utf-8")
        started = time.monotonic()
        try:
            response = self.upstream.request(
                "POST", CHAT_PATH, client_headers, payload, timeout_ms
            )
        except UpstreamTimeout:
            return ServerCall(
                status=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                terminal=server_failure(
                    request_id,
                    "server_timeout",
                    "Server did not respond in time.",
                    repair,
                ),
            )
        except UpstreamUnreachable:
            return ServerCall(
                status=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                terminal=server_failure(
                    request_id,
                    "server_unreachable",
                    "Server did not respond.",
                    repair,
                ),
            )
        duration = int((time.monotonic() - started) * 1000)
        headers = list(response.headers)

        if not 200 <= response.status < 300:
            return ServerCall(
                status=response.status,
                duration_ms=duration,
                headers=headers,
                terminal=passthrough_error(
                    request_id,
                    response.status,
                    response.body,
                    filter_response_headers(headers),
                ),
            )

        if not validate_json:
            return ServerCall(
                status=response.status,
                duration_ms=duration,
                headers=headers,
                raw=response.body,
            )

        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ServerCall(
                status=response.status,
                duration_ms=duration,
                headers=headers,
                terminal=server_failure(
                    request_id,
                    "server_invalid_response",
                    "Server returned an invalid response.",
                    repair,
                ),
            )
        try:
            validate_chat_response(parsed)
        except EgressError:
            return ServerCall(
                status=response.status,
                duration_ms=duration,
                headers=headers,
                terminal=server_failure(
                    request_id,
                    "server_invalid_response",
                    "Server returned an invalid response.",
                    repair,
                ),
            )
        return ServerCall(
            status=response.status,
            duration_ms=duration,
            headers=headers,
            raw=response.body,
            body=parsed,
        )

    def _server_entry(self, attempt: int, call: ServerCall) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "attempt": attempt,
            "status": call.status,
            "duration_ms": call.duration_ms,
        }
        if call.terminal is not None:
            if call.terminal.host_code is not None:
                entry["error"] = call.terminal.host_code
        elif call.body is not None:
            usage = call.body.get("usage")
            if isinstance(usage, dict):
                entry["usage"] = usage
        return entry

    # -- passthrough ---------------------------------------------------

    def _passthrough(
        self,
        request: InboundRequest,
        request_id: str,
        started_at: str,
        started: float,
    ) -> tuple[Response, dict[str, Any]]:
        record = self._record(request_id, started_at, started, False)
        record["passthrough"] = {"method": request.method, "path": request.path}
        started_call = time.monotonic()
        try:
            response = self.upstream.request(
                request.method,
                request.path,
                request.headers,
                request.body if request.body else None,
                self.config.server.timeout_ms,
            )
        except UpstreamTimeout:
            terminal = server_failure(
                request_id, "server_timeout", "Server did not respond in time."
            )
            record["server"]["requests"].append(
                {
                    "attempt": 0,
                    "status": None,
                    "duration_ms": int((time.monotonic() - started_call) * 1000),
                    "error": "server_timeout",
                }
            )
            record["passthrough"]["status"] = terminal.status
            return self._terminal_response(terminal, request_id, 0), record
        except UpstreamUnreachable:
            terminal = server_failure(
                request_id, "server_unreachable", "Server did not respond."
            )
            record["server"]["requests"].append(
                {
                    "attempt": 0,
                    "status": None,
                    "duration_ms": int((time.monotonic() - started_call) * 1000),
                    "error": "server_unreachable",
                }
            )
            record["passthrough"]["status"] = terminal.status
            return self._terminal_response(terminal, request_id, 0), record

        record["server"]["requests"].append(
            {
                "attempt": 0,
                "status": response.status,
                "duration_ms": int((time.monotonic() - started_call) * 1000),
            }
        )
        record["passthrough"]["status"] = response.status
        headers = filter_response_headers(response.headers)
        headers["x-consideration-request-id"] = request_id
        headers["x-consideration-repairs"] = "0"
        if self.config.journal.include_bodies:
            record["bodies"] = {
                "request": self._decode(request.body),
                "response": self._decode(response.body),
            }
        return Response(response.status, headers, response.body), record

    # -- responses -----------------------------------------------------

    def _success_response(
        self,
        request_id: str,
        repairs: int,
        body: bytes,
        status: int,
        headers: dict[str, str],
        *,
        stream: bool,
        include_usage: bool = False,
        parsed: dict[str, Any] | None = None,
    ) -> Response:
        headers = dict(headers)
        headers.pop("content-length", None)
        if stream:
            if parsed is not None:
                body = synthesize_sse(parsed, include_usage=include_usage)
            headers["content-type"] = "text/event-stream"
            headers["cache-control"] = "no-cache"
            headers["connection"] = "keep-alive"
            headers["x-accel-buffering"] = "no"
        else:
            headers.setdefault("content-type", "application/json")
        headers["x-consideration-request-id"] = request_id
        headers["x-consideration-repairs"] = str(repairs)
        return Response(status, headers, body)

    def _terminal_response(
        self, terminal: Terminal, request_id: str, repairs: int
    ) -> Response:
        headers: dict[str, str] = {}
        if terminal.kind == "passthrough":
            headers.update(terminal.headers)
        else:
            headers["content-type"] = "application/json"
        headers.pop("content-length", None)
        headers["x-consideration-request-id"] = request_id
        headers["x-consideration-repairs"] = str(repairs)
        headers["x-consideration-verdict"] = terminal.verdict
        if terminal.host_code is not None:
            headers["x-consideration-code"] = terminal.host_code
        return Response(terminal.status, headers, terminal.body_bytes())

    # -- journal -------------------------------------------------------

    def _record(
        self, request_id: str, started_at: str, started: float, streamed: bool
    ) -> dict[str, Any]:
        return {
            "schema": 1,
            "request_id": request_id,
            "config_hash": self.config.config_hash,
            "started_at": started_at,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "client_streamed": streamed,
            "direction_results": {},
            "server": {"requests": []},
            "repairs": [],
            "state_writes": [],
            "response_bytes": 0,
        }

    def _decode(self, data: bytes) -> Any:
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            text, truncated = truncate_bytes(
                data.decode("utf-8", errors="replace"),
                self.config.journal.max_body_bytes,
            )
            return {"text": text, "truncated": truncated}

    def _body_entry(self, value: Any) -> dict[str, Any]:
        text = dumps(value)
        if len(text.encode("utf-8")) <= self.config.journal.max_body_bytes:
            return {"body": value, "truncated": False}
        clipped, _ = truncate_bytes(text, self.config.journal.max_body_bytes)
        return {"body": clipped, "truncated": True}

    # -- telemetry -----------------------------------------------------

    def _emit(self, event: str, **fields: Any) -> None:
        if self.telemetry is None:
            return
        try:
            self.telemetry.emit(event, **fields)
        except Exception:
            pass


def build_app(config: Config) -> App:
    """Boot the host components in the order of 14.1."""
    telemetry = Telemetry(config.telemetry)
    plugins = PluginManager(config, telemetry)
    plugins.start_all()
    return App(config, plugins, telemetry=telemetry)
