from __future__ import annotations

import json
import unittest

from consideration.repair import (
    all_retryable,
    build_repair_request,
    render_instruction,
    resolve_pointer,
    span_excerpt,
)

SERVER_REQUEST = {
    "model": "gpt-x",
    "stream": True,
    "stream_options": {"include_usage": True},
    "messages": [{"role": "user", "content": "Explain recursion."}],
    "temperature": 0.2,
}

REJECTED = {
    "id": "chatcmpl-abc",
    "object": "chat.completion",
    "created": 1763121600,
    "model": "gpt-x",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Recursion is a function calling itself \u2014 with a base case.",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 14, "total_tokens": 26},
}

VIOLATION = {
    "plugin": "acme/style-lint",
    "plugin_version": "1.2.0",
    "stage_id": "guard-out",
    "code": "style.em_dash",
    "severity": "error",
    "message": "Output contains an em dash.",
    "hint": "Rewrite the sentence using a comma, colon, or period instead.",
    "retryable": True,
    "spans": [{"path": "/choices/0/message/content", "start": 39, "end": 42}],
    "details": {"count": 1},
}


class RepairTests(unittest.TestCase):
    def test_all_retryable(self):
        self.assertTrue(all_retryable([VIOLATION]))
        self.assertFalse(all_retryable([{**VIOLATION, "retryable": False}]))
        self.assertFalse(all_retryable([]))

    def test_pointer_resolution(self):
        self.assertEqual(
            resolve_pointer(REJECTED, "/choices/0/message/content"),
            REJECTED["choices"][0]["message"]["content"],
        )
        self.assertIsNone(resolve_pointer(REJECTED, "/choices/9/message/content"))
        self.assertIsNone(resolve_pointer(REJECTED, "/missing"))

    def test_span_excerpt_is_utf8_byte_slice(self):
        self.assertEqual(span_excerpt(REJECTED, VIOLATION["spans"][0]), "\u2014")

    def test_instruction_matches_appendix(self):
        instruction = render_instruction(REJECTED, [VIOLATION])
        self.assertEqual(
            instruction,
            "Your previous response was rejected by policy.\n"
            "\n"
            "Violations:\n"
            "- style.em_dash (error): Output contains an em dash.\n"
            '  At /choices/0/message/content [39,42): "\u2014"\n'
            "  Hint: Rewrite the sentence using a comma, colon, or period instead.\n"
            "\n"
            "Return a complete replacement assistant turn. Do not describe the rejection.",
        )
        for forbidden in ("plugin", "plugin_version", "stage_id"):
            self.assertNotIn(forbidden, instruction)

    def test_instruction_omits_absent_hint_and_span(self):
        violation = {k: v for k, v in VIOLATION.items() if k not in ("hint", "spans")}
        instruction = render_instruction(REJECTED, [violation])
        self.assertNotIn("Hint:", instruction)
        self.assertNotIn("At ", instruction)

    def test_repair_request_shape(self):
        request = build_repair_request(SERVER_REQUEST, REJECTED, [VIOLATION])
        self.assertFalse(request["stream"])
        self.assertNotIn("stream_options", request)
        self.assertEqual(request["temperature"], 0.2)
        self.assertEqual(request["messages"][0], SERVER_REQUEST["messages"][0])
        self.assertEqual(
            request["messages"][1],
            {
                "role": "assistant",
                "content": "Recursion is a function calling itself \u2014 with a base case.",
            },
        )
        self.assertEqual(request["messages"][2]["role"], "user")
        self.assertTrue(
            request["messages"][2]["content"].startswith(
                "Your previous response was rejected by policy."
            )
        )

    def test_tool_calls_are_preserved(self):
        rejected = {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call_1", "type": "function"}],
                    },
                }
            ]
        }
        request = build_repair_request(SERVER_REQUEST, rejected, [VIOLATION])
        assistant = request["messages"][1]
        self.assertIsNone(assistant["content"])
        self.assertEqual(
            assistant["tool_calls"], [{"id": "call_1", "type": "function"}]
        )

    def test_server_request_is_not_mutated(self):
        original = json.loads(json.dumps(SERVER_REQUEST))
        build_repair_request(SERVER_REQUEST, REJECTED, [VIOLATION])
        self.assertEqual(SERVER_REQUEST, original)


if __name__ == "__main__":
    unittest.main()
