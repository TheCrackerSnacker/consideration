from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from consideration.app import App
from consideration.config import load_config
from consideration.plugin import PluginManager
from consideration.server import ConsiderationHTTPServer
from consideration.upstream import UpstreamClient
from tests.fake_upstream import FakeUpstream, json_response
from tests.helpers import make_config

REQUEST = {
    "model": "gpt-x",
    "messages": [{"role": "user", "content": "Hello"}],
}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.managers: list[PluginManager] = []
        self.servers: list[ThreadingHTTPServer] = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for manager in self.managers:
            manager.shutdown_all()
        self.tmp.cleanup()

    def start_app_server(
        self,
        *,
        host_extra: str = "",
        base_url: str = "http://127.0.0.1:9/v1",
        upstream=None,
    ):
        config = make_config(
            self.root,
            server_bound=False,
            client_bound=False,
            host_extra=host_extra,
            base_url=base_url,
        )
        manager = PluginManager(config, telemetry=None)
        manager.start_all()
        self.managers.append(manager)
        app = App(
            config,
            manager,
            telemetry=None,
            upstream=upstream or UpstreamClient(config),
        )
        server = ConsiderationHTTPServer(("127.0.0.1", 0), app)
        self.servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def request(
        self,
        server,
        method="POST",
        path="/v1/chat/completions",
        body=None,
        headers=None,
    ):
        payload = json.dumps(REQUEST).encode() if body is None else body
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request(
            method, path, payload, headers or {"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), data

    def test_post_chat_success(self):
        upstream = _start_upstream(self.servers)
        server = self.start_app_server(
            base_url=f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        )
        status, headers, body = self.request(server)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body)["object"], "chat.completion")

    @unittest.skipUnless(socket.has_ipv6, "IPv6 is unavailable")
    def test_ipv6_listener_starts(self):
        config = make_config(self.root, server_bound=False, client_bound=False)
        config_path = Path(config.source_path)
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace("127.0.0.1:0", '"[::1]:0"'),
            encoding="utf-8",
        )
        config = load_config(config_path)
        manager = PluginManager(config, telemetry=None)
        manager.start_all()
        self.managers.append(manager)
        app = App(config, manager, telemetry=None, upstream=UpstreamClient(config))
        try:
            server = ConsiderationHTTPServer((config.listen_host, 0), app)
        except OSError as exc:
            self.skipTest(f"IPv6 loopback is unavailable: {exc}")
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.assertEqual(server.server_address[0], "::1")

    def test_get_passthrough(self):
        upstream = _start_upstream(self.servers, status=204, body=b"")
        server = self.start_app_server(
            base_url=f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        )
        status, _, body = self.request(server, "GET", "/v1/models", b"")
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")

    def test_repeated_set_cookie_fields_are_preserved_on_wire(self):
        upstream = _start_upstream(
            self.servers,
            status=200,
            body=b"ok",
            headers=[
                ("Set-Cookie", "first=1; Path=/"),
                ("Set-Cookie", "second=2; Path=/"),
                ("Via", "upstream"),
                ("Connection", "X-Private"),
                ("X-Private", "remove"),
                ("Content-Security-Policy", "default-src 'none'"),
            ],
        )
        server = self.start_app_server(
            base_url=f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        )
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request("GET", "/v1/models")
        response = connection.getresponse()
        response_headers = response.getheaders()
        self.assertEqual(
            [value for name, value in response_headers if name.lower() == "set-cookie"],
            ["first=1; Path=/", "second=2; Path=/"],
        )
        self.assertEqual(dict(response_headers)["Via"], "upstream")
        self.assertEqual(
            dict(response_headers)["Content-Security-Policy"], "default-src 'none'"
        )
        self.assertNotIn("X-Private", dict(response_headers))
        response.read()
        connection.close()

    def test_chunked_request_body(self):
        server = self.start_app_server(upstream=FakeUpstream([json_response()]))
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        payload = json.dumps(REQUEST).encode()
        connection.putrequest("POST", "/v1/chat/completions")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        connection.send(f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
        response = connection.getresponse()
        response_body = response.read()
        self.assertEqual(response.status, 200, response_body)
        connection.close()

    def test_oversized_body_is_rejected(self):
        server = self.start_app_server(host_extra="  max_body_bytes: 10\n")
        status, _, body = self.request(server, body=b"{" + b"x" * 20)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")

    def raw_request(self, server, payload: bytes) -> bytes:
        connection = socket.create_connection(server.server_address, timeout=2)
        connection.settimeout(2)
        connection.sendall(payload)
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        try:
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            connection.close()
        return b"".join(chunks)

    def test_passthrough_body_bypasses_pipeline_limit(self):
        server = self.start_app_server(
            host_extra="  max_body_bytes: 10\n",
            upstream=FakeUpstream([json_response()]),
        )
        status, _, _ = self.request(
            server, method="POST", path="/v1/other", body=b"x" * 100
        )
        self.assertEqual(status, 200)

    def test_truncated_fixed_length_body_is_rejected(self):
        server = self.start_app_server()
        response = self.raw_request(
            server,
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\nContent-Length: 10\r\n\r\nabc",
        )
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_malformed_chunk_terminator_is_rejected(self):
        server = self.start_app_server()
        response = self.raw_request(
            server,
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3\r\nabcXX0\r\n\r\n",
        )
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_invalid_chunk_size_is_rejected(self):
        server = self.start_app_server()
        response = self.raw_request(
            server,
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"+3\r\nabc\r\n0\r\n\r\n",
        )
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_chunk_trailers_are_consumed_and_validated(self):
        server = self.start_app_server(upstream=FakeUpstream([json_response()]))
        payload = json.dumps(REQUEST).encode()
        chunk = f"{len(payload):x}\r\n".encode() + payload + b"\r\n"
        request = (
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\nTransfer-Encoding: chunked\r\n\r\n"
            + chunk
            + b"0\r\nX-Trace: done\r\n\r\n"
        )
        response = self.raw_request(server, request)
        self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0], response)

    def test_conflicting_framing_headers_are_rejected(self):
        server = self.start_app_server()
        response = self.raw_request(
            server,
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        )
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_conflicting_duplicate_content_lengths_are_rejected(self):
        server = self.start_app_server()
        response = self.raw_request(
            server,
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Length: 1\r\nContent-Length: 2\r\n\r\na",
        )
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_malformed_chunk_trailer_is_rejected(self):
        server = self.start_app_server()
        response = self.raw_request(
            server,
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"0\r\nnot-a-header\r\n\r\n",
        )
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_stalled_body_read_has_a_deadline(self):
        server = self.start_app_server(host_extra="  read_timeout_ms: 200\n")
        connection = socket.create_connection(server.server_address, timeout=2)
        connection.settimeout(2)
        started = time.monotonic()
        connection.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\nContent-Length: 20\r\n\r\n{}"
        )
        response = connection.recv(4096)
        connection.close()
        self.assertIn(b" 408 ", response.split(b"\r\n", 1)[0])
        self.assertLess(time.monotonic() - started, 1.5)

    def test_slow_trickle_cannot_extend_absolute_read_deadline(self):
        server = self.start_app_server(host_extra="  read_timeout_ms: 250\n")
        connection = socket.create_connection(server.server_address, timeout=2)
        connection.settimeout(2)
        connection.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\nContent-Length: 20\r\n\r\n{"
        )

        def trickle():
            for _ in range(19):
                time.sleep(0.1)
                try:
                    connection.sendall(b"x")
                except OSError:
                    return

        sender = threading.Thread(target=trickle, daemon=True)
        started = time.monotonic()
        sender.start()
        response = connection.recv(4096)
        connection.close()
        sender.join(timeout=1)
        self.assertIn(b" 408 ", response.split(b"\r\n", 1)[0])
        self.assertLess(time.monotonic() - started, 1.5)


class _UpstreamHandler(BaseHTTPRequestHandler):
    status = 200
    payload = (
        b'{"id":"upstream","object":"chat.completion","created":1,'
        b'"model":"gpt-x","choices":[{"index":0,"message":'
        b'{"role":"assistant","content":"Hello"},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'
    )
    extra_headers = []

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        for name, value in self.extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(self.payload)

    do_POST = do_GET

    def log_message(self, *_args):
        pass


def _start_upstream(servers, *, status=200, body=None, headers=None):
    handler = type("TestUpstreamHandler", (_UpstreamHandler,), {})
    handler.status = status
    if body is not None:
        handler.payload = body
    handler.extra_headers = headers or []
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    servers.append(server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    unittest.main()
