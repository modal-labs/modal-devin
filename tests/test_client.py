from __future__ import annotations

import json
import urllib.error
from email.message import Message

import pytest

from modal_devin import ConfigurationError, OutpostsAPIError, OutpostsProtocolError
from modal_devin._client import ClaimConflict, OutpostsClient, PendingSession, SessionStatus


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


class RecordingUrlOpen:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []
        self.timeouts = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        body = self.bodies.pop(0)
        if isinstance(body, BaseException):
            raise body
        return Response(json.dumps(body).encode())


def make_client(recorder):
    return OutpostsClient(
        "https://api.example.com/",
        "token-123",
        timeout=7.5,
        urlopen=recorder,
    )


def test_pending_sessions_are_typed_and_requests_have_standard_headers():
    recorder = RecordingUrlOpen(
        [
            {
                "items": [
                    {
                        "metadata": {"session_id": "devin-1"},
                        "status": {"session_status": "running"},
                    }
                ]
            }
        ]
    )

    result = make_client(recorder).pending_sessions("outpost with spaces")

    assert result == (PendingSession("devin-1", SessionStatus.RUNNING),)
    [request] = recorder.requests
    assert request.full_url.endswith("outpost=outpost+with+spaces&phase=pending&first=200")
    assert request.get_header("Authorization") == "Bearer token-123"
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("User-agent") == "modal-devin"
    assert recorder.timeouts == [7.5]


def test_claim_quotes_session_id_and_parses_deadline():
    recorder = RecordingUrlOpen([{"status": {"claim_deadline": "tomorrow"}}])

    claim = make_client(recorder).claim("session/1", "modal-worker")

    assert claim.deadline == "tomorrow"
    [request] = recorder.requests
    assert "/session%2F1/claim" in request.full_url
    assert json.loads(request.data) == {"acceptor_id": "modal-worker"}


def test_claim_conflict_has_a_domain_exception():
    error = urllib.error.HTTPError("url", 409, "Conflict", Message(), None)
    recorder = RecordingUrlOpen([error])

    with pytest.raises(ClaimConflict):
        make_client(recorder).claim("devin-1", "modal-worker")


def test_http_and_transport_failures_have_domain_exceptions():
    server_error = urllib.error.HTTPError("url", 500, "Nope", Message(), None)
    with pytest.raises(OutpostsAPIError):
        make_client(RecordingUrlOpen([server_error])).pending_sessions("outpost")

    with pytest.raises(OutpostsAPIError):
        make_client(RecordingUrlOpen([urllib.error.URLError("offline")])).pending_sessions(
            "outpost"
        )


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"not_items": []},
        {"items": ["not-an-object"]},
        {"items": [{"bad": "item"}]},
    ],
)
def test_malformed_responses_raise_protocol_errors(body):
    recorder = RecordingUrlOpen([body])

    with pytest.raises(OutpostsProtocolError):
        make_client(recorder).pending_sessions("outpost")


def test_status_distinguishes_successful_absence_from_request_failure():
    not_found = urllib.error.HTTPError("url", 404, "Not Found", Message(), None)
    client = make_client(RecordingUrlOpen([not_found]))
    assert client.session_status("devin-1") is None

    failing = make_client(RecordingUrlOpen([urllib.error.URLError("offline")]))
    with pytest.raises(OutpostsAPIError):
        failing.session_status("devin-1")


def test_status_is_parsed_into_a_closed_protocol_type():
    known = make_client(RecordingUrlOpen([{"status": {"session_status": "suspended"}}]))

    assert known.session_status("devin-1") is SessionStatus.SUSPENDED

    unknown = make_client(RecordingUrlOpen([{"status": {"session_status": "future-state"}}]))
    with pytest.raises(OutpostsProtocolError, match="unknown Outposts session status"):
        unknown.session_status("devin-1")

    missing = make_client(RecordingUrlOpen([{"status": {}}]))
    with pytest.raises(OutpostsProtocolError, match=r"status\.session_status"):
        missing.session_status("devin-1")


