"""HTTP listener, request framing, and the command-line entry point."""

from __future__ import annotations

import argparse
import logging
import re
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .app import App, InboundRequest, Response, build_app
from .config import ConfigError, load_config
from .errors import StageFailure
from .util import dumps


class BodyTooLarge(Exception):
    pass


class RequestReadTimeout(Exception):
    pass


class DeadlineReader:
    """Unbuffered socket reader that enforces one absolute per-request deadline."""

    def __init__(self, connection: socket.socket, timeout_s: float):
        self.connection = connection
        self.stream = connection.makefile("rb", buffering=0)
        self.timeout_s = timeout_s
        self.deadline = 0.0

    def reset_deadline(self) -> None:
        self.deadline = time.monotonic() + self.timeout_s

    def _read(self, size: int) -> bytes:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request read deadline exceeded")
        self.connection.settimeout(remaining)
        return self.stream.read(size)

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunks = []
            while True:
                chunk = self._read(8192)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self._read(size - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)

    def readline(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        line = bytearray()
        while size < 0 or len(line) < size:
            char = self._read(1)
            if not char:
                break
            line.extend(char)
            if char == b"\n":
                break
        return bytes(line)

    def close(self) -> None:
        self.stream.close()


class ConsiderationHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: App):
        self.app = app
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"consideration/{__version__}"

    def setup(self) -> None:
        super().setup()
        app: App = self.server.app  # type: ignore[attr-defined]
        timeout = app.config.read_timeout_ms / 1000.0
        self.connection.settimeout(timeout)
        self.rfile.close()
        self.rfile = DeadlineReader(self.connection, timeout)

    def handle_one_request(self) -> None:
        self.rfile.reset_deadline()
        super().handle_one_request()

    # -- verbs ---------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    # -- plumbing ------------------------------------------------------

    def _dispatch(self) -> None:
        app: App = self.server.app  # type: ignore[attr-defined]
        pipeline_request = (
            self.command.upper() == "POST"
            and self.path.split("?", 1)[0] == "/v1/chat/completions"
        )
        body_limit = (
            min(app.config.max_body_bytes, app.config.max_transport_body_bytes)
            if pipeline_request
            else app.config.max_transport_body_bytes
        )
        try:
            body = self._read_body(body_limit, app.config.max_transport_body_bytes)
        except BodyTooLarge:
            self.connection.settimeout(app.config.read_timeout_ms / 1000.0)
            self.close_connection = True
            self._send_json(
                413,
                {
                    "error": {
                        "message": "Request body exceeds the configured inbound body limit.",
                        "type": "consideration_request_error",
                        "param": None,
                        "code": "invalid_request",
                    }
                },
            )
            return
        except RequestReadTimeout:
            self.connection.settimeout(app.config.read_timeout_ms / 1000.0)
            self._send_json(
                408,
                {
                    "error": {
                        "message": "Timed out while reading request.",
                        "type": "consideration_request_error",
                        "param": None,
                        "code": "request_timeout",
                    }
                },
            )
            self.close_connection = True
            return
        except ValueError:
            self.connection.settimeout(app.config.read_timeout_ms / 1000.0)
            self.close_connection = True
            self._send_json(
                400,
                {
                    "error": {
                        "message": "Malformed request body.",
                        "type": "consideration_request_error",
                        "param": None,
                        "code": "invalid_request",
                    }
                },
            )
            return
        except OSError:
            self.close_connection = True
            return
        else:
            self.connection.settimeout(app.config.read_timeout_ms / 1000.0)

        headers = {name: value for name, value in self.headers.items()}
        connection_values = self.headers.get_all("Connection", [])
        if connection_values:
            headers["Connection"] = ", ".join(connection_values)
        request = InboundRequest(
            method=self.command,
            path=self.path,
            headers=headers,
            body=body,
            remote_addr=self.client_address[0] if self.client_address else "",
        )
        try:
            response = app.handle(request)
        except Exception as exc:
            logging.getLogger(__name__).error(
                "request handler failed internally (%s)", type(exc).__name__
            )
            response = Response(
                500,
                {"content-type": "application/json"},
                dumps(
                    {
                        "error": {
                            "message": "Internal host error.",
                            "type": "consideration_internal",
                            "param": None,
                            "code": "internal",
                        }
                    }
                ).encode("utf-8"),
            )
        self._write(response)

    def _read_body(self, limit: int, transport_limit: int) -> bytes:
        transfer_values = self.headers.get_all("Transfer-Encoding", [])
        length_values = self.headers.get_all("Content-Length", [])
        if transfer_values and length_values:
            raise ValueError("Transfer-Encoding and Content-Length conflict")
        if transfer_values:
            if (
                len(transfer_values) != 1
                or transfer_values[0].strip().lower() != "chunked"
            ):
                raise ValueError("unsupported or duplicate Transfer-Encoding")
            return self._read_chunked(limit, transport_limit)
        if not length_values:
            return b""
        if any(not re.fullmatch(r"[0-9]+", value.strip()) for value in length_values):
            raise ValueError("invalid Content-Length")
        if len({value.strip() for value in length_values}) != 1:
            raise ValueError("conflicting Content-Length headers")
        try:
            length = int(length_values[0].strip())
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length > transport_limit:
            raise BodyTooLarge()
        if length > limit:
            self._discard_exact(length)
            raise BodyTooLarge()
        try:
            body = self.rfile.read(length)
        except TimeoutError as exc:
            raise RequestReadTimeout() from exc
        if len(body) != length:
            raise ValueError("truncated fixed-length body")
        return body

    def _discard_exact(self, length: int) -> None:
        remaining = length
        while remaining:
            try:
                chunk = self.rfile.read(min(remaining, 8192))
            except TimeoutError as exc:
                raise RequestReadTimeout() from exc
            if not chunk:
                raise ValueError("truncated fixed-length body")
            remaining -= len(chunk)

    def _read_chunked(self, limit: int, transport_limit: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        too_large = False
        while True:
            try:
                line = self.rfile.readline(65536)
            except TimeoutError as exc:
                raise RequestReadTimeout() from exc
            if not line:
                raise ValueError("truncated chunked body")
            if not line.endswith(b"\r\n"):
                raise ValueError("malformed chunk-size line")
            size_text = line[:-2].split(b";", 1)[0]
            if not re.fullmatch(rb"[0-9A-Fa-f]+", size_text):
                raise ValueError("invalid chunk size")
            size = int(size_text, 16)
            if size == 0:
                trailer_bytes = 0
                while True:
                    try:
                        trailer = self.rfile.readline(65536)
                    except TimeoutError as exc:
                        raise RequestReadTimeout() from exc
                    if not trailer:
                        raise ValueError("truncated chunk trailers")
                    if trailer == b"\r\n":
                        break
                    trailer_bytes += len(trailer)
                    if trailer_bytes > 65536 or not trailer.endswith(b"\r\n"):
                        raise ValueError("malformed chunk trailer")
                    name, separator, _value = trailer[:-2].partition(b":")
                    if not separator or not re.fullmatch(
                        rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name
                    ):
                        raise ValueError("malformed chunk trailer")
                    if name.lower() in {b"content-length", b"transfer-encoding"}:
                        raise ValueError("forbidden framing trailer")
                break
            total += size
            if total > transport_limit:
                raise BodyTooLarge()
            if total > limit:
                too_large = True
            try:
                chunk = self.rfile.read(size)
            except TimeoutError as exc:
                raise RequestReadTimeout() from exc
            if len(chunk) != size:
                raise ValueError("truncated chunk")
            if not too_large:
                chunks.append(chunk)
            try:
                terminator = self.rfile.read(2)
            except TimeoutError as exc:
                raise RequestReadTimeout() from exc
            if terminator != b"\r\n":
                raise ValueError("malformed chunk terminator")
        if too_large:
            raise BodyTooLarge()
        return b"".join(chunks)

    def _write(self, response: Response) -> None:
        try:
            self.send_response(response.status)
            headers = dict(response.headers)
            output_headers = response.header_items or list(headers.items())
            for name, value in output_headers:
                if name.lower() == "content-length":
                    continue
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if self.command != "HEAD" and response.body:
                self.wfile.write(response.body)
                self.wfile.flush()
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "response write failed (%s)", type(exc).__name__
            )
            return

    def _send_json(self, status: int, value: Any) -> None:
        body = dumps(value).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "error response write failed (%s)", type(exc).__name__
            )

    def log_message(self, format: str, *args: Any) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="consideration")
    parser.add_argument(
        "--config", "-c", default="config.yaml", help="path to config.yaml"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the configuration and exit",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"consideration: {exc}", file=sys.stderr)
        return 2

    if args.check:
        print(f"config ok: {config.config_hash}")
        return 0

    if config.pipeline.repair.max_attempts > 0 and not config.pipeline.client_bound:
        print(
            "consideration: warning: repair.max_attempts > 0 but the "
            "client_bound pipeline is empty; no repair can occur",
            file=sys.stderr,
        )
    if not config.pipeline.client_bound:
        print(
            "consideration: warning: the client_bound pipeline is empty; "
            "server streams are relayed without output checks",
            file=sys.stderr,
        )

    try:
        app = build_app(config)
    except StageFailure as exc:
        print(f"consideration: {exc}", file=sys.stderr)
        return 1

    try:
        server = ConsiderationHTTPServer((config.listen_host, config.listen_port), app)
    except OSError as exc:
        print(f"consideration: cannot listen: {exc}", file=sys.stderr)
        app.plugins.shutdown_all()
        return 1

    host, port = server.server_address[:2]
    print(f"consideration listening on {host}:{port}", file=sys.stderr)

    def stop(signum: int, frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, stop)
    except (AttributeError, ValueError):
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        app.plugins.shutdown_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
