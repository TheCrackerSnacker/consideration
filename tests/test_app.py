from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY

from consideration.app import App, InboundRequest
from consideration.plugin import PluginManager
from consideration.replay import replay
from consideration.upstream import UpstreamTimeout, UpstreamUnreachable
from consideration.util import dumps
from tests.assertions import SubsetAssertions
from tests.fake_upstream import (
    FakeUpstream,
    chat_body,
    json_response,
    raw_response,
)
from tests.helpers import make_config

VIOLATION = {
    "code": "style.em_dash",
    "severity": "error",
    "message": "Output contains an em dash.",
    "hint": "Rewrite without em dashes.",
    "retryable": True,
    "spans": [{"path": "/choices/0/message/content", "start": 0, "end": 1}],
}

REQUEST = {
    "model": "gpt-x",
    "messages": [{"role": "user", "content": "Explain recursion."}],
    "temperature": 0.2,
}


def sse_events(data: bytes) -> list:
    events = []
    for block in data.decode("utf-8").split("\n\n"):
        if not block:
            continue
        line = block[len("data: ") :]
        events.append(line if line == "[DONE]" else json.loads(line))
    return events


class AppTests(SubsetAssertions, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.managers: list[PluginManager] = []

    def tearDown(self):
        for manager in self.managers:
            manager.shutdown_all()
        self.tmp.cleanup()

    def make_app(
        self,
        *,
        plugin_config: dict | None = None,
        server_bound: bool = True,
        client_bound: bool = True,
        repair: dict | None = None,
        responses: list | None = None,
        extra: str = "",
        on_error: str = "fail_closed",
    ):
        config = make_config(
            self.root,
            plugin_config=plugin_config or {},
            server_bound=server_bound,
            client_bound=client_bound,
            repair=repair,
            extra=extra,
            on_error=on_error,
        )
        manager = PluginManager(config, telemetry=None)
        manager.start_all()
        self.managers.append(manager)
        upstream = FakeUpstream(responses or [json_response()])
        app = App(config, manager, telemetry=None, upstream=upstream)
        return app, upstream, config

    def post(
        self,
        app: App,
        body: dict | bytes | None = None,
        headers: dict[str, str] | None = None,
    ):
        if body is None:
            payload = dumps(REQUEST).encode("utf-8")
        elif isinstance(body, bytes):
            payload = body
        else:
            payload = dumps(body).encode("utf-8")
        return app.handle(
            InboundRequest(
                method="POST",
                path="/v1/chat/completions",
                headers=headers or {"content-type": "application/json"},
                body=payload,
                remote_addr="127.0.0.1:1234",
            )
        )

    # -- server-bound --------------------------------------------------

    def test_server_bound_terminal_is_400_and_skips_server(self):
        app, upstream, _ = self.make_app(
            plugin_config={
                "mode": "direction_gate",
                "fail_direction": "server_bound",
                "fail_attempts": 99,
                "violations": [VIOLATION],
            },
            client_bound=False,
        )
        response = self.post(app)
        self.assertEqual(response.status, 400)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["type"], "consideration_violation")
        self.assertEqual(body["error"]["message"], VIOLATION["message"])
        self.assertEqual(body["error"]["code"], VIOLATION["code"])
        self.assertEqual(body["consideration"]["direction"], "server_bound")
        self.assertNotIn("repair", body["consideration"])
        self.assertEqual(body["consideration"]["violations"][0]["stage_id"], "in")
        self.assertEqual(response.headers["x-consideration-verdict"], "blocked")
        self.assertEqual(response.headers["x-consideration-code"], VIOLATION["code"])
        self.assertEqual(response.headers["x-consideration-repairs"], "0")
        self.assertEqual(upstream.calls, [])

    def test_invalid_request_body(self):
        app, upstream, _ = self.make_app(client_bound=False)
        response = self.post(app, {"model": "", "messages": []})
        self.assertEqual(response.status, 400)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["type"], "consideration_request_error")
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertEqual(upstream.calls, [])

    def test_malformed_json(self):
        app, upstream, _ = self.make_app(client_bound=False)
        response = self.post(app, b"{not json")
        self.assertEqual(response.status, 400)
        self.assertEqual(upstream.calls, [])

    # -- passthrough ---------------------------------------------------

    def test_success_without_client_bound_is_byte_identical(self):
        upstream_body = chat_body("Hi.", provider_specific={"kept": True})
        app, upstream, _ = self.make_app(
            plugin_config={"mode": "continue"},
            client_bound=False,
            responses=[json_response(upstream_body)],
        )
        response = self.post(app)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, json_response(upstream_body).body)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.headers["x-consideration-repairs"], "0")
        self.assertIn("x-consideration-request-id", response.headers)
        self.assertNotIn("x-consideration-verdict", response.headers)
        self.assertNotIn("x-consideration-code", response.headers)

    def test_non_chat_path_is_passthrough(self):
        app, upstream, _ = self.make_app(client_bound=False)
        response = app.handle(
            InboundRequest(
                method="GET",
                path="/v1/models",
                headers={"authorization": "Bearer sk-client"},
                body=b"",
            )
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(len(upstream.calls), 1)
        self.assertEqual(upstream.calls[0]["method"], "GET")
        self.assertEqual(upstream.calls[0]["path"], "/v1/models")

    def test_authorization_is_forwarded_but_not_exposed(self):
        app, upstream, _ = self.make_app(
            plugin_config={"mode": "meta_patch"},
            client_bound=False,
            responses=[json_response()],
        )
        headers = {
            "content-type": "application/json",
            "authorization": "Bearer sk-client",
            "cookie": "session=secret",
            "user-agent": "openai-python/1.0",
            "x-request-id": "req_client",
            "x-secret-key": "nope",
        }
        response = self.post(app, headers=headers)
        self.assertEqual(response.status, 200)
        # The fake sees the unfiltered client headers; UpstreamClient is
        # responsible for filtering and is tested separately.
        self.assertEqual(
            upstream.calls[0]["headers"]["authorization"], "Bearer sk-client"
        )
        meta = upstream.last_body()["x_meta"]
        self.assertEqual(meta["headers"]["user-agent"], "openai-python/1.0")
        self.assertEqual(meta["headers"]["x-request-id"], "req_client")
        self.assertNotIn("authorization", meta["headers"])
        self.assertNotIn("cookie", meta["headers"])
        self.assertNotIn("x-secret-key", meta["headers"])
        self.assertNotIn("sk-client", response.body.decode("utf-8"))

    # -- repair --------------------------------------------------------

    def test_repair_accepts_second_generation(self):
        app, upstream, _ = self.make_app(
            plugin_config={
                "mode": "direction_gate",
                "fail_direction": "client_bound",
                "fail_attempts": 1,
                "violations": [VIOLATION],
            },
            repair={"max_attempts": 2},
            responses=[
                json_response(chat_body("bad \u2014 text")),
                json_response(chat_body("good text")),
            ],
        )
        response = self.post(app)
        self.assertEqual(response.status, 200)
        body = json.loads(response.body)
        self.assertEqual(body["choices"][0]["message"]["content"], "good text")
        self.assertEqual(response.headers["x-consideration-repairs"], "1")
        self.assertEqual(len(upstream.calls), 2)

        repair_request = json.loads(upstream.calls[1]["body"].decode("utf-8"))
        self.assertFalse(repair_request["stream"])
        self.assertNotIn("stream_options", repair_request)
        self.assertEqual(repair_request["temperature"], 0.2)
        self.assertEqual(repair_request["messages"][0], REQUEST["messages"][0])
        self.assertEqual(repair_request["messages"][1]["role"], "assistant")
        self.assertEqual(repair_request["messages"][1]["content"], "bad \u2014 text")
        self.assertEqual(repair_request["messages"][2]["role"], "user")
        self.assertTrue(
            repair_request["messages"][2]["content"].startswith(
                "Your previous response was rejected by policy."
            )
        )

    def test_repair_exhaustion_violation(self):
        app, upstream, _ = self.make_app(
            plugin_config={
                "mode": "direction_gate",
                "fail_direction": "client_bound",
                "fail_attempts": 99,
                "violations": [VIOLATION],
            },
            repair={"max_attempts": 2, "on_exhaustion": "violation"},
            responses=[json_response(chat_body("bad \u2014 text"))],
        )
        response = self.post(app)
        self.assertEqual(response.status, 400)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["type"], "consideration_violation")
        self.assertEqual(body["error"]["code"], VIOLATION["code"])
        self.assertEqual(
            body["consideration"]["repair"],
            {
                "attempts": 2,
                "max_attempts": 2,
                "exhausted": True,
                "cause": "violation",
            },
        )
        self.assertEqual(response.headers["x-consideration-repairs"], "2")
        self.assertEqual(len(upstream.calls), 3)

    def test_repair_exhaustion_http_502(self):
        app, upstream, _ = self.make_app(
            plugin_config={
                "mode": "direction_gate",
                "fail_direction": "client_bound",
                "fail_attempts": 99,
                "violations": [VIOLATION],
            },
            repair={"max_attempts": 1, "on_exhaustion": "http_502"},
            responses=[json_response(chat_body("bad \u2014 text"))],
        )
        response = self.post(app)
        self.assertEqual(response.status, 502)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["type"], "consideration_plugin_error")
        self.assertEqual(body["error"]["code"], "repair_exhausted")
        self.assertEqual(
            body["error"]["message"],
            "Client-bound validation failed after 1 repair attempts.",
        )
        self.assertEqual(
            body["consideration"]["violations"][0]["code"], VIOLATION["code"]
        )
        self.assertEqual(response.headers["x-consideration-code"], "repair_exhausted")
        self.assertEqual(len(upstream.calls), 2)

    def test_non_retryable_short_circuits(self):
        non_retryable = dict(VIOLATION, retryable=False)
        app, upstream, _ = self.make_app(
            plugin_config={
                "mode": "direction_gate",
                "fail_direction": "client_bound",
                "fail_attempts": 99,
                "violations": [non_retryable],
            },
            repair={"max_attempts": 2},
            responses=[json_response(chat_body("bad"))],
        )
        response = self.post(app)
        self.assertEqual(response.status, 400)
        body = json.loads(response.body)
        self.assertEqual(body["consideration"]["repair"]["cause"], "non_retryable")
        self.assertEqual(body["consideration"]["repair"]["attempts"], 0)
        self.assertEqual(len(upstream.calls), 1)

    def test_max_attempts_zero_disables_repair(self):
        app, upstream, _ = self.make_app(
            plugin_config={
                "mode": "direction_gate",
                "fail_direction": "client_bound",
                "fail_attempts": 99,
                "violations": [VIOLATION],
            },
            repair={"max_attempts": 0},
            responses=[json_response(chat_body("bad"))],
        )
        response = self.post(app)
        self.assertEqual(response.status, 400)
        body = json.loads(response.body)
        self.assertEqual(body["consideration"]["repair"]["attempts"], 0)
        self.assertEqual(len(upstream.calls), 1)

    def test_total_timeout_exhaustion(self):
        app, upstream, _ = self.make_app(
            plugin_config={
                "mode": "direction_gate",
                "fail_direction": "client_bound",
                "fail_attempts": 99,
                "violations": [VIOLATION],
                "delay_ms": 20,
            },
            repair={"max_attempts": 5, "total_timeout_ms": 1},
            responses=[json_response(chat_body("bad"))],
        )
        response = self.post(app)
        self.assertEqual(response.status, 400)
        body = json.loads(response.body)
        self.assertEqual(body["consideration"]["repair"]["cause"], "timeout")
        self.assertEqual(len(upstream.calls), 1)

    def test_context_is_the_final_server_request(self):
        app, upstream, _ = self.make_app(
            plugin_config={"mode": "attempt_patch"},
            responses=[json_response()],
        )
        response = self.post(app)
        self.assertEqual(response.status, 200)
        body = json.loads(response.body)
        self.assertEqual(body["x_attempt"], 0)
        self.assertEqual(body["x_context"]["server_request"]["kind"], "chat.request")
        self.assertEqual(
            body["x_context"]["server_request"]["body"],
            json.loads(upstream.calls[0]["body"].decode("utf-8")),
        )

    def test_repair_does_not_rerun_server_bound(self):
        app, upstream, config = self.make_app(
            plugin_config={
                "mode": "direction_gate",
                "fail_direction": "client_bound",
                "fail_attempts": 1,
                "violations": [VIOLATION],
            },
            repair={"max_attempts": 2},
            responses=[
                json_response(chat_body("bad")),
                json_response(chat_body("good")),
            ],
        )
        response = self.post(app)
        self.assertEqual(response.status, 200)
        journal = self._read_journal()
        self.assertEqual(len(journal["direction_results"]["server_bound"]["stages"]), 1)
        self.assertEqual(
            len(journal["direction_results"]["client_bound"]["attempts"]), 2
        )
        self.assertFalse(journal["repairs"][0]["server_bound_rerun"])

    # -- server failures ----------------------------------------------

    def test_server_unreachable(self):
        app, _, _ = self.make_app(
            client_bound=False, responses=[UpstreamUnreachable("down")]
        )
        response = self.post(app)
        self.assertEqual(response.status, 502)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["type"], "server_error")
        self.assertEqual(body["error"]["code"], "server_unreachable")
        self.assertNotIn("direction", body["consideration"])
        self.assertEqual(response.headers["x-consideration-code"], "server_unreachable")

    def test_server_timeout(self):
        app, _, _ = self.make_app(
            client_bound=False, responses=[UpstreamTimeout("slow")]
        )
        response = self.post(app)
        self.assertEqual(response.status, 504)
        self.assertEqual(json.loads(response.body)["error"]["code"], "server_timeout")

    def test_server_failure_during_repair(self):
        app, upstream, _ = self.make_app(
            plugin_config={
                "mode": "direction_gate",
                "fail_direction": "client_bound",
                "fail_attempts": 99,
                "violations": [VIOLATION],
            },
            repair={"max_attempts": 2},
            responses=[
                json_response(chat_body("bad")),
                UpstreamTimeout("slow"),
            ],
        )
        response = self.post(app)
        self.assertEqual(response.status, 504)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["code"], "server_timeout")
        self.assertEqual(
            body["consideration"]["repair"],
            {"attempts": 1, "max_attempts": 2, "exhausted": False},
        )
        self.assertEqual(response.headers["x-consideration-repairs"], "1")
        self.assertEqual(len(upstream.calls), 2)

    def test_non_2xx_is_verbatim(self):
        raw = b'{"error":{"message":"bad key","type":"invalid_request_error","code":"invalid_api_key"}}'
        app, _, _ = self.make_app(
            client_bound=False,
            responses=[raw_response(401, raw)],
        )
        response = self.post(app)
        self.assertEqual(response.status, 401)
        self.assertEqual(response.body, raw)
        self.assertEqual(response.headers["x-consideration-verdict"], "error")
        self.assertNotIn("x-consideration-code", response.headers)

    def test_invalid_server_body(self):
        app, _, _ = self.make_app(
            client_bound=False,
            responses=[raw_response(200, b'{"choices": []}')],
        )
        response = self.post(app)
        self.assertEqual(response.status, 502)
        self.assertEqual(
            json.loads(response.body)["error"]["code"], "server_invalid_response"
        )

    # -- streaming -----------------------------------------------------

    def test_streaming_is_synthesized(self):
        body = dict(REQUEST, stream=True, stream_options={"include_usage": True})
        app, upstream, _ = self.make_app(
            plugin_config={"mode": "continue"},
            responses=[json_response(chat_body("Hi."))],
        )
        response = self.post(app, body)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["content-type"], "text/event-stream")
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        events = sse_events(response.body)
        self.assertEqual(len(events), 5)
        self.assertEqual(
            events[0]["choices"][0]["delta"],
            {"role": "assistant", "content": ""},
        )
        self.assertEqual(events[1]["choices"][0]["delta"]["content"], "Hi.")
        self.assertEqual(events[2]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(events[3]["choices"], [])
        self.assertEqual(events[3]["usage"]["total_tokens"], 14)
        self.assertEqual(events[4], "[DONE]")

        sent = json.loads(upstream.calls[0]["body"].decode("utf-8"))
        self.assertFalse(sent["stream"])
        self.assertNotIn("stream_options", sent)

    def test_streaming_is_relayed_without_client_bound(self):
        stream_body = b'data: {"id":"x"}\n\ndata: [DONE]\n\n'
        app, _, _ = self.make_app(
            plugin_config={"mode": "continue"},
            client_bound=False,
            responses=[
                raw_response(
                    200,
                    stream_body,
                    headers=[("content-type", "text/event-stream")],
                )
            ],
        )
        response = self.post(app, dict(REQUEST, stream=True))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, stream_body)
        self.assertEqual(response.headers["content-type"], "text/event-stream")

    # -- failures ------------------------------------------------------

    def test_client_bound_plugin_failure_is_not_repaired(self):
        app, upstream, _ = self.make_app(
            plugin_config={"mode": "crash"},
            server_bound=False,
            client_bound=True,
            repair={"max_attempts": 2},
            responses=[json_response()],
        )
        response = self.post(app)
        self.assertEqual(response.status, 500)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["code"], "plugin_crash")
        self.assertNotIn("violations", body["consideration"])
        self.assertEqual(len(upstream.calls), 1)

    def test_client_bound_fail_open_continues(self):
        app, _, _ = self.make_app(
            plugin_config={"mode": "crash"},
            server_bound=False,
            client_bound=True,
            on_error="fail_open",
            responses=[json_response(chat_body("kept"))],
        )
        response = self.post(app)
        self.assertEqual(response.status, 200)
        self.assertEqual(
            json.loads(response.body)["choices"][0]["message"]["content"], "kept"
        )

    # -- journal -------------------------------------------------------

    def _read_journal(self) -> dict:
        files = sorted((self.root / "journal").glob("journal-*.jsonl"))
        self.assertTrue(files)
        lines = files[-1].read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def test_journal_record(self):
        app, upstream, config = self.make_app(
            plugin_config={
                "mode": "direction_gate",
                "fail_direction": "client_bound",
                "fail_attempts": 1,
                "violations": [VIOLATION],
            },
            repair={"max_attempts": 2},
            responses=[
                json_response(chat_body("bad")),
                json_response(chat_body("good")),
            ],
        )
        response = self.post(app)
        self.assertEqual(response.status, 200)
        record = self._read_journal()
        self.assert_subset(
            record,
            {
                "schema": 1,
                "request_id": ANY,
                "config_hash": config.config_hash,
                "started_at": ANY,
                "client_streamed": False,
                "direction_results": {
                    "server_bound": {
                        "stages": [{"stage_id": "in", "action": "continue"}],
                        "terminal": None,
                    },
                    "client_bound": {
                        "attempts": [
                            {
                                "attempt": 0,
                                "terminal": {
                                    "kind": "violation",
                                    "stage_id": "out",
                                    "codes": [VIOLATION["code"]],
                                },
                            },
                            {"attempt": 1, "terminal": None},
                        ],
                        "terminal": None,
                    },
                },
                "server": {
                    "requests": [
                        {"attempt": 0, "status": 200, "usage": {"total_tokens": 14}},
                        {"attempt": 1, "status": 200, "usage": {"total_tokens": 14}},
                    ]
                },
                "repairs": [
                    {
                        "attempt": 1,
                        "trigger": {
                            "stage_id": "out",
                            "codes": [VIOLATION["code"]],
                        },
                        "server_bound_rerun": False,
                        "outcome": "accepted",
                    }
                ],
                "state_writes": [],
            },
            path="record",
        )
        self.assertGreaterEqual(record["duration_ms"], 0)
        self.assertEqual(record["response_bytes"], len(response.body))

    def test_journal_bodies_when_enabled(self):
        app, _, _ = self.make_app(
            plugin_config={"mode": "continue"},
            responses=[json_response()],
            extra="journal:\n  include_bodies: true\n",
        )
        self.post(app)
        record = self._read_journal()
        self.assertFalse(record["bodies"]["request"]["truncated"])
        self.assertFalse(record["bodies"]["response"]["truncated"])
        self.assertEqual(record["bodies"]["request"]["body"]["model"], "gpt-x")

    def test_journal_replay_matches_record(self):
        app, _, config = self.make_app(
            plugin_config={"mode": "continue"},
            responses=[json_response(chat_body("replayed"))],
            extra="journal:\n  include_bodies: true\n",
        )
        self.post(app)
        journal_path = sorted((self.root / "journal").glob("journal-*.jsonl"))[0]
        original, replayed = replay(config.source_path, journal_path)
        for value in (original, replayed):
            for key in ("request_id", "started_at", "duration_ms", "response_bytes"):
                value.pop(key, None)
        self.assertEqual(original, replayed)

    def test_journal_replay_reproduces_repairs(self):
        app, _, config = self.make_app(
            plugin_config={
                "mode": "direction_gate",
                "fail_direction": "client_bound",
                "fail_attempts": 1,
                "violations": [VIOLATION],
            },
            repair={"max_attempts": 2},
            responses=[
                json_response(chat_body("bad")),
                json_response(chat_body("good")),
            ],
            extra="journal:\n  include_bodies: true\n",
        )
        self.post(app)
        journal_path = sorted((self.root / "journal").glob("journal-*.jsonl"))[0]
        original, replayed = replay(config.source_path, journal_path)
        for value in (original, replayed):
            for key in ("request_id", "started_at", "duration_ms", "response_bytes"):
                value.pop(key, None)
        self.assertEqual(original, replayed)


if __name__ == "__main__":
    unittest.main()
