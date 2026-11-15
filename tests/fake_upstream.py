"""An in-process stand-in for the upstream OpenAI-compatible server."""

from __future__ import annotations

from typing import Any, Callable

from consideration.upstream import UpstreamResponse
from consideration.util import dumps


def chat_body(content: str = "Hello.", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1763121600,
        "model": "gpt-x",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    }
    body.update(overrides)
    return body


def json_response(
    body: dict[str, Any] | None = None, status: int = 200
) -> UpstreamResponse:
    payload = dumps(body if body is not None else chat_body()).encode("utf-8")
    return UpstreamResponse(
        status, [("content-type", "application/json")], payload
    )


def raw_response(
    status: int, body: bytes, headers: list[tuple[str, str]] | None = None
) -> UpstreamResponse:
    return UpstreamResponse(
        status, headers or [("content-type", "application/json")], body
    )


class FakeUpstream:
    """Returns queued responses in order, repeating the last one."""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self._index = 0

    def request(
        self,
        method: str,
        path: str,
        client_headers: dict[str, str],
        body: bytes | None,
        timeout_ms: int,
    ) -> UpstreamResponse:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "headers": dict(client_headers),
                "body": body,
                "timeout_ms": timeout_ms,
            }
        )
        if not self.responses:
            raise AssertionError("FakeUpstream has no queued responses")
        if self._index < len(self.responses):
            item = self.responses[self._index]
            self._index += 1
        else:
            item = self.responses[-1]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        if callable(item):
            return item()
        return item

    def last_body(self) -> dict[str, Any]:
        import json

        assert self.calls
        return json.loads(self.calls[-1]["body"].decode("utf-8"))
