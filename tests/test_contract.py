from __future__ import annotations

import json
import unittest
from pathlib import Path

from consideration import CONTRACT_VERSION, __version__


class ContractTests(unittest.TestCase):
    def test_v020_schema_is_strict_and_contains_no_header_properties(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "docs" / "plugin-hook.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["title"], "consideration v0.2.0 plugin hook parameters")
        self.assertEqual(schema["additionalProperties"], False)
        self.assertNotIn("headers", schema["$defs"]["envelopeMeta"]["properties"])
        self.assertFalse(any("headers" in value for value in schema["$defs"].values()))
        self.assertEqual(CONTRACT_VERSION, "0.2")
        self.assertEqual(__version__, "0.2.0")

    def test_schema_rejects_extra_hook_and_metadata_properties(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "docs" / "plugin-hook.schema.json").read_text(
                encoding="utf-8"
            )
        )
        params = {
            "stage_id": "guard-in",
            "hook": "request",
            "direction": "server_bound",
            "request_id": "req_test",
            "attempt": 0,
            "deadline_ms": 50,
            "envelope": {
                "kind": "chat.request",
                "body": {"model": "gpt-x", "messages": []},
                "meta": {
                    "request_id": "req_test",
                    "direction": "server_bound",
                    "received_at": "now",
                },
            },
            "state": {},
        }
        self.assertTrue(set(schema["required"]).issubset(params))
        self.assertFalse(set(params) - set(schema["properties"]))
        envelope_meta = schema["$defs"]["envelopeMeta"]
        self.assertFalse(
            set(params["envelope"]["meta"]) - set(envelope_meta["properties"])
        )
        params["envelope"]["meta"]["headers"] = {}
        self.assertTrue(
            set(params["envelope"]["meta"]) - set(envelope_meta["properties"])
        )


if __name__ == "__main__":
    unittest.main()
