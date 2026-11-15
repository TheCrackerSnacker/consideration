from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from consideration.errors import PluginCrash, PluginProtocolError, PluginTimeout
from consideration.plugin import PluginManager
from tests.helpers import make_config


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
