"""Plugin process management, handshake, and supervision."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import CONTRACT_VERSION
from .config import Config, PluginEntry, contract_compatible
from .errors import (
    PluginCrash,
    PluginProtocolError,
    PluginTimeout,
    PluginUnavailable,
    StageFailure,
)
from .protocol import read_frame, write_frame
from .util import new_namespace_id


class _Pending:
    __slots__ = ("event", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: dict[str, Any] | None = None


def _close_streams(proc: subprocess.Popen[bytes]) -> None:
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def _kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """The one platform-specific shutdown path (14.3)."""
    if proc.poll() is None:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass
    _close_streams(proc)


class PluginInstance:
    """One child process for one plugin id. Never shared across plugin ids."""

    def __init__(self, entry: PluginEntry, config: Config, telemetry: Any):
        self.entry = entry
        self.config = config
        self.telemetry = telemetry
        self.namespace_id = new_namespace_id()
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 1
        self._proc: subprocess.Popen[bytes] | None = None
        self._generation = 0
        self._alive = False
        self._starting = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._slots = threading.BoundedSemaphore(config.plugins.workers)
        self._restart_failures = 0
        self._last_restart_attempt = 0.0
        self._circuit_until = 0.0
        self._health_failures = 0
        self._unhealthy = False
        self.ready = False
        self.disabled = False
        self.manifest = entry.manifest
        self.version = str(entry.manifest.get("version", ""))

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._starting:
                return
            self._starting = True
        try:
            self._spawn_and_handshake()
            self._start_health_thread()
        finally:
            with self._lock:
                self._starting = False

    def _spawn_and_handshake(self) -> None:
        env = dict(os.environ)
        env.update(self.entry.env)
        command = list(self.entry.command)
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            proc = subprocess.Popen(
                command,
                cwd=self.entry.cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=-1,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise PluginCrash(f"could not spawn {self.entry.id}: {exc}") from exc

        with self._lock:
            self._generation += 1
            generation = self._generation
            self._proc = proc
            self._alive = True
            self._unhealthy = False
            self._health_failures = 0
        reader = threading.Thread(
            target=self._reader_loop,
            args=(generation, proc),
            name=f"plugin-reader-{self.entry.id}",
            daemon=True,
        )
        reader.start()
        self._threads.append(reader)
        if proc.stderr is not None:
            stderr_thread = threading.Thread(
                target=self._stderr_loop,
                args=(generation, proc),
                name=f"plugin-stderr-{self.entry.id}",
                daemon=True,
            )
            stderr_thread.start()
            self._threads.append(stderr_thread)

        try:
            described = self._request("plugin/describe", {}, self.config.plugins.startup_timeout_ms)
        except StageFailure:
            _kill_process_tree(proc)
            raise
        if not isinstance(described, dict):
            _kill_process_tree(proc)
            raise PluginProtocolError("plugin/describe did not return an object")
        if described.get("id") != self.entry.id:
            _kill_process_tree(proc)
            raise PluginProtocolError(
                f"plugin/describe id {described.get('id')!r} does not match {self.entry.id!r}"
            )
        contract = described.get("contract")
        if not isinstance(contract, str) or not contract_compatible(CONTRACT_VERSION, contract):
            _kill_process_tree(proc)
            raise PluginProtocolError(
                f"plugin contract {contract!r} is incompatible with {CONTRACT_VERSION}"
            )
        for key in ("version", "hooks", "read_only"):
            if key not in described:
                _kill_process_tree(proc)
                raise PluginProtocolError(f"plugin/describe is missing {key}")
        try:
            initialized = self._request(
                "plugin/init",
                {
                    "config": self.entry.config,
                    "plugin_id": self.entry.id,
                    "namespace_id": self.namespace_id,
                    "contract_version": CONTRACT_VERSION,
                },
                self.config.plugins.startup_timeout_ms,
            )
        except StageFailure:
            _kill_process_tree(proc)
            raise
        if not isinstance(initialized, dict) or initialized.get("ok") is not True:
            _kill_process_tree(proc)
            raise PluginProtocolError("plugin/init did not return {ok: true}")
        with self._lock:
            self.ready = True
            self._restart_failures = 0
            self._circuit_until = 0.0
        self._emit("plugin.lifecycle", action="ready", plugin=self.entry.id, version=self.version)

    def _start_health_thread(self) -> None:
        thread = threading.Thread(
            target=self._health_loop,
            name=f"plugin-health-{self.entry.id}",
            daemon=True,
        )
        thread.start()
        self._threads.append(thread)

    def _health_loop(self) -> None:
        interval = self.config.plugins.health_interval_ms / 1000.0
        while not self._stop.wait(interval):
            if self.disabled:
                continue
            with self._lock:
                circuit_open = time.monotonic() < self._circuit_until
                alive = self._alive and self._proc is not None and self._proc.poll() is None
            if circuit_open:
                continue
            if not alive:
                try:
                    self._restart()
                except StageFailure as exc:
                    self._emit(
                        "plugin.lifecycle",
                        action="restart_failed",
                        plugin=self.entry.id,
                        detail=str(exc),
                    )
                continue
            try:
                self._request("$/ping", {}, self.config.plugins.health_interval_ms)
                with self._lock:
                    self._health_failures = 0
            except StageFailure:
                with self._lock:
                    self._health_failures += 1
                    failures = self._health_failures
                if failures >= 3:
                    with self._lock:
                        self._unhealthy = True
                    self._emit("plugin.lifecycle", action="unhealthy", plugin=self.entry.id)
                    proc = self._proc
                    if proc is not None:
                        _kill_process_tree(proc)
                    try:
                        self._restart()
                    except StageFailure:
                        pass

    def _restart(self) -> None:
        with self._lock:
            if self.disabled:
                raise PluginUnavailable(f"plugin {self.entry.id} is disabled")
            if (
                self._alive
                and not self._unhealthy
                and self._proc is not None
                and self._proc.poll() is None
            ):
                return
            now = time.monotonic()
            if now < self._circuit_until:
                raise PluginUnavailable(
                    f"plugin {self.entry.id} circuit is open for "
                    f"{int(self._circuit_until - now)}s"
                )
            if self._restart_failures:
                backoff = min(2 ** (self._restart_failures - 1), 30)
                if now - self._last_restart_attempt < backoff:
                    raise PluginUnavailable(
                        f"plugin {self.entry.id} is in restart backoff"
                    )
            self._last_restart_attempt = now
            proc = self._proc
        if proc is not None:
            _kill_process_tree(proc)
        self._emit("plugin.lifecycle", action="restart", plugin=self.entry.id)
        try:
            self._spawn_and_handshake()
        except StageFailure:
            with self._lock:
                self._restart_failures += 1
                if self._restart_failures >= 5:
                    self._circuit_until = time.monotonic() + 60.0
                    self._restart_failures = 0
                    self._emit(
                        "plugin.lifecycle", action="circuit_open", plugin=self.entry.id
                    )
            raise

    def ensure_available(self) -> None:
        if self.disabled:
            raise PluginUnavailable(f"plugin {self.entry.id} is disabled")
        with self._lock:
            alive = self._alive and self._proc is not None and self._proc.poll() is None
            unhealthy = self._unhealthy
        if alive and not unhealthy:
            return
        self._restart()

    # -- calls ---------------------------------------------------------

    def call_hook(self, params: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        return self._request("hook", params, timeout_ms)

    def _request(self, method: str, params: dict[str, Any], timeout_ms: int) -> Any:
        # Restart and handshake happen before taking a worker slot: the
        # handshake itself issues requests and must not deadlock at workers=1.
        self.ensure_available()
        acquired = self._slots.acquire(timeout=timeout_ms / 1000.0)
        if not acquired:
            raise PluginTimeout(f"{method} timed out waiting for a worker slot")
        try:
            with self._lock:
                request_id = self._next_id
                self._next_id += 1
                pending = _Pending()
                self._pending[request_id] = pending
            try:
                with self._write_lock:
                    proc = self._proc
                    if proc is None or proc.stdin is None or proc.poll() is not None:
                        raise PluginCrash("plugin process is not running")
                    write_frame(
                        proc.stdin,
                        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                    )
            except (BrokenPipeError, OSError) as exc:
                with self._lock:
                    self._pending.pop(request_id, None)
                raise PluginCrash(f"plugin stdin closed: {exc}") from exc

            if not pending.event.wait(timeout_ms / 1000.0):
                with self._lock:
                    self._pending.pop(request_id, None)
                self._cancel(request_id)
                raise PluginTimeout(f"{method} exceeded {timeout_ms}ms")

            response = pending.response
            if response is None:
                raise PluginCrash("plugin call failed")
            if "error" in response:
                error = response.get("error") or {}
                message = error.get("message", "plugin returned a JSON-RPC error")
                raise PluginProtocolError(str(message))
            if "result" not in response:
                raise PluginProtocolError("plugin response has neither result nor error")
            return response["result"]
        finally:
            self._slots.release()

    def _cancel(self, request_id: int) -> None:
        try:
            with self._write_lock:
                proc = self._proc
                if proc is not None and proc.stdin is not None and proc.poll() is None:
                    write_frame(
                        proc.stdin,
                        {"jsonrpc": "2.0", "method": "$/cancel", "params": {"id": request_id}},
                    )
        except Exception:
            pass

    # -- reader --------------------------------------------------------

    def _reader_loop(self, generation: int, proc: subprocess.Popen[bytes]) -> None:
        try:
            assert proc.stdout is not None
            while True:
                frame = read_frame(proc.stdout, self.config.plugins.max_frame_bytes)
                if frame is None:
                    break
                self._dispatch(generation, frame)
        except StageFailure as exc:
            self._fail_pending(generation, exc)
        except Exception as exc:
            self._fail_pending(generation, PluginProtocolError(str(exc)))
        finally:
            self._mark_dead(generation)
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass

    def _dispatch(self, generation: int, frame: dict[str, Any]) -> None:
        with self._lock:
            if generation != self._generation:
                return
        method = frame.get("method")
        request_id = frame.get("id")
        if method is not None and request_id is None:
            if method == "plugin/log":
                params = frame.get("params") or {}
                self._emit(
                    "plugin.log",
                    plugin=self.entry.id,
                    level=str(params.get("level", "info")),
                    message=str(params.get("message", ""))[:4096],
                )
            return
        if request_id is None:
            return
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is not None:
            pending.response = frame
            pending.event.set()
            return
        if method is not None:
            # A plugin initiated a request. v0 defines none; answer politely.
            try:
                with self._write_lock:
                    proc = self._proc
                    if proc is not None and proc.stdin is not None and proc.poll() is None:
                        write_frame(
                            proc.stdin,
                            {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "error": {"code": -32601, "message": "method not found"},
                            },
                        )
            except Exception:
                pass

    def _fail_pending(self, generation: int, exc: StageFailure) -> None:
        with self._lock:
            if generation != self._generation:
                return
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.response = None
            item.event.set()

    def _mark_dead(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._alive = False
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.response = None
            item.event.set()

    def _stderr_loop(self, generation: int, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stderr is not None
        try:
            for raw in proc.stderr:
                with self._lock:
                    if generation != self._generation:
                        return
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._emit("plugin.log", plugin=self.entry.id, level="stderr", message=line[:4096])
        except Exception:
            return
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass

    def _emit(self, event: str, **fields: Any) -> None:
        if self.telemetry is not None:
            try:
                self.telemetry.emit(event, **fields)
            except Exception:
                pass

    # -- shutdown ------------------------------------------------------

    def shutdown(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if proc.stdin is not None:
                    try:
                        write_frame(
                            proc.stdin,
                            {"jsonrpc": "2.0", "id": 0, "method": "plugin/shutdown", "params": {}},
                        )
                    except Exception:
                        pass
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=self.config.plugins.shutdown_timeout_ms / 1000.0)
                    except subprocess.TimeoutExpired:
                        _kill_process_tree(proc)
                else:
                    _kill_process_tree(proc)
            else:
                # The child exited on its own; the pipes still need closing.
                _close_streams(proc)
        finally:
            _close_streams(proc)
            with self._lock:
                self._alive = False


class PluginManager:
    def __init__(self, config: Config, telemetry: Any):
        self.config = config
        self.telemetry = telemetry
        self.instances: dict[str, PluginInstance] = {
            entry_id: PluginInstance(entry, config, telemetry)
            for entry_id, entry in config.plugins.entries.items()
        }

    def start_all(self) -> None:
        if not self.instances:
            return
        failures: dict[str, StageFailure] = {}
        with ThreadPoolExecutor(max_workers=len(self.instances)) as pool:
            futures = {
                entry_id: pool.submit(instance.start)
                for entry_id, instance in self.instances.items()
            }
            for entry_id, future in futures.items():
                try:
                    future.result()
                except StageFailure as exc:
                    failures[entry_id] = exc
        for entry_id, exc in failures.items():
            instance = self.instances[entry_id]
            entry = self.config.plugins.entries[entry_id]
            if entry.required:
                self.shutdown_all()
                raise PluginUnavailable(f"required plugin {entry_id} failed to start: {exc}")
            instance.disabled = True
            self._emit(
                "plugin.lifecycle",
                action="unavailable",
                plugin=entry_id,
                detail=str(exc),
            )

    def _emit(self, event: str, **fields: Any) -> None:
        if self.telemetry is not None:
            try:
                self.telemetry.emit(event, **fields)
            except Exception:
                pass

    def get(self, plugin_id: str) -> PluginInstance:
        return self.instances[plugin_id]

    def shutdown_all(self) -> None:
        threads = [
            threading.Thread(target=instance.shutdown)
            for instance in self.instances.values()
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=self.config.plugins.shutdown_timeout_ms / 1000.0 + 2)
