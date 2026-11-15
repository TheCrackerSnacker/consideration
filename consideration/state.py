"""Request-scoped private state with per-run staging."""

from __future__ import annotations

from typing import Any

from .errors import InvalidResult
from .util import canonical_json, deep_copy, json_hash

MAX_KEYS = 256
MAX_BYTES = 64 * 1024


class StateStore:
    """One in-memory store per request, keyed by plugin namespace."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            namespace: deep_copy(values) for namespace, values in self._data.items()
        }

    def commit(self, staged: dict[str, dict[str, Any]]) -> None:
        self._data = {
            namespace: deep_copy(values) for namespace, values in staged.items()
        }

    def get(self, namespace: str) -> dict[str, Any]:
        return deep_copy(self._data.get(namespace, {}))


def apply_writes(
    scope: dict[str, Any], writes: Any
) -> tuple[dict[str, Any], list[str]]:
    """Validate and apply a hook result's state block to one plugin namespace."""
    if writes is None:
        return deep_copy(scope), []
    if not isinstance(writes, dict):
        raise InvalidResult("state must be an object")
    unknown = set(writes) - {"set", "delete"}
    if unknown:
        raise InvalidResult(f"state has unknown key(s): {', '.join(sorted(unknown))}")
    result = deep_copy(scope)
    changed: list[str] = []
    set_values = writes.get("set", {})
    delete_values = writes.get("delete", [])
    if set_values is None:
        set_values = {}
    if not isinstance(set_values, dict):
        raise InvalidResult("state.set must be an object")
    if delete_values is None:
        delete_values = []
    if not isinstance(delete_values, list) or any(
        not isinstance(k, str) for k in delete_values
    ):
        raise InvalidResult("state.delete must be a list of strings")
    for key, value in set_values.items():
        if not isinstance(key, str) or not key:
            raise InvalidResult("state keys must be non-empty strings")
        result[key] = deep_copy(value)
        changed.append(key)
    for key in delete_values:
        result.pop(key, None)
        if key not in changed:
            changed.append(key)
    if len(result) > MAX_KEYS:
        raise InvalidResult(f"state exceeds {MAX_KEYS} keys")
    if len(canonical_json(result).encode("utf-8")) > MAX_BYTES:
        raise InvalidResult(f"state exceeds {MAX_BYTES} bytes")
    return result, changed


def state_value(value: Any, mode: str) -> Any:
    if mode == "plaintext":
        return deep_copy(value)
    return json_hash(value)
