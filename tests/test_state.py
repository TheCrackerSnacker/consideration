from __future__ import annotations

import unittest

from consideration.errors import InvalidResult
from consideration.state import MAX_KEYS, StateStore, apply_writes, state_value


class StateTests(unittest.TestCase):
    def test_set_and_delete(self):
        scope, keys = apply_writes(
            {"a": 1}, {"set": {"b": 2}, "delete": ["a", "missing"]}
        )
        self.assertEqual(scope, {"b": 2})
        self.assertEqual(keys, ["b", "a", "missing"])

    def test_scope_is_not_mutated(self):
        original = {"a": {"b": 1}}
        scope, _ = apply_writes(original, {"set": {"c": 2}})
        self.assertEqual(original, {"a": {"b": 1}})
        self.assertEqual(scope["a"], {"b": 1})

    def test_unknown_write_key_rejected(self):
        with self.assertRaises(InvalidResult):
            apply_writes({}, {"namespace": "other"})

    def test_bad_delete_rejected(self):
        with self.assertRaises(InvalidResult):
            apply_writes({}, {"delete": "a"})

    def test_key_limit(self):
        with self.assertRaises(InvalidResult):
            apply_writes({}, {"set": {f"k{i}": i for i in range(MAX_KEYS + 1)}})

    def test_byte_limit(self):
        with self.assertRaises(InvalidResult):
            apply_writes({}, {"set": {"big": "x" * (64 * 1024)}})

    def test_store_snapshot_is_isolated(self):
        store = StateStore()
        store.commit({"ns": {"a": 1}})
        snapshot = store.snapshot()
        snapshot["ns"]["a"] = 2
        self.assertEqual(store.get("ns"), {"a": 1})

    def test_state_value_modes(self):
        self.assertEqual(state_value({"a": 1}, "plaintext"), {"a": 1})
        self.assertTrue(state_value({"a": 1}, "hash").startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
