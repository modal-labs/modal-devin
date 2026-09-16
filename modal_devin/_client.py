"""Typed boundary around the Devin Outposts HTTP API."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Protocol, TypeAlias, cast

from modal_devin._config import validate_outpost_name
from modal_devin._exceptions import OutpostsAPIError, OutpostsProtocolError

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_LIST_PAGES = 10_000


class HTTPResponse(Protocol):
    def __enter__(self) -> HTTPResponse: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def read(self, size: int = -1) -> bytes: ...


class UrlOpen(Protocol):
    def __call__(self, request: urllib.request.Request, *, timeout: float) -> HTTPResponse: ...


_DEFAULT_URLOPEN = cast(UrlOpen, urllib.request.urlopen)


class ClaimConflict(OutpostsAPIError):
    """The session was claimed by another worker first."""


class _HTTPError(OutpostsAPIError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Claim:
    deadline: str | None
    connect_token: str | None
    gateway_url: str | None
    remote_binary_sha: str | None = None


class SessionStatus(StrEnum):
    """Session states understood by the Outposts worker protocol."""

    NEW = "new"
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    RESUMING = "resuming"
    SUSPENDED = "suspended"
    EXIT = "exit"
    ERROR = "error"
    TERMINATED = "terminated"


@dataclass(frozen=True, slots=True)
class PendingSession:
    """One queue entry in the pending phase."""

    session_id: str
    session_status: SessionStatus | None


def _json_object(body: bytes, *, url: str) -> JsonObject:
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OutpostsProtocolError(f"invalid JSON response from {url}") from error
    if not isinstance(parsed, dict):
        raise OutpostsProtocolError(
            f"expected a JSON object from {url}, got {type(parsed).__name__}"
        )
    return cast(JsonObject, parsed)


def _items(response: Mapping[str, JsonValue]) -> Iterable[Mapping[str, JsonValue]]:
    items = response.get("items")
    if not isinstance(items, list):
        raise OutpostsProtocolError("Outposts response is missing an items array")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise OutpostsProtocolError(
                f"expected items[{index}] to be an object, got {type(item).__name__}"
            )
        yield item


def _nested_string(mapping: Mapping[str, JsonValue], outer_key: str, inner_key: str) -> str | None:
    outer = mapping.get(outer_key)
    if not isinstance(outer, dict):
        return None
    value = outer.get(inner_key)
    return value if isinstance(value, str) else None


def _required_nested_string(
    mapping: Mapping[str, JsonValue],
    outer_key: str,
    inner_key: str,
    *,
    context: str,
) -> str:
    value = _nested_string(mapping, outer_key, inner_key)
    if value is None:
        raise OutpostsProtocolError(f"{context} is missing {outer_key}.{inner_key}")
    return value


def _session_status(value: str) -> SessionStatus:
    try:
        return SessionStatus(value)
    except ValueError as error:
        raise OutpostsProtocolError(f"unknown Outposts session status: {value!r}") from error


class OutpostsClient:
    """Small synchronous client used by the worker runtime."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float,
        urlopen: UrlOpen = _DEFAULT_URLOPEN,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._urlopen = urlopen

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, JsonValue] | None = None,
    ) -> JsonObject:
        data = json.dumps(body).encode() if body is not None else None
        url = f"{self.base_url}/{path.lstrip('/')}"
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("User-Agent", "modal-devin")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                response_body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(response_body) > _MAX_RESPONSE_BYTES:
                    raise OutpostsProtocolError(f"response from {url} exceeded 1 MiB")
                return _json_object(response_body, url=url)
        except urllib.error.HTTPError as error:
            raise _HTTPError(error.code, f"request to {url} failed: {error}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise OutpostsAPIError(f"request to {url} failed: {error}") from error

    @staticmethod
    def _session_path(session_id: str, action: str) -> str:
        encoded = urllib.parse.quote(session_id, safe="")
        return f"/opbeta/outposts/devins/{encoded}/{action}"

    @staticmethod
    def _session_resource_path(session_id: str) -> str:
        encoded = urllib.parse.quote(session_id, safe="")
        return f"/opbeta/outposts/devins/{encoded}"

    def create_outpost(self, name: str, *, platform: str = "linux", description: str = "") -> str:
        validate_outpost_name(name)
        response = self._request(
            "POST",
            "/opbeta/outposts",
            {"name": name, "platform": platform, "description": description},
        )
        return _required_nested_string(response, "metadata", "outpost_id", context="outpost create")

    def delete_outpost(self, outpost_id: str) -> None:
        encoded = urllib.parse.quote(outpost_id, safe="")
        self._request("DELETE", f"/opbeta/outposts/{encoded}")

    def pending_sessions(self, outpost_id: str) -> tuple[PendingSession, ...]:
        sessions: dict[str, PendingSession] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page = 0
        while True:
            if page >= _MAX_LIST_PAGES:
                raise OutpostsProtocolError(f"Outposts pagination exceeded {_MAX_LIST_PAGES} pages")
            parameters = {"outpost": outpost_id, "phase": "pending", "first": "200"}
            if cursor is not None:
                parameters["cursor"] = cursor
            query = urllib.parse.urlencode(parameters)
            response = self._request("GET", f"/opbeta/outposts/devins?{query}")
            for index, item in enumerate(_items(response)):
                session_id = _required_nested_string(
                    item,
                    "metadata",
                    "session_id",
                    context=f"pending page {page} item {index}",
                )
                status_value = _nested_string(item, "status", "session_status")
                sessions[session_id] = PendingSession(
                    session_id=session_id,
                    session_status=None if status_value is None else _session_status(status_value),
                )
            has_next_page = response.get("has_next_page", False)
            if not isinstance(has_next_page, bool):
                raise OutpostsProtocolError("Outposts response has a non-boolean has_next_page")
            if not has_next_page:
                return tuple(sessions.values())
            next_cursor = response.get("cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise OutpostsProtocolError(
                    "paginated Outposts response is missing a usable cursor"
                )
            if next_cursor in seen_cursors:
                raise OutpostsProtocolError(f"Outposts pagination repeated cursor {next_cursor!r}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            page += 1

    def claim(self, session_id: str, acceptor_id: str) -> Claim:
        try:
            response = self._request(
                "POST",
                self._session_path(session_id, "claim"),
                {"acceptor_id": acceptor_id},
            )
        except _HTTPError as error:
            if error.status_code == 409:
                raise ClaimConflict(session_id) from error
            raise OutpostsAPIError(f"claim request for {session_id!r} failed: {error}") from error
        return Claim(
            deadline=_nested_string(response, "status", "claim_deadline"),
            connect_token=_nested_string(response, "status", "connect_token"),
            gateway_url=_nested_string(response, "status", "gateway_url"),
            remote_binary_sha=_nested_string(response, "spec", "remote_binary_sha"),
        )

    def release(self, session_id: str, acceptor_id: str) -> None:
        self._request(
            "POST",
            self._session_path(session_id, "release"),
            {"acceptor_id": acceptor_id},
        )

    def session_status(self, session_id: str) -> SessionStatus | None:
        try:
            response = self._request("GET", self._session_resource_path(session_id))
        except _HTTPError as error:
            if error.status_code == 404:
                return None
            raise OutpostsAPIError(f"status request for {session_id!r} failed: {error}") from error
        value = _required_nested_string(
            response,
            "status",
            "session_status",
            context=f"session {session_id!r}",
        )
        return _session_status(value)

    def remote_binary_sha(self, session_id: str) -> str | None:
        """Return the devin-remote git SHA the session's queue entry pins, if any."""
        try:
            response = self._request("GET", self._session_resource_path(session_id))
        except _HTTPError as error:
            if error.status_code == 404:
                return None
            raise OutpostsAPIError(f"spec request for {session_id!r} failed: {error}") from error
        return _nested_string(response, "spec", "remote_binary_sha")
