from __future__ import annotations

import json
import unittest

from consideration.terminal import (
    exhaustion_terminal,
    host_failure,
    passthrough_error,
    policy_terminal,
    request_error,
    server_failure,
)

VIOLATION = {
    "plugin": "acme/style-lint",
    "plugin_version": "1.2.0",
    "stage_id": "guard-out",
    "code": "style.em_dash",
    "severity": "error",
    "message": "Output contains an em dash.",
    "hint": "Rewrite without em dashes.",
    "retryable": True,
    "spans": [{"path": "/choices/0/message/content", "start": 39, "end": 42}],
}


class TerminalTests(unittest.TestCase):
    def test_server_bound_violation(self):
        terminal = policy_terminal("req_1", "server_bound", [VIOLATION])
        body = json.loads(terminal.body_bytes())
        self.assertEqual(terminal.status, 400)
        self.assertEqual(terminal.verdict, "blocked")
        self.assertEqual(body["error"]["type"], "consideration_violation")
        self.assertEqual(body["error"]["message"], VIOLATION["message"])
        self.assertEqual(body["error"]["code"], VIOLATION["code"])
        self.assertIsNone(body["error"]["param"])
        self.assertEqual(body["consideration"]["direction"], "server_bound")
        self.assertNotIn("repair", body["consideration"])
        self.assertEqual(body["consideration"]["violations"], [VIOLATION])

    def test_exhaustion_violation(self):
        repair = {"attempts": 2, "max_attempts": 2, "exhausted": True, "cause": "violation"}
        terminal = exhaustion_terminal("req_1", [VIOLATION], repair, "violation")
        body = json.loads(terminal.body_bytes())
        self.assertEqual(terminal.status, 400)
        self.assertEqual(body["error"]["type"], "consideration_violation")
        self.assertEqual(body["consideration"]["repair"], repair)

    def test_exhaustion_http_502(self):
        repair = {"attempts": 2, "max_attempts": 2, "exhausted": True, "cause": "violation"}
        terminal = exhaustion_terminal("req_1", [VIOLATION], repair, "http_502")
        body = json.loads(terminal.body_bytes())
        self.assertEqual(terminal.status, 502)
        self.assertEqual(terminal.verdict, "error")
        self.assertEqual(body["error"]["type"], "consideration_plugin_error")
        self.assertEqual(body["error"]["code"], "repair_exhausted")
        self.assertEqual(
            body["error"]["message"],
            "Client-bound validation failed after 2 repair attempts.",
        )
        self.assertEqual(body["consideration"]["violations"], [VIOLATION])

    def test_host_failure_has_no_violations(self):
        terminal = host_failure("req_1", "plugin_timeout", "Stage x failed: timeout", "client_bound")
        body = json.loads(terminal.body_bytes())
        self.assertEqual(terminal.status, 500)
        self.assertEqual(body["error"]["type"], "consideration_plugin_error")
        self.assertNotIn("violations", body["consideration"])
        self.assertEqual(body["consideration"]["direction"], "client_bound")

    def test_request_error_is_400(self):
        terminal = request_error("req_1", "model must be a non-empty string")
        body = json.loads(terminal.body_bytes())
        self.assertEqual(terminal.status, 400)
        self.assertEqual(body["error"]["type"], "consideration_request_error")
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertNotIn("direction", body["consideration"])

    def test_server_failure_status(self):
        self.assertEqual(
            server_failure("req_1", "server_unreachable", "Server did not respond.").status, 502
        )
        self.assertEqual(
            server_failure("req_1", "server_timeout", "Server timed out.").status, 504
        )
        terminal = server_failure(
            "req_1",
            "server_unreachable",
            "Server did not respond.",
            {"attempts": 1, "max_attempts": 2, "exhausted": False},
        )
        body = json.loads(terminal.body_bytes())
        self.assertEqual(body["error"]["type"], "server_error")
        self.assertNotIn("direction", body["consideration"])
        self.assertNotIn("cause", body["consideration"]["repair"])

    def test_passthrough_error_is_verbatim(self):
        raw = b'{"error":{"message":"nope","type":"invalid_request_error","code":"x"}}'
        terminal = passthrough_error("req_1", 401, raw, {"content-type": "application/json"})
        self.assertEqual(terminal.status, 401)
        self.assertEqual(terminal.body_bytes(), raw)
        self.assertIsNone(terminal.host_code)
        self.assertEqual(terminal.verdict, "error")


if __name__ == "__main__":
    unittest.main()
