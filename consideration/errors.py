"""Error and failure types shared by the host components."""

from __future__ import annotations


class ConsiderationError(Exception):
    """Base class for host errors."""


class RequestError(ConsiderationError):
    """The client request is structurally invalid."""


class EgressError(ConsiderationError):
    """A body failed structural validation before leaving the host."""


class StageFailure(Exception):
    """A non-terminating stage failure, handled by the stage's on_error policy."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.detail = detail
        super().__init__(detail or kind)

    def code(self) -> str:
        return {
            "timeout": "plugin_timeout",
            "crash": "plugin_crash",
            "protocol_error": "plugin_protocol_error",
            "invalid_result": "plugin_invalid_output",
            "invalid_stage_output": "plugin_invalid_output",
            "unavailable": "plugin_unavailable",
        }.get(self.kind, "internal")


class PluginTimeout(StageFailure):
    def __init__(self, detail: str = "timeout"):
        super().__init__("timeout", detail)


class PluginCrash(StageFailure):
    def __init__(self, detail: str = "plugin process died"):
        super().__init__("crash", detail)


class PluginProtocolError(StageFailure):
    def __init__(self, detail: str = "protocol error"):
        super().__init__("protocol_error", detail)


class PluginUnavailable(StageFailure):
    def __init__(self, detail: str = "plugin unavailable"):
        super().__init__("unavailable", detail)


class InvalidResult(StageFailure):
    def __init__(self, detail: str = "invalid result"):
        super().__init__("invalid_result", detail)


class InvalidStageOutput(StageFailure):
    def __init__(self, detail: str = "invalid stage output"):
        super().__init__("invalid_stage_output", detail)
