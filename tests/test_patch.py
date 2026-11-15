from __future__ import annotations

import unittest

from consideration.patch import merge_patch


class MergePatchTests(unittest.TestCase):
    def test_rfc7386_object_replacement(self):
        self.assertEqual(merge_patch({"a": "b"}, {"a": "c"}), {"a": "c"})

    def test_rfc7386_remove(self):
        self.assertEqual(merge_patch({"a": "b", "c": "d"}, {"a": None}), {"c": "d"})

    def test_rfc7386_nested(self):
        target = {"a": {"b": "c", "d": "e", "f": "g"}}
        patch = {"a": {"b": "z", "d": None}}
        self.assertEqual(merge_patch(target, patch), {"a": {"b": "z", "f": "g"}})

    def test_rfc7386_array_replaced_whole(self):
        self.assertEqual(merge_patch({"a": [1, 2]}, {"a": [3]}), {"a": [3]})

    def test_non_object_patch_replaces(self):
        self.assertEqual(merge_patch({"a": 1}, [1, 2]), [1, 2])
        self.assertEqual(merge_patch({"a": 1}, None), None)

    def test_does_not_mutate_target(self):
        target = {"a": {"b": 1}}
        result = merge_patch(target, {"a": {"c": 2}})
        self.assertEqual(target, {"a": {"b": 1}})
        self.assertEqual(result, {"a": {"b": 1, "c": 2}})

    def test_null_inside_new_object(self):
        self.assertEqual(merge_patch({}, {"a": None}), {})


if __name__ == "__main__":
    unittest.main()
