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
            "message": {"role": "assistant", "content": "Recursion is a function that calls itself."},
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
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant", "content": ""})
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
        self.assertEqual(chunks[1]["choices"][0]["delta"]["tool_calls"], [{"id": "call_1"}])
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
                        "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        chunks = parse(synthesize_sse(body))
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "ab")


if __name__ == "__main__":
    unittest.main()
