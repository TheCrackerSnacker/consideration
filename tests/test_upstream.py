from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from consideration.config import load_config
from consideration.upstream import (
    UpstreamClient,
    filter_response_header_items,
    filter_response_headers,
    is_credential_header,
)


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def client(
        self, base_url: str = "http://127.0.0.1:9/v1", extra: str = ""
    ) -> UpstreamClient:
        text = (
            "host:\n"
            "  listen: 127.0.0.1:0\n"
            "server:\n"
            f"  base_url: {base_url}\n"
            "  timeout_ms: 1000\n" + extra + "plugins:\n"
            "  entries: []\n"
            "pipeline:\n"
            "  server_bound: []\n"
            "  client_bound: []\n"
        )
        path = self.root / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return UpstreamClient(load_config(path))

    def test_target_maps_v1_once(self):
        client = self.client("http://example.test/v1")
        self.assertEqual(client.target("/v1/chat/completions"), "/v1/chat/completions")
        self.assertEqual(client.target("/v1/models"), "/v1/models")
        self.assertEqual(client.target("/health"), "/v1/health")

    def test_target_without_base_path(self):
        client = self.client("http://example.test")
        self.assertEqual(client.target("/v1/chat/completions"), "/v1/chat/completions")

    def test_prepare_headers_filters_and_forwards(self):
        client = self.client(extra=("  headers:\n    x-custom: server\n"))
        headers = client.prepare_headers(
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer sk-client",
                "Cookie": "session=secret",
                "Proxy-Authorization": "Basic x",
                "x-api-key": "nope",
                "User-Agent": "openai-python/1.0",
                "X-Request-Id": "req_1",
                "x-client-trace": "abc",
                "Connection": "keep-alive",
                "Content-Length": "999",
            },
            42,
        )
        self.assertEqual(headers["authorization"], "Bearer sk-client")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["User-Agent"], "openai-python/1.0")
        self.assertEqual(headers["X-Request-Id"], "req_1")
        self.assertEqual(headers["x-client-trace"], "abc")
        self.assertEqual(headers["x-custom"], "server")
        self.assertEqual(headers["content-length"], "42")
        self.assertNotIn("cookie", {key.lower() for key in headers})
        self.assertNotIn("proxy-authorization", {key.lower() for key in headers})
        self.assertNotIn("x-api-key", {key.lower() for key in headers})
        self.assertNotIn("connection", {key.lower() for key in headers})

    def test_connection_header_nominates_additional_request_headers(self):
        headers = self.client().prepare_headers(
            {
                "Connection": "X-Client-Trace, keep-alive",
                "X-Client-Trace": "must-not-forward",
                "User-Agent": "preserved",
            },
            None,
        )
        self.assertNotIn("x-client-trace", {name.lower() for name in headers})
        self.assertEqual(headers["User-Agent"], "preserved")

    def test_credential_detection(self):
        self.assertTrue(is_credential_header("Authorization"))
        self.assertTrue(is_credential_header("x-api-key"))
        self.assertTrue(is_credential_header("x-auth-token"))
        self.assertTrue(is_credential_header("client-secret"))
        self.assertTrue(is_credential_header("Set-Cookie"))
        self.assertFalse(is_credential_header("content-type"))

    def test_response_headers_are_filtered(self):
        filtered = filter_response_headers(
            [
                ("Content-Type", "application/json"),
                ("Content-Length", "12"),
                ("Transfer-Encoding", "chunked"),
                ("Connection", "keep-alive"),
                ("OpenAI-Processing-Ms", "120"),
            ]
        )
        self.assertEqual(
            filtered,
            {
                "Content-Type": "application/json",
                "OpenAI-Processing-Ms": "120",
            },
        )

    def test_response_connection_tokens_and_repeated_headers(self):
        filtered = filter_response_header_items(
            [
                ("Connection", "X-Internal, keep-alive"),
                ("X-Internal", "remove"),
                ("Set-Cookie", "a=1; Path=/"),
                ("Set-Cookie", "b=2; Path=/"),
                ("Server", "backend"),
                ("Via", "1.1 gateway"),
                ("Alt-Svc", 'h3=":443"'),
                ("Location", "/next"),
                ("Refresh", "5; url=/next"),
                ("WWW-Authenticate", "Bearer"),
                ("Content-Security-Policy", "default-src 'none'"),
                ("Strict-Transport-Security", "max-age=31536000"),
                ("Access-Control-Allow-Origin", "*"),
                ("Content-Length", "10"),
            ]
        )
        self.assertEqual(
            filtered,
            [
                ("Set-Cookie", "a=1; Path=/"),
                ("Set-Cookie", "b=2; Path=/"),
                ("Server", "backend"),
                ("Via", "1.1 gateway"),
                ("Alt-Svc", 'h3=":443"'),
                ("Location", "/next"),
                ("Refresh", "5; url=/next"),
                ("WWW-Authenticate", "Bearer"),
                ("Content-Security-Policy", "default-src 'none'"),
                ("Strict-Transport-Security", "max-age=31536000"),
                ("Access-Control-Allow-Origin", "*"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
