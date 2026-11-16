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

    def test_ipv6_listener_address_is_parsed(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(
            config_text().replace("127.0.0.1:0", '"[::1]:4000"'),
            encoding="utf-8",
        )
        config = load_config(path)
        self.assertEqual(config.listen_host, "::1")
        self.assertEqual(config.listen_port, 4000)

    def test_invalid_listener_addresses_are_rejected(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        for address in (
            "[::1]4000",
            "[]:4000",
            "[not-ipv6]:4000",
            "::1:4000",
            "127.0.0.1:nope",
        ):
            with self.subTest(address=address):
                path.write_text(
                    config_text().replace("127.0.0.1:0", json.dumps(address)),
                    encoding="utf-8",
                )
                with self.assertRaises(ConfigError):
                    load_config(path)

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

    def test_artifact_pin_matches_launched_script_digest(self):
        script = self.root / "plugins" / "echo" / "entry.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('artifact')\n", encoding="utf-8")
        write_plugin(self.root, command=[os.sys.executable, str(script)])
        config_path = self.root / "config.yaml"
        config_path.write_text(config_text(), encoding="utf-8")
        expected = load_config(config_path).plugins.entries["acme/echo"].artifact_digest
        config_path.write_text(
            config_text().replace(
                "      required: true\n",
                f"      required: true\n      artifact: {expected}\n",
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            load_config(config_path).plugins.entries["acme/echo"].artifact_digest,
            expected,
        )

    def test_opaque_module_command_requires_external_artifact_pin(self):
        command = [os.sys.executable, "-m", "example_plugin"]
        write_plugin(self.root, command=command)
        config_path = self.root / "config.yaml"
        config_path.write_text(config_text(), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "external artifact digest"):
            load_config(config_path)
        pin = "sha256:" + "a" * 64
        config_path.write_text(
            config_text().replace(
                "      required: true\n",
                f"      required: true\n      artifact: {pin}\n",
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            load_config(config_path).plugins.entries["acme/echo"].artifact_digest,
            pin,
        )

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

    def test_removed_passthrough_allowlist_setting_is_rejected(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(
            config_text().replace(
                "  timeout_ms: 5000\n",
                "  timeout_ms: 5000\n  passthrough_paths:\n    - /v1/chat/completions\n",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "unknown key.*passthrough_paths"):
            load_config(path)

    def test_removed_unused_settings_are_rejected(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        cases = (
            (
                config_text().replace(
                    "plugins:\n  entries:",
                    "plugins:\n  directory: ./plugins\n  entries:",
                ),
                "directory",
            ),
            (config_text(extra="journal:\n  state_values: hash\n"), "state_values"),
        )
        for text, key in cases:
            with self.subTest(key=key):
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, f"unknown key.*{key}"):
                    load_config(path)

    def test_journal_sensitivity_acknowledgement(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(
            config_text(extra="journal:\n  include_bodies: true\n"),
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

    def test_invalid_fallback_violation_span_is_rejected(self):
        write_plugin(self.root)
        violation = {
            "code": "test.invalid",
            "severity": "error",
            "message": "invalid",
            "retryable": False,
            "spans": [{"path": "/x", "start": 5, "end": 2}],
        }
        path = self.root / "config.yaml"
        path.write_text(
            config_text(
                on_error="fail_verdict",
                stage_extra={"fallback_violations": [violation]},
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "end must be >= start"):
            load_config(path)

    def test_critical_fallback_cannot_be_retryable(self):
        write_plugin(self.root)
        path = self.root / "config.yaml"
        path.write_text(
            config_text(
                on_error="fail_verdict",
                stage_extra={
                    "fallback_violations": [
                        {
                            "code": "test.critical",
                            "severity": "critical",
                            "message": "critical",
                            "retryable": True,
                        }
                    ]
                },
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "false for critical"):
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

    def test_malformed_manifest_fields_are_config_errors(self):
        for key, value in (
            ("version", []),
            ("contract", 2),
            ("hooks", {"server_bound": "request"}),
            ("read_only", "false"),
        ):
            with self.subTest(key=key):
                manifest_path = write_plugin(self.root)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest[key] = value
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                config_path = self.root / "config.yaml"
                config_path.write_text(config_text(), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(config_path)

    def test_plugin_source_changes_config_hash(self):
        script = self.root / "plugins" / "echo" / "entry.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('one')\n", encoding="utf-8")
        write_plugin(self.root, command=[os.sys.executable, str(script)])
        config_path = self.root / "config.yaml"
        config_path.write_text(config_text(), encoding="utf-8")
        first = load_config(config_path).config_hash
        script.write_text("print('two')\n", encoding="utf-8")
        second = load_config(config_path).config_hash
        self.assertNotEqual(first, second)

    def test_adjacent_imported_plugin_source_changes_config_hash(self):
        plugin_dir = self.root / "plugins" / "echo"
        script = plugin_dir / "entry.py"
        script.parent.mkdir(parents=True)
        script.write_text("from common import handler\n", encoding="utf-8")
        shared = plugin_dir.parent / "common.py"
        shared.write_text("def handler(): return 1\n", encoding="utf-8")
        write_plugin(self.root, command=[os.sys.executable, str(script)])
        config_path = self.root / "config.yaml"
        config_path.write_text(config_text(), encoding="utf-8")
        first = load_config(config_path).config_hash
        shared.write_text("def handler(): return 2\n", encoding="utf-8")
        second = load_config(config_path).config_hash
        self.assertNotEqual(first, second)

    def test_plugin_source_change_invalidates_replay_hash(self):
        from consideration.replay import replay

        script = self.root / "plugins" / "echo" / "entry.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('before')\n", encoding="utf-8")
        write_plugin(self.root, command=[os.sys.executable, str(script)])
        config_path = self.root / "config.yaml"
        config_path.write_text(config_text(), encoding="utf-8")
        config_hash = load_config(config_path).config_hash
        journal_path = self.root / "journal.jsonl"
        journal_path.write_text(
            json.dumps({"config_hash": config_hash}) + "\n", encoding="utf-8"
        )
        script.write_text("print('after')\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "config_hash"):
            replay(config_path, journal_path)


if __name__ == "__main__":
    unittest.main()
