from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from consideration.app import App
from consideration.plugin import PluginManager
from consideration.server import ConsiderationHTTPServer
from consideration.upstream import UpstreamClient
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
        self, *, host_extra: str = "", base_url: str = "http://127.0.0.1:9/v1"
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
        app = App(config, manager, telemetry=None, upstream=UpstreamClient(config))
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

    def test_get_passthrough(self):
        upstream = _start_upstream(self.servers, status=204, body=b"")
        server = self.start_app_server(
            base_url=f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        )
        status, _, body = self.request(server, "GET", "/v1/models", b"")
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")

    def test_chunked_request_body(self):
        upstream = _start_upstream(self.servers)
        server = self.start_app_server(
            base_url=f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        )
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        payload = json.dumps(REQUEST).encode()
        connection.putrequest("POST", "/v1/chat/completions")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        connection.send(f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        connection.close()

    def test_oversized_body_is_rejected(self):
        server = self.start_app_server(host_extra="  max_body_bytes: 10\n")
        status, _, body = self.request(server, body=b"{" + b"x" * 20)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")


class _UpstreamHandler(BaseHTTPRequestHandler):
    status = 200
    payload = (
        b'{"id":"upstream","object":"chat.completion","created":1,'
        b'"model":"gpt-x","choices":[{"index":0,"message":'
        b'{"role":"assistant","content":"Hello"},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'
    )

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    do_POST = do_GET

    def log_message(self, *_args):
        pass


def _start_upstream(servers, *, status=200, body=None):
    handler = type("TestUpstreamHandler", (_UpstreamHandler,), {})
    handler.status = status
    if body is not None:
        handler.payload = body
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    servers.append(server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    unittest.main()
