from __future__ import annotations

import json
import unittest

from consideration.streaming import synthesize_sse

BODY = {
    "id": "chatcmpl-def",
    "object": "chat.completion",
    "created": 1763121601,
    "model": "gpt-x",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Recursion is a function that calls itself.",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 40, "completion_tokens": 18, "total_tokens": 58},
}


def parse(data: bytes) -> list:
    values = []
    for block in data.decode("utf-8").split("\n\n"):
        if not block:
            continue
        line = block[len("data: ") :]
        values.append(line if line == "[DONE]" else json.loads(line))
    return values


class StreamingTests(unittest.TestCase):
    def test_chunk_sequence(self):
        chunks = parse(synthesize_sse(BODY))
        self.assertEqual(len(chunks), 4)
        self.assertEqual(
            chunks[0]["choices"][0]["delta"], {"role": "assistant", "content": ""}
        )
        self.assertEqual(
            chunks[1]["choices"][0]["delta"]["content"],
            "Recursion is a function that calls itself.",
        )
        self.assertEqual(chunks[2]["choices"][0]["delta"], {})
        self.assertEqual(chunks[2]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(chunks[3], "[DONE]")
        for chunk in chunks[:3]:
            self.assertEqual(chunk["object"], "chat.completion.chunk")
            self.assertEqual(chunk["id"], "chatcmpl-def")
            self.assertEqual(chunk["created"], 1763121601)
            self.assertEqual(chunk["model"], "gpt-x")

    def test_usage_chunk(self):
        chunks = parse(synthesize_sse(BODY, include_usage=True))
        self.assertEqual(chunks[3]["choices"], [])
        self.assertEqual(chunks[3]["usage"], BODY["usage"])
        self.assertEqual(chunks[4], "[DONE]")

    def test_tool_calls_in_content_delta(self):
        body = {
            "id": "x",
            "created": 1,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call_1"}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        chunks = parse(synthesize_sse(body))
        self.assertEqual(
            chunks[1]["choices"][0]["delta"]["tool_calls"], [{"id": "call_1"}]
        )
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "")
        self.assertEqual(chunks[2]["choices"][0]["finish_reason"], "tool_calls")

    def test_content_parts_are_flattened(self):
        body = {
            "id": "x",
            "created": 1,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "a"},
                            {"type": "text", "text": "b"},
                        ],
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        chunks = parse(synthesize_sse(body))
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "ab")

    def test_multiple_choices_are_preserved(self):
        body = {
            **BODY,
            "choices": [
                {
                    "index": 2,
                    "message": {"role": "assistant", "content": "first"},
                    "finish_reason": "stop",
                },
                {
                    "index": 4,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "call_2"}],
                    },
                    "finish_reason": "tool_calls",
                },
            ],
        }
        chunks = parse(synthesize_sse(body))
        self.assertEqual([choice["index"] for choice in chunks[0]["choices"]], [2, 4])
        self.assertEqual(
            [choice["delta"]["content"] for choice in chunks[1]["choices"]],
            ["first", ""],
        )
        self.assertEqual(
            chunks[1]["choices"][1]["delta"]["tool_calls"], [{"id": "call_2"}]
        )
        self.assertEqual(
            [choice["finish_reason"] for choice in chunks[2]["choices"]],
            ["stop", "tool_calls"],
        )

    def test_delta_response_content_and_function_call_are_synthesized(self):
        body = {
            "id": "delta-id",
            "created": 10,
            "model": "gpt-x",
            "choices": [
                {
                    "index": 3,
                    "delta": {
                        "content": [
                            {"type": "text", "text": "part "},
                            {"type": "text", "text": "one"},
                        ],
                        "function_call": {"name": "lookup", "arguments": "{}"},
                    },
                    "finish_reason": "function_call",
                }
            ],
            "usage": {"total_tokens": 2},
        }
        chunks = parse(synthesize_sse(body, include_usage=True))
        self.assertEqual(chunks[1]["choices"][0]["index"], 3)
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "part one")
        self.assertEqual(
            chunks[1]["choices"][0]["delta"]["function_call"],
            {"name": "lookup", "arguments": "{}"},
        )
        self.assertEqual(chunks[2]["choices"][0]["finish_reason"], "function_call")
        self.assertEqual(chunks[3]["usage"], {"total_tokens": 2})


if __name__ == "__main__":
    unittest.main()
