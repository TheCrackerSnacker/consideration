from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from consideration.pipeline import PipelineRunner
from consideration.plugin import PluginManager
from consideration.state import StateStore
from tests.helpers import make_config

REQUEST = {
    "kind": "chat.request",
    "body": {"model": "gpt-x", "messages": [{"role": "user", "content": "hi"}]},
    "meta": {"request_id": "req_test", "direction": "server_bound"},
}

RESPONSE = {
    "kind": "chat.response",
    "body": {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-x",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
    },
    "meta": {"request_id": "req_test", "direction": "client_bound"},
}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.managers: list[PluginManager] = []

    def tearDown(self):
        for manager in self.managers:
            manager.shutdown_all()
        self.tmp.cleanup()

    def runner(self, **kwargs):
        config = make_config(self.root, **kwargs)
        manager = PluginManager(config, telemetry=None)
        manager.start_all()
        self.managers.append(manager)
        return PipelineRunner(config, manager, telemetry=None)

    def pipeline_run(
        self,
        runner,
        direction="server_bound",
        attempt=0,
        store=None,
        server_request=None,
    ):
        envelope = REQUEST if direction == "server_bound" else RESPONSE
        return runner.run(
            direction,
            envelope,
            "req_test",
            attempt,
            store or StateStore(),
            server_request=server_request,
        )

    def test_continue(self):
        result = self.pipeline_run(self.runner())
        self.assertIsNone(result.terminal)
        self.assertFalse(result.changed)
        self.assertEqual(result.envelope["body"], REQUEST["body"])
        self.assertEqual(result.stages[0].action, "continue")

    def test_patch(self):
        runner = self.runner(
            plugin_config={
                "mode": "patch",
                "patch": {"messages": [{"role": "user", "content": "x"}]},
            }
        )
        result = self.pipeline_run(runner)
        self.assertIsNone(result.terminal)
        self.assertTrue(result.changed)
        self.assertEqual(result.envelope["body"]["messages"][0]["content"], "x")

    def test_replace(self):
        body = {"model": "gpt-y", "messages": [{"role": "user", "content": "y"}]}
        runner = self.runner(plugin_config={"mode": "replace", "body": body})
        result = self.pipeline_run(runner)
        self.assertEqual(result.envelope["body"], body)

    def test_terminate_renders_policy_terminal(self):
        violation = {
            "code": "style.em_dash",
            "severity": "error",
            "message": "Output contains an em dash.",
            "retryable": True,
        }
        runner = self.runner(
            plugin_config={"mode": "terminate", "violations": [violation]}
        )
        result = self.pipeline_run(runner, "client_bound")
        self.assertIsNotNone(result.terminal)
        assert result.terminal is not None
        self.assertEqual(result.terminal.status, 400)
        self.assertEqual(result.terminal.direction, "client_bound")
        rendered = result.terminal.violations[0]
        self.assertEqual(rendered["plugin"], "acme/echo")
        self.assertEqual(rendered["stage_id"], "out")
        self.assertEqual(rendered["code"], "style.em_dash")
        self.assertIs(result.changed, False)

    def test_invalid_action_fail_closed(self):
        result = self.pipeline_run(self.runner(plugin_config={"mode": "invalid"}))
        self.assertIsNotNone(result.terminal)
        assert result.terminal is not None
        self.assertEqual(result.terminal.status, 500)
        self.assertEqual(result.terminal.error["code"], "plugin_invalid_output")
        self.assertEqual(result.terminal.kind, "host_error")

    def test_invalid_replacement_structure(self):
        result = self.pipeline_run(
            self.runner(
                plugin_config={"mode": "replace", "body": {"model": "", "messages": []}}
            )
        )
        self.assertEqual(result.terminal.error["code"], "plugin_invalid_output")

    def test_fail_open_continues_with_pre_stage_envelope(self):
        result = self.pipeline_run(
            self.runner(plugin_config={"mode": "crash"}, on_error="fail_open")
        )
        self.assertIsNone(result.terminal)
        self.assertEqual(result.envelope["body"], REQUEST["body"])
        self.assertEqual(result.stages[0].failure, "crash")

    def test_fail_verdict_uses_fallback_violations(self):
        violation = {
            "code": "guard.unavailable",
            "severity": "critical",
            "message": "Guard unavailable.",
            "retryable": False,
        }
        config = make_config(
            self.root,
            plugin_config={"mode": "crash"},
            on_error="fail_verdict",
            stage_extra={"fallback_violations": [violation]},
        )
        manager = PluginManager(config, telemetry=None)
        manager.start_all()
        self.managers.append(manager)
        runner = PipelineRunner(config, manager, telemetry=None)
        result = self.pipeline_run(runner)
        self.assertIsNotNone(result.terminal)
        self.assertEqual(result.terminal.kind, "host_error")
        self.assertEqual(result.terminal.violations[0]["code"], "guard.unavailable")

    def test_state_is_visible_across_pipelines(self):
        runner = self.runner(
            plugin_config={
                "mode": "state_patch",
                "state_set": {"map": {"[[P1]]": "John"}},
            }
        )
        store = StateStore()
        server = self.pipeline_run(runner, "server_bound", store=store)
        self.assertIsNone(server.terminal)
        client = self.pipeline_run(
            runner,
            "client_bound",
            store=store,
            server_request=REQUEST["body"],
        )
        self.assertIsNone(client.terminal)
        self.assertEqual(
            client.envelope["body"]["x_state"], {"map": {"[[P1]]": "John"}}
        )

    def test_rejected_attempt_discards_writes(self):
        violation = {
            "code": "style.em_dash",
            "severity": "error",
            "message": "no",
            "retryable": True,
        }
        runner = self.runner(
            plugin_config={
                "mode": "terminate",
                "violations": [violation],
                "state_set": {"leak": True},
            }
        )
        store = StateStore()
        result = self.pipeline_run(runner, "client_bound", store=store)
        self.assertIsNotNone(result.terminal)
        self.assertEqual(store.get("acme/echo"), {})
        self.assertEqual(result.state_writes[0].committed, False)

    def test_context_is_original_server_request(self):
        runner = self.runner(plugin_config={"mode": "context_patch"})
        result = self.pipeline_run(
            runner,
            "client_bound",
            server_request=REQUEST["body"],
        )
        self.assertEqual(
            result.envelope["body"]["x_context"],
            {"server_request": {"kind": "chat.request", "body": REQUEST["body"]}},
        )

    def test_state_limits_surface_as_invalid_output(self):
        runner = self.runner(
            plugin_config={
                "mode": "continue",
                "state_set": {f"k{i}": i for i in range(300)},
            }
        )
        result = self.pipeline_run(runner)
        self.assertEqual(result.terminal.error["code"], "plugin_invalid_output")

    def test_no_stages_is_a_noop(self):
        runner = self.runner(server_bound=False, client_bound=False)
        result = self.pipeline_run(runner)
        self.assertIsNone(result.terminal)
        self.assertEqual(result.envelope["body"], REQUEST["body"])


if __name__ == "__main__":
    unittest.main()
