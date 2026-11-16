"""Pipeline iteration, action validation, and attempt-scoped state."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config, StageConfig
from .envelope import validate_body
from .errors import InvalidResult, InvalidStageOutput, StageFailure
from .patch import merge_patch
from .plugin import PluginManager
from .state import StateStore, apply_writes
from .terminal import Terminal, plugin_failure, policy_terminal
from .util import deep_copy, json_hash
from .validation import VIOLATION_KEYS, validate_violation


@dataclass
class StageRecord:
    stage_id: str
    plugin: str
    plugin_version: str
    action: str
    duration_ms: int
    input_hash: str
    output_hash: str | None = None
    violations: list[dict[str, Any]] | None = None
    failure: str | None = None

    def journal(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "stage_id": self.stage_id,
            "plugin": self.plugin,
            "plugin_version": self.plugin_version,
            "action": self.action,
            "duration_ms": self.duration_ms,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
        }
        if self.violations is not None:
            value["violations"] = self.violations
        if self.failure is not None:
            value["failure"] = self.failure
        return value


@dataclass
class StateWriteRecord:
    stage_id: str
    plugin: str
    attempt: int
    keys: list[str]
    values: dict[str, Any] | None = None
    committed: bool = False

    def journal(self) -> dict[str, Any]:
        value = {
            "stage_id": self.stage_id,
            "plugin": self.plugin,
            "attempt": self.attempt,
            "keys": self.keys,
            "committed": self.committed,
        }
        if self.values is not None:
            value["values"] = self.values
        return value


@dataclass
class PipelineResult:
    envelope: dict[str, Any]
    terminal: Terminal | None
    stages: list[StageRecord] = field(default_factory=list)
    state_writes: list[StateWriteRecord] = field(default_factory=list)
    terminal_stage_id: str | None = None

    def journal(self) -> dict[str, Any]:
        terminal = None
        if self.terminal is not None:
            terminal = {
                "kind": self.terminal.kind,
                "stage_id": self.terminal_stage_id,
                "codes": [
                    violation.get("code")
                    for violation in (self.terminal.violations or [])
                ],
            }
        return {
            "stages": [stage.journal() for stage in self.stages],
            "terminal": terminal,
        }


def enrich_violation(stage: StageConfig, violation: dict[str, Any]) -> dict[str, Any]:
    clean = {
        key: deep_copy(value)
        for key, value in violation.items()
        if key in VIOLATION_KEYS
    }
    enriched = {
        "plugin": stage.plugin,
        "plugin_version": stage.plugin_version,
        "stage_id": stage.stage_id,
    }
    enriched.update(clean)
    return enriched


class PipelineRunner:
    def __init__(self, config: Config, plugins: PluginManager, telemetry: Any):
        self.config = config
        self.plugins = plugins
        self.telemetry = telemetry

    def run(
        self,
        direction: str,
        envelope: dict[str, Any],
        request_id: str,
        attempt: int,
        state_store: StateStore,
        server_request: dict[str, Any] | None = None,
    ) -> PipelineResult:
        stages = (
            self.config.pipeline.server_bound
            if direction == "server_bound"
            else self.config.pipeline.client_bound
        )
        current = deep_copy(envelope)
        staged = state_store.snapshot()
        result = PipelineResult(envelope=current, terminal=None)
        if not stages:
            return result

        for stage in stages:
            before = deep_copy(current)
            scope = staged.get(stage.plugin, {})
            params: dict[str, Any] = {
                "stage_id": stage.stage_id,
                "hook": stage.hook,
                "direction": direction,
                "request_id": request_id,
                "attempt": attempt,
                "deadline_ms": stage.timeout_ms,
                "envelope": deep_copy(before),
                "state": deep_copy(scope),
            }
            if direction == "client_bound" and server_request is not None:
                params["context"] = {
                    "server_request": {
                        "kind": "chat.request",
                        "body": deep_copy(server_request),
                    }
                }
            self._emit(
                "stage.start",
                request_id=request_id,
                stage_id=stage.stage_id,
                plugin=stage.plugin,
                direction=direction,
                attempt=attempt,
            )
            started = time.monotonic()
            input_hash = json_hash(before)
            try:
                raw_result = self.plugins.get(stage.plugin).call_hook(
                    params, stage.timeout_ms
                )
                (
                    action,
                    new_envelope,
                    violations,
                    writes,
                    new_scope,
                    keys,
                ) = self._apply_result(stage, raw_result, before, scope)
            except StageFailure as failure:
                duration = int((time.monotonic() - started) * 1000)
                record = StageRecord(
                    stage_id=stage.stage_id,
                    plugin=stage.plugin,
                    plugin_version=stage.plugin_version,
                    action="error",
                    duration_ms=duration,
                    input_hash=input_hash,
                    output_hash=None,
                    failure=failure.kind,
                )
                result.stages.append(record)
                self._emit(
                    "stage.end",
                    request_id=request_id,
                    stage_id=stage.stage_id,
                    plugin=stage.plugin,
                    direction=direction,
                    attempt=attempt,
                    action="error",
                    failure=failure.kind,
                )
                terminal = self._handle_failure(stage, failure, request_id, direction)
                if terminal is not None:
                    result.terminal = terminal
                    result.terminal_stage_id = stage.stage_id
                    result.envelope = before
                    self._finish_writes(result, committed=False)
                    return result
                current = before
                result.envelope = current
                continue

            duration = int((time.monotonic() - started) * 1000)
            record = StageRecord(
                stage_id=stage.stage_id,
                plugin=stage.plugin,
                plugin_version=stage.plugin_version,
                action=action,
                duration_ms=duration,
                input_hash=input_hash,
                output_hash=None if action == "terminate" else json_hash(new_envelope),
                violations=[
                    {"code": violation["code"], "retryable": violation["retryable"]}
                    for violation in violations
                ]
                if violations
                else None,
            )
            result.stages.append(record)
            self._emit(
                "stage.end",
                request_id=request_id,
                stage_id=stage.stage_id,
                plugin=stage.plugin,
                direction=direction,
                attempt=attempt,
                action=action,
                duration_ms=duration,
            )
            if action == "terminate":
                if writes is not None:
                    result.state_writes.append(
                        StateWriteRecord(
                            stage_id=stage.stage_id,
                            plugin=stage.plugin,
                            attempt=attempt,
                            keys=keys,
                            values=(
                                deep_copy(writes)
                                if self.config.journal.include_bodies
                                else None
                            ),
                        )
                    )
                result.terminal = policy_terminal(request_id, direction, violations)
                result.terminal_stage_id = stage.stage_id
                result.envelope = before
                self._finish_writes(result, committed=False)
                return result

            current = new_envelope
            result.envelope = current
            if writes is not None:
                staged[stage.plugin] = new_scope
                result.state_writes.append(
                    StateWriteRecord(
                        stage_id=stage.stage_id,
                        plugin=stage.plugin,
                        attempt=attempt,
                        keys=keys,
                        values=(
                            deep_copy(writes)
                            if self.config.journal.include_bodies
                            else None
                        ),
                    )
                )
        state_store.commit(staged)
        self._finish_writes(result, committed=True)
        return result

    def _finish_writes(self, result: PipelineResult, committed: bool) -> None:
        for record in result.state_writes:
            record.committed = committed

    def _apply_result(
        self,
        stage: StageConfig,
        raw: Any,
        before: dict[str, Any],
        scope: dict[str, Any],
    ) -> tuple[
        str, dict[str, Any], list[dict[str, Any]], Any, dict[str, Any], list[str]
    ]:
        if not isinstance(raw, dict):
            raise InvalidResult("hook result must be an object")
        state_writes = raw.get("state")
        if stage.read_only and state_writes not in (None, {}):
            if not isinstance(state_writes, dict) or any(
                state_writes.get(key) for key in ("set", "delete")
            ):
                raise InvalidResult("read-only plugin must not write state")
        allowed = {"action", "patch", "body", "state", "violations"}
        unknown = set(raw) - allowed
        if unknown:
            raise InvalidResult(
                f"hook result has unknown key(s): {', '.join(sorted(unknown))}"
            )
        action = raw.get("action")
        if action not in {"continue", "patch", "replace", "terminate"}:
            raise InvalidResult("action must be continue, patch, replace, or terminate")
        if action == "patch":
            if "patch" not in raw:
                raise InvalidResult("patch action requires patch")
            if "body" in raw:
                raise InvalidResult("patch and replace are mutually exclusive")
        elif action == "replace":
            if "body" not in raw:
                raise InvalidResult("replace action requires body")
            if "patch" in raw:
                raise InvalidResult("patch and replace are mutually exclusive")
        else:
            if "patch" in raw or "body" in raw:
                raise InvalidResult(f"{action} action must not carry patch or body")

        raw_violations = raw.get("violations") or []
        if not isinstance(raw_violations, list):
            raise InvalidResult("violations must be a list")
        if action == "terminate":
            if not raw_violations:
                raise InvalidResult("terminate requires at least one violation")
            violations = [
                enrich_violation(
                    stage, validate_violation(value, "violation", InvalidResult)
                )
                for value in raw_violations
            ]
        else:
            if raw_violations:
                raise InvalidResult("violations are only valid with terminate")
            violations = []

        if action == "continue":
            new_envelope = before
        elif action == "patch":
            patch = raw.get("patch")
            if not isinstance(patch, dict):
                raise InvalidResult("patch must be an object")
            body = merge_patch(before["body"], patch)
            new_envelope = {
                "kind": before["kind"],
                "body": body,
                "meta": deep_copy(before["meta"]),
            }
        elif action == "replace":
            body = raw.get("body")
            if not isinstance(body, dict):
                raise InvalidResult("body must be an object")
            new_envelope = {
                "kind": before["kind"],
                "body": deep_copy(body),
                "meta": deep_copy(before["meta"]),
            }
        else:  # terminate
            new_envelope = before

        if action == "terminate":
            writes = raw.get("state")
            keys: list[str] = []
            if writes is not None:
                _, keys = apply_writes(scope, writes)
            return action, before, violations, writes, scope, keys

        try:
            validate_body(before["kind"], new_envelope["body"])
        except Exception as exc:
            raise InvalidStageOutput(str(exc)) from exc
        new_scope, keys = apply_writes(scope, raw.get("state"))
        return action, new_envelope, violations, raw.get("state"), new_scope, keys

    def _handle_failure(
        self, stage: StageConfig, failure: StageFailure, request_id: str, direction: str
    ) -> Terminal | None:
        if stage.on_error == "fail_open":
            return None
        if stage.on_error == "fail_verdict":
            violations = [
                enrich_violation(
                    stage, validate_violation(value, "violation", InvalidResult)
                )
                for value in stage.fallback_violations
            ]
            return policy_terminal(request_id, direction, violations, kind="host_error")
        return plugin_failure(
            request_id, stage.stage_id, failure.code(), failure.kind, direction
        )

    def _emit(self, event: str, **fields: Any) -> None:
        if self.telemetry is not None:
            try:
                self.telemetry.emit(event, **fields)
            except Exception:
                pass
