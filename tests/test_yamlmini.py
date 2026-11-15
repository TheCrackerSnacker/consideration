from __future__ import annotations

import unittest

from consideration.yamlmini import YAMLError, loads


class YamlMiniTests(unittest.TestCase):
    def test_nested_mapping(self):
        value = loads("a:\n  b: 1\n  c: two\n")
        self.assertEqual(value, {"a": {"b": 1, "c": "two"}})

    def test_sequence_of_mappings(self):
        text = """
pipeline:
  server_bound:
    - stage_id: guard-in
      plugin: acme/prompt-guard
      hook: request
      timeout_ms: 250
      on_error: fail_closed
  client_bound: []
"""
        value = loads(text)
        stage = value["pipeline"]["server_bound"][0]
        self.assertEqual(stage["stage_id"], "guard-in")
        self.assertEqual(stage["timeout_ms"], 250)
        self.assertEqual(value["pipeline"]["client_bound"], [])

    def test_flow_sequence_and_mapping(self):
        value = loads('config: {"banned": ["\\u2014", "x"], "ok": true, "n": null}\n')
        self.assertEqual(value["config"], {"banned": ["\u2014", "x"], "ok": True, "n": None})

    def test_comments_outside_quotes(self):
        value = loads('a: "x # y"  # trailing\nb: z#not-a-comment\n')
        self.assertEqual(value, {"a": "x # y", "b": "z#not-a-comment"})

    def test_scalars(self):
        value = loads(
            "i: -3\nf: 1.5\nb1: true\nb2: false\nn1: null\nn2: ~\n"
            "s: 127.0.0.1:4000\nq: 'it''s'\n"
        )
        self.assertEqual(value["i"], -3)
        self.assertEqual(value["f"], 1.5)
        self.assertIs(value["b1"], True)
        self.assertIs(value["b2"], False)
        self.assertIsNone(value["n1"])
        self.assertIsNone(value["n2"])
        self.assertEqual(value["s"], "127.0.0.1:4000")
        self.assertEqual(value["q"], "it's")

    def test_nested_sequence_of_scalars(self):
        value = loads("a:\n  - x\n  - y\nb: z\n")
        self.assertEqual(value, {"a": ["x", "y"], "b": "z"})

    def test_empty_document(self):
        self.assertIsNone(loads("\n# only a comment\n"))

    def test_bad_indentation(self):
        with self.assertRaises(YAMLError):
            loads("a:\n    b: 1\n  c: 2\n")

    def test_unterminated_flow(self):
        with self.assertRaises(YAMLError):
            loads("a: [1, 2\n")

    def test_tab_indentation_rejected(self):
        with self.assertRaises(YAMLError):
            loads("a:\n\tb: 1\n")


if __name__ == "__main__":
    unittest.main()
