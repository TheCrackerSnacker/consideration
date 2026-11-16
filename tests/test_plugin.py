from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from consideration.config import load_config
from consideration.errors import (
    PluginCrash,
    PluginProtocolError,
    PluginTimeout,
    PluginUnavailable,
)
from consideration.plugin import PluginManager, validate_runtime_description
from tests.helpers import config_text, make_config, write_plugin


class PluginLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.managers: list[PluginManager] = []

    def tearDown(self):
        for manager in self.managers:
            manager.shutdown_all()
        self.tmp.cleanup()

    def manager(self, **kwargs) -> PluginManager:
        config = make_config(self.root, **kwargs)
        manager = PluginManager(config, telemetry=None)
        manager.start_all()
        self.managers.append(manager)
        return manager

    def hook(self, manager, timeout_ms=500):
        return manager.get("acme/echo").call_hook(
            {
                "stage_id": "in",
                "hook": "request",
                "direction": "server_bound",
                "request_id": "req_test",
                "attempt": 0,
                "deadline_ms": timeout_ms,
                "envelope": {"kind": "chat.request", "body": {}, "meta": {}},
                "state": {},
            },
            timeout_ms,
        )

    def test_handshake_and_continue(self):
        manager = self.manager()
        instance = manager.get("acme/echo")
        self.assertTrue(instance.ready)
        self.assertEqual(self.hook(manager), {"action": "continue"})

    def test_reference_plugins_match_their_manifests_at_runtime(self):
        with patch.dict("os.environ", {"OPENAI_BASE_URL": "https://api.openai.com/v1"}):
            config = load_config(Path(__file__).parents[1] / "examples" / "config.yaml")
        manager = PluginManager(config, telemetry=None)
        manager.start_all()
        self.managers.append(manager)
        self.assertTrue(all(instance.ready for instance in manager.instances.values()))

    def test_runtime_description_must_match_install_manifest(self):
        manifest = {
            "id": "acme/echo",
            "version": "0.2.0",
            "contract": "0.2",
            "hooks": {"server_bound": ["request"], "client_bound": ["response"]},
            "read_only": False,
        }
        validate_runtime_description(manifest, manifest, "acme/echo")
        for field, value in (
            ("id", "acme/other"),
            ("version", "0.2.1"),
            ("contract", "0.1"),
            ("hooks", {"server_bound": ["response"]}),
            ("read_only", True),
        ):
            described = dict(manifest)
            described[field] = value
            with self.subTest(field=field), self.assertRaises(PluginProtocolError):
                validate_runtime_description(described, manifest, "acme/echo")

    def test_cancellation_write_failure_is_reported(self):
        manager = self.manager()
        instance = manager.get("acme/echo")

        class RecordingTelemetry:
            def __init__(self):
                self.events = []

            def emit(self, event, **fields):
                self.events.append((event, fields))

        telemetry = RecordingTelemetry()
        instance.telemetry = telemetry
        with patch("consideration.plugin.write_frame", side_effect=OSError("closed")):
            instance._cancel(99)
        diagnostics = [
            fields for event, fields in telemetry.events if event == "plugin.diagnostic"
        ]
        self.assertEqual(diagnostics[0]["operation"], "cancel")
        self.assertEqual(diagnostics[0]["error_type"], "OSError")

    def test_plugin_log_content_is_not_forwarded_to_telemetry(self):
        manager = self.manager()
        instance = manager.get("acme/echo")

        class RecordingTelemetry:
            def __init__(self):
                self.events = []

            def emit(self, event, **fields):
                self.events.append((event, fields))

        telemetry = RecordingTelemetry()
        instance.telemetry = telemetry
        instance._dispatch(
            instance._generation,
            {
                "jsonrpc": "2.0",
                "method": "plugin/log",
                "params": {"level": "info", "message": "private prompt text"},
            },
        )
        event, fields = telemetry.events[0]
        self.assertEqual(event, "plugin.log")
        self.assertEqual(fields["message_length"], len("private prompt text"))
        self.assertNotIn("message", fields)

    def test_shutdown_write_failure_is_reported(self):
        manager = self.manager()
        instance = manager.get("acme/echo")

        class RecordingTelemetry:
            def __init__(self):
                self.events = []

            def emit(self, event, **fields):
                self.events.append((event, fields))

        telemetry = RecordingTelemetry()
        instance.telemetry = telemetry
        with patch("consideration.plugin.write_frame", side_effect=OSError("closed")):
            instance.shutdown()
        operations = [
            fields["operation"]
            for event, fields in telemetry.events
            if event == "plugin.diagnostic"
        ]
        self.assertIn("shutdown_notification", operations)

    def test_modified_artifact_is_rejected_before_plugin_spawn(self):
        script = self.root / "plugins" / "echo" / "entry.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('original')\n", encoding="utf-8")
        write_plugin(self.root, command=[sys.executable, str(script)])
        config_path = self.root / "config.yaml"
        config_path.write_text(config_text(), encoding="utf-8")
        config = load_config(config_path)
        script.write_text("print('modified')\n", encoding="utf-8")
        manager = PluginManager(config, telemetry=None)
        with self.assertRaisesRegex(PluginUnavailable, "artifact changed"):
            manager.start_all()

    def test_timeout(self):
        manager = self.manager(
            plugin_config={"mode": "sleep", "delay_ms": 400}, timeout_ms=100
        )
        started = time.monotonic()
        with self.assertRaises(PluginTimeout):
            self.hook(manager, timeout_ms=100)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_crash(self):
        manager = self.manager(plugin_config={"mode": "crash"})
        with self.assertRaises(PluginCrash):
            self.hook(manager)

    def test_jsonrpc_error(self):
        manager = self.manager(plugin_config={"mode": "error"})
        with self.assertRaises(PluginProtocolError):
            self.hook(manager)

    def test_patch_result(self):
        manager = self.manager(
            plugin_config={"mode": "patch", "patch": {"messages": []}}
        )
        self.assertEqual(
            self.hook(manager), {"action": "patch", "patch": {"messages": []}}
        )

    def test_restart_after_crash_does_not_hang(self):
        manager = self.manager(plugin_config={"mode": "crash"})
        with self.assertRaises(PluginCrash):
            self.hook(manager)
        started = time.monotonic()
        with self.assertRaises((PluginCrash, PluginTimeout)):
            self.hook(manager, timeout_ms=500)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_shutdown_is_idempotent(self):
        manager = self.manager()
        manager.shutdown_all()
        manager.shutdown_all()


if __name__ == "__main__":
    unittest.main()
