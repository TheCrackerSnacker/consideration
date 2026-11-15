"""RFC 7386 JSON Merge Patch."""

from __future__ import annotations

from typing import Any

from .util import deep_copy


def merge_patch(target: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return deep_copy(patch)
    if not isinstance(target, dict):
        target = {}
    else:
        target = deep_copy(target)
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = merge_patch(target.get(key), value)
    return target
