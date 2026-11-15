from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from consideration.config import ConfigError, load_config
from tests.helpers import config_text, make_config, write_plugin


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_and_hash(self):
        config = make_config(self.root)
        self.assertEqual(config.pipeline.repair.max_attempts, 2)
        self.assertEqual(config.pipeline.repair.on_exhaustion, "violation")
        self.assertEqual(config.server.timeout_ms, 5000)
        self.assertEqual(config.pipeline.server_bound[0].stage_id, "in")
        self.assertTrue(config.config_hash.startswith("sha256:"))
        self.assertEqual(config.journal.include_bodies, False)

    def test_unknown_key_rejected(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(config_text() + "surprise: true\n", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_unknown_stage_key_rejected(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(
            config_text().replace(
                "      hook: request\n", "      hook: request\n      nope: 1\n"
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_credential_header_rejected(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(
            config_text().replace(
                "  timeout_ms: 5000\n",
                "  timeout_ms: 5000\n  headers:\n    Authorization: Bearer x\n",
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_env_reference_resolved(self):
        write_plugin(self.root)
        os.environ["CONSIDERATION_TEST_BASE"] = "http://example.test/v1"
        self.addCleanup(os.environ.pop, "CONSIDERATION_TEST_BASE", None)
        path = self.root / "config.yaml"
        path.write_text(
            config_text().replace(
                "http://127.0.0.1:9/v1", "env:CONSIDERATION_TEST_BASE"
            ),
            encoding="utf-8",
        )
        config = load_config(path)
        self.assertEqual(config.server.base_url, "http://example.test/v1")

    def test_missing_env_reference_rejected(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(
            config_text().replace(
                "http://127.0.0.1:9/v1", "env:CONSIDERATION_TEST_MISSING"
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_unknown_plugin_rejected(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(
            config_text().replace("plugin: acme/echo", "plugin: acme/missing", 1),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_duplicate_stage_id_rejected(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        text = config_text().replace("stage_id: out", "stage_id: in")
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_duplicate_plugin_in_one_pipeline_rejected(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        text = config_text().replace(
            "  client_bound:\n    - stage_id: out\n      plugin: acme/echo\n      hook: response\n      timeout_ms: 500\n      on_error: fail_closed\n",
            "  client_bound:\n    - stage_id: out\n      plugin: acme/echo\n      hook: response\n      timeout_ms: 500\n      on_error: fail_closed\n    - stage_id: out2\n      plugin: acme/echo\n      hook: response\n      timeout_ms: 500\n      on_error: fail_closed\n",
        )
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_paired_plugin_requires_both_sides(self):
        write_plugin(self.root, paired=True)
        path = self.root / "config.yaml"
        path.write_text(config_text(client_bound=False), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_artifact_mismatch_rejected(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(
            config_text().replace(
                "      required: true\n",
                "      required: true\n      artifact: sha256:deadbeef\n",
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_repair_validation(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(config_text(repair={"max_attempts": -1}), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)
        path.write_text(
            config_text(repair={"on_exhaustion": "explode"}), encoding="utf-8"
        )
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_passthrough_overlap_rejected(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(
            config_text().replace(
                "  timeout_ms: 5000\n",
                "  timeout_ms: 5000\n  passthrough_paths:\n    - /v1/chat/completions\n",
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_journal_sensitivity_acknowledgement(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(
            config_text(
                extra="journal:\n  include_bodies: true\n  state_values: plaintext\n"
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_fail_verdict_requires_violations(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(config_text(on_error="fail_verdict"), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_manifest_id_mismatch_rejected(self):
        path = write_plugin(self.root)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["id"] = "acme/other"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        config_path = self.root / "config.yaml"
        config_path.write_text(config_text(), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(config_path)


if __name__ == "__main__":
    unittest.main()
