"""The host's only HTTP client. Plugins never see this module."""

from __future__ import annotations

import fnmatch
import http.client
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import Config
from .errors import ConsiderationError

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

CREDENTIAL_HEADERS = {"authorization", "cookie", "proxy-authorization", "set-cookie"}


class UpstreamTimeout(ConsiderationError):
    pass


class UpstreamUnreachable(ConsiderationError):
    pass


@dataclass
class UpstreamResponse:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return None

    def header_dict(self) -> dict[str, str]:
        return {key.lower(): value for key, value in self.headers}


def connection_header_tokens(headers: Iterable[tuple[str, str]]) -> set[str]:
    tokens: set[str] = set()
    for name, value in headers:
        if name.lower() == "connection":
            tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )
    return tokens


def is_credential_header(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in CREDENTIAL_HEADERS
        or "key" in lowered
        or "token" in lowered
        or "secret" in lowered
    )


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy without credential- or cookie-bearing header values."""
    return {
        name: value for name, value in headers.items() if not is_credential_header(name)
    }


class UpstreamClient:
    def __init__(self, config: Config):
        self.config = config
        parts = urlsplit(config.server.base_url)
        self.scheme = parts.scheme
        self.host = parts.hostname or ""
        self.port = parts.port
        self.base_path = parts.path.rstrip("/")

    def target(self, path: str) -> str:
        if self.base_path and path.startswith(self.base_path + "/"):
            return self.base_path + path[len(self.base_path) :]
        return self.base_path + path

    def prepare_headers(
        self, client_headers: dict[str, str], body_length: int | None
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        nominated = connection_header_tokens(client_headers.items())
        for name, value in client_headers.items():
            lowered = name.lower()
            if (
                lowered in HOP_BY_HOP
                or lowered in nominated
                or lowered == "accept-encoding"
            ):
                continue
            if lowered == "authorization":
                headers["authorization"] = value
                continue
            if is_credential_header(lowered):
                continue
            if any(
                fnmatch.fnmatch(lowered, pattern.lower())
                for pattern in self.config.header_allowlist
            ):
                headers[name] = value
        for name, value in self.config.server.headers.items():
            headers[name] = value
        if self.config.server.organization and "openai-organization" not in {
            key.lower() for key in headers
        }:
            headers["openai-organization"] = self.config.server.organization
        nominated = connection_header_tokens(headers.items())
        headers = {
            name: value
            for name, value in headers.items()
            if name.lower() not in HOP_BY_HOP
            and name.lower() not in nominated
            and name.lower() != "accept-encoding"
        }
        if body_length is not None:
            headers["content-length"] = str(body_length)
        return headers

    def _connection(self, timeout_s: float):
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout_s)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout_s)

    def open(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_ms: int,
    ):
        connection = self._connection(timeout_ms / 1000.0)
        try:
            connection.request(method, self.target(path), body=body, headers=headers)
            response = connection.getresponse()
        except TimeoutError as exc:
            connection.close()
            raise UpstreamTimeout(str(exc)) from exc
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            raise UpstreamUnreachable(str(exc)) from exc
        return connection, response

    def request(
        self,
        method: str,
        path: str,
        client_headers: dict[str, str],
        body: bytes | None,
        timeout_ms: int,
    ) -> UpstreamResponse:
        headers = self.prepare_headers(
            client_headers, len(body) if body is not None else None
        )
        connection, response = self.open(method, path, headers, body, timeout_ms)
        try:
            try:
                data = response.read()
            except TimeoutError as exc:
                raise UpstreamTimeout(str(exc)) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise UpstreamUnreachable(str(exc)) from exc
            return UpstreamResponse(response.status, response.getheaders(), data)
        finally:
            connection.close()


def filter_response_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return dict(filter_response_header_items(headers))


def filter_response_header_items(
    headers: Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
    values = list(headers)
    nominated = connection_header_tokens(values)
    result: list[tuple[str, str]] = []
    for name, value in values:
        lowered = name.lower()
        if lowered in HOP_BY_HOP or lowered in nominated:
            continue
        result.append((name, value))
    return result
