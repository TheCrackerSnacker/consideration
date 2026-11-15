"""Assertion helpers for JSON-shaped values."""

from __future__ import annotations

from typing import Any


class SubsetAssertions:
    """Assert only the keys a test cares about, with path-aware failures.

    Dictionaries are matched recursively by the keys present in ``expected``;
    lists must match in length and order. Volatile values can be matched with
    ``unittest.mock.ANY``.
    """

    def assert_subset(self, actual: Any, expected: Any, path: str = "value") -> None:
        if isinstance(expected, dict):
            self.assertIsInstance(actual, dict, f"{path} must be an object")
            for key, value in expected.items():
                self.assertIn(key, actual, f"{path}.{key} is missing")
                self.assert_subset(actual[key], value, f"{path}.{key}")
        elif isinstance(expected, list):
            self.assertIsInstance(actual, list, f"{path} must be an array")
            self.assertEqual(len(actual), len(expected), f"{path} length")
            for index, value in enumerate(expected):
                self.assert_subset(actual[index], value, f"{path}[{index}]")
        else:
            self.assertEqual(actual, expected, path)