def test_pending_sessions_paginate_and_deduplicate_page_boundaries():
    recorder = RecordingUrlOpen(
        [
            {
                "items": [{"metadata": {"session_id": "devin-1"}}],
                "cursor": "next",
                "has_next_page": True,
            },
            {
                "items": [
                    {"metadata": {"session_id": "devin-1"}},
                    {"metadata": {"session_id": "devin-2"}},
                ],
                "cursor": "done",
                "has_next_page": False,
            },
        ]
    )

    assert make_client(recorder).pending_sessions("outpost") == (
        PendingSession("devin-1", None),
        PendingSession("devin-2", None),
    )
    assert "cursor=next" in recorder.requests[1].full_url


def test_pending_sessions_parse_session_status_into_the_closed_protocol_type():
    recorder = RecordingUrlOpen(
        [
            {
                "items": [
                    {
                        "metadata": {"session_id": "devin-1"},
                        "status": {"session_status": "suspended"},
                    }
                ]
            }
        ]
    )

    assert make_client(recorder).pending_sessions("outpost") == (
        PendingSession("devin-1", SessionStatus.SUSPENDED),
    )

    unknown = RecordingUrlOpen(
        [
            {
                "items": [
                    {
                        "metadata": {"session_id": "devin-1"},
                        "status": {"session_status": "future-state"},
                    }
                ]
            }
        ]
    )
    with pytest.raises(OutpostsProtocolError, match="unknown Outposts session status"):
        make_client(unknown).pending_sessions("outpost")


def test_pending_sessions_reject_non_adjacent_cursor_cycles():
    recorder = RecordingUrlOpen(
        [
            {"items": [], "cursor": "A", "has_next_page": True},
            {"items": [], "cursor": "B", "has_next_page": True},
            {"items": [], "cursor": "A", "has_next_page": True},
        ]
    )

    with pytest.raises(OutpostsProtocolError, match="repeated cursor 'A'"):
        make_client(recorder).pending_sessions("outpost")

    assert len(recorder.requests) == 3


def test_response_size_is_bounded():
    def oversized(request, *, timeout):
        return Response(b"{" + b"x" * (1024 * 1024 + 1))

    client = OutpostsClient(
        "https://api.example.com",
        "token",
        timeout=1,
        urlopen=oversized,
    )

    with pytest.raises(OutpostsProtocolError, match="exceeded 1 MiB"):
        client.pending_sessions("outpost")


def test_create_outpost_posts_to_opbeta_outposts_and_returns_the_id():
    recorder = RecordingUrlOpen([{"metadata": {"outpost_id": "outpost_env-demo"}}])

    outpost_id = make_client(recorder).create_outpost("demo")

    assert outpost_id == "outpost_env-demo"
    [request] = recorder.requests
    assert request.full_url == "https://api.example.com/opbeta/outposts"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {
        "name": "demo",
        "platform": "linux",
        "description": "",
    }
    assert request.get_header("Authorization") == "Bearer token-123"


def test_create_outpost_rejects_invalid_names_without_a_request():
    recorder = RecordingUrlOpen([])

    with pytest.raises(ConfigurationError, match="lowercase letters"):
        make_client(recorder).create_outpost("Bad Name")

    assert recorder.requests == []


def test_create_outpost_raises_a_protocol_error_when_the_id_is_missing():
    recorder = RecordingUrlOpen([{"metadata": {}}])

    with pytest.raises(OutpostsProtocolError, match="outpost_id"):
        make_client(recorder).create_outpost("demo")


def test_delete_outpost_sends_a_quoted_delete_request():
    recorder = RecordingUrlOpen([{}])

    make_client(recorder).delete_outpost("outpost/env demo")

    [request] = recorder.requests
    assert request.full_url == "https://api.example.com/opbeta/outposts/outpost%2Fenv%20demo"
    assert request.get_method() == "DELETE"
    assert request.get_header("Authorization") == "Bearer token-123"


def test_delete_outpost_raises_on_http_failure():
    error = urllib.error.HTTPError("url", 404, "Not Found", Message(), None)
    with pytest.raises(OutpostsAPIError):
        make_client(RecordingUrlOpen([error])).delete_outpost("outpost_env-demo")
