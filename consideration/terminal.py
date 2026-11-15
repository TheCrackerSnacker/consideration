"""Host-owned terminal rendering (section 10.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .util import dumps


@dataclass
class Terminal:
    status: int
    error: dict[str, Any]
    consideration: dict[str, Any]
    verdict: str
    kind: str  # violation | host_error | server_error | passthrough
    host_code: str | None
    direction: str | None = None
    violations: list[dict[str, Any]] | None = None
    repair: dict[str, Any] | None = None
    raw_body: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def body_bytes(self) -> bytes:
        if self.raw_body is not None:
            return self.raw_body
        return dumps({"error": self.error, "consideration": self.consideration}).encode(
            "utf-8"
        )


def _consideration(
    request_id: str,
    direction: str | None = None,
    repair: dict[str, Any] | None = None,
    violations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"request_id": request_id}
    if direction is not None:
        value["direction"] = direction
    if repair is not None:
        value["repair"] = repair
    if violations is not None:
        value["violations"] = violations
    return value


def policy_terminal(
    request_id: str,
    direction: str,
    violations: list[dict[str, Any]],
    repair: dict[str, Any] | None = None,
    kind: str = "violation",
) -> Terminal:
    primary = violations[0]
    error = {
        "message": primary["message"],
        "type": "consideration_violation",
        "param": None,
        "code": primary["code"],
    }
    return Terminal(
        status=400,
        error=error,
        consideration=_consideration(request_id, direction, repair, violations),
        verdict="blocked",
        kind=kind,
        host_code=primary["code"],
        direction=direction,
        violations=violations,
        repair=repair,
    )


def exhaustion_terminal(
    request_id: str,
    violations: list[dict[str, Any]],
    repair: dict[str, Any],
    on_exhaustion: str,
) -> Terminal:
    if on_exhaustion == "violation":
        return policy_terminal(request_id, "client_bound", violations, repair)
    error = {
        "message": f"Client-bound validation failed after {repair['attempts']} repair attempts.",
        "type": "consideration_plugin_error",
        "param": None,
        "code": "repair_exhausted",
    }
    return Terminal(
        status=502,
        error=error,
        consideration=_consideration(request_id, "client_bound", repair, violations),
        verdict="error",
        kind="violation",
        host_code="repair_exhausted",
        direction="client_bound",
        violations=violations,
        repair=repair,
    )


def host_failure(
    request_id: str,
    code: str,
    message: str,
    direction: str | None = None,
    error_type: str = "consideration_plugin_error",
) -> Terminal:
    error = {"message": message, "type": error_type, "param": None, "code": code}
    return Terminal(
        status=400 if error_type == "consideration_request_error" else 500,
        error=error,
        consideration=_consideration(request_id, direction),
        verdict="error",
        kind="host_error",
        host_code=code,
        direction=direction,
    )


def request_error(request_id: str, message: str) -> Terminal:
    return host_failure(
        request_id, "invalid_request", message, error_type="consideration_request_error"
    )


def server_failure(
    request_id: str, code: str, message: str, repair: dict[str, Any] | None = None
) -> Terminal:
    error = {"message": message, "type": "server_error", "param": None, "code": code}
    return Terminal(
        status=504 if code == "server_timeout" else 502,
        error=error,
        consideration=_consideration(request_id, None, repair, None),
        verdict="error",
        kind="server_error",
        host_code=code,
        repair=repair,
    )


def passthrough_error(
    request_id: str,
    status: int,
    body: bytes,
    headers: dict[str, str],
) -> Terminal:
    return Terminal(
        status=status,
        error={},
        consideration=_consideration(request_id),
        verdict="error",
        kind="passthrough",
        host_code=None,
        raw_body=body,
        headers=headers,
    )


def plugin_failure(
    request_id: str, stage_id: str, code: str, kind: str, direction: str
) -> Terminal:
    return host_failure(
        request_id,
        code,
        f"Stage {stage_id} failed: {kind}",
        direction=direction,
    )
