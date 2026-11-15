"""HTTP listener, request framing, and the command-line entry point."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .app import App, InboundRequest, Response, build_app
from .config import ConfigError, load_config
from .errors import StageFailure
from .util import dumps


class BodyTooLarge(Exception):
    pass


class ConsiderationHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: App):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"consideration/{__version__}"

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
        try:
            body = self._read_body(app.config.max_body_bytes)
        except BodyTooLarge:
            self._send_json(
                413,
                {
                    "error": {
                        "message": "Request body exceeds max_body_bytes.",
                        "type": "consideration_request_error",
                        "param": None,
                        "code": "invalid_request",
                    }
                },
            )
            return
        except ValueError:
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

        headers = {name: value for name, value in self.headers.items()}
        request = InboundRequest(
            method=self.command,
            path=self.path,
            headers=headers,
            body=body,
            remote_addr=self.client_address[0] if self.client_address else "",
        )
        try:
            response = app.handle(request)
        except Exception:
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

    def _read_body(self, limit: int) -> bytes:
        transfer = self.headers.get("Transfer-Encoding", "")
        if "chunked" in transfer.lower():
            return self._read_chunked(limit)
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0:
            raise ValueError("invalid Content-Length")
        if length > limit:
            raise BodyTooLarge()
        return self.rfile.read(length)

    def _read_chunked(self, limit: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(65536)
            if not line:
                raise ValueError("truncated chunked body")
            size_text = line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise ValueError("invalid chunk size") from exc
            if size == 0:
                while True:
                    trailer = self.rfile.readline(65536)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            total += size
            if total > limit:
                raise BodyTooLarge()
            chunk = self.rfile.read(size)
            if len(chunk) != size:
                raise ValueError("truncated chunk")
            chunks.append(chunk)
            self.rfile.read(2)
        return b"".join(chunks)

    def _write(self, response: Response) -> None:
        try:
            self.send_response(response.status)
            headers = dict(response.headers)
            for name, value in headers.items():
                if name.lower() == "content-length":
                    continue
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if self.command != "HEAD" and response.body:
                self.wfile.write(response.body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_json(self, status: int, value: Any) -> None:
        body = dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

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
