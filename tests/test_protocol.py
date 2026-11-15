from __future__ import annotations

import io
import json
import unittest

from consideration.errors import PluginProtocolError
from consideration.protocol import read_frame, write_frame


def framed(payload: bytes) -> io.BytesIO:
    return io.BytesIO(b"Content-Length: %d\r\n\r\n" % len(payload) + payload)


class FramingTests(unittest.TestCase):
    def test_round_trip(self):
        buffer = io.BytesIO()
        write_frame(buffer, {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        buffer.seek(0)
        self.assertEqual(
            read_frame(buffer, 1024),
            {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
        )

    def test_lf_header_terminator(self):
        payload = json.dumps({"a": 1}).encode()
        stream = io.BytesIO(b"Content-Length: %d\n\n" % len(payload) + payload)
        self.assertEqual(read_frame(stream, 1024), {"a": 1})

    def test_crlf_inside_payload_is_preserved(self):
        payload = json.dumps({"a": "x\r\ny"}).encode()
        self.assertEqual(read_frame(framed(payload), 1024)["a"], "x\r\ny")

    def test_eof_before_frame(self):
        self.assertIsNone(read_frame(io.BytesIO(b""), 1024))

    def test_oversized_frame_rejected(self):
        payload = json.dumps({"a": "x" * 100}).encode()
        with self.assertRaises(PluginProtocolError):
            read_frame(framed(payload), 10)

    def test_short_body_rejected(self):
        stream = io.BytesIO(b"Content-Length: 10\r\n\r\n{}")
        with self.assertRaises(PluginProtocolError):
            read_frame(stream, 1024)

    def test_missing_content_length_rejected(self):
        stream = io.BytesIO(b"X-Test: 1\r\n\r\n{}")
        with self.assertRaises(PluginProtocolError):
            read_frame(stream, 1024)

    def test_invalid_utf8_rejected(self):
        stream = io.BytesIO(b"Content-Length: 2\r\n\r\n\xff\xfe")
        with self.assertRaises(PluginProtocolError):
            read_frame(stream, 1024)

    def test_non_object_rejected(self):
        payload = b"[1,2]"
        with self.assertRaises(PluginProtocolError):
            read_frame(framed(payload), 1024)


if __name__ == "__main__":
    unittest.main()
