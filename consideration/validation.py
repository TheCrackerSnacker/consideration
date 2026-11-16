"""Shared schema validation for host-configured and plugin-returned violations."""

from __future__ import annotations

from typing import Any

from .util import dumps

VIOLATION_KEYS = {
    "code",
    "severity",
    "message",
    "hint",
    "retryable",
    "spans",
    "details",
}
SEVERITIES = {"info", "warning", "error", "critical"}


def validate_violation(
    value: Any, where: str, error_type: type[Exception]
) -> dict[str, Any]:
    def fail(field: str, message: str) -> None:
        label = f"{where}.{field}" if field else where
        raise error_type(f"{label} {message}")

    if not isinstance(value, dict):
        fail("", "must be an object")
    unknown = set(value) - VIOLATION_KEYS
    if unknown:
        fail("", f"has unknown key(s): {', '.join(sorted(unknown))}")
    for key in ("code", "severity", "message", "retryable"):
        if key not in value:
            fail(key, "is required")
    if not isinstance(value["code"], str) or not value["code"]:
        fail("code", "must be a non-empty string")
    if not isinstance(value["severity"], str) or value["severity"] not in SEVERITIES:
        fail("severity", f"must be one of {sorted(SEVERITIES)}")
    if not isinstance(value["message"], str):
        fail("message", "must be a string")
    if not isinstance(value["retryable"], bool):
        fail("retryable", "must be a boolean")
    if value["severity"] == "critical" and value["retryable"]:
        fail("retryable", "must be false for critical violations")
    if "hint" in value and not isinstance(value["hint"], str):
        fail("hint", "must be a string")
    spans = value.get("spans")
    if spans is not None:
        if not isinstance(spans, list):
            fail("spans", "must be a list")
        for index, span in enumerate(spans):
            prefix = f"spans[{index}]"
            if not isinstance(span, dict):
                fail(prefix, "must be an object")
            if not isinstance(span.get("path"), str):
                fail(f"{prefix}.path", "must be a string")
            for bound in ("start", "end"):
                offset = span.get(bound)
                if (
                    isinstance(offset, bool)
                    or not isinstance(offset, int)
                    or offset < 0
                ):
                    fail(f"{prefix}.{bound}", "must be a non-negative integer")
            if span["end"] < span["start"]:
                fail(prefix, "end must be >= start")
            extra = set(span) - {"path", "start", "end"}
            if extra:
                fail(prefix, f"has unknown key(s): {', '.join(sorted(extra))}")
    details = value.get("details")
    if details is not None:
        if not isinstance(details, dict):
            fail("details", "must be an object")
        try:
            dumps(details)
        except (TypeError, ValueError) as exc:
            raise error_type(f"{where}.details must be JSON-serializable") from exc
    return value
