"""Claim, dispatch, and session lifecycle orchestration."""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import modal
from modal.stream_type import StreamType

from modal_devin import _observability as observability
from modal_devin._client import ClaimConflict, OutpostsClient, SessionStatus
from modal_devin._config import (
    WorkerConfig,
    WorkerSettings,
    _sandbox_lifetime_seconds,
)
from modal_devin._exceptions import (
    ClaimDeadlineError,
    OutpostsAPIError,
    OutpostsProtocolError,
    SessionAttachTimeoutError,
    SessionStatusUnknownError,
    WorkerExitedError,
)
from modal_devin.images import _DEVIN_BIN

logger = logging.getLogger("modal_devin.worker")

WORKDIR = "/root/workspace"
# In direct-serve mode (DEVIN_REMOTE_SESSION_TOKEN set) the CLI never contacts the
# queue API, but it refuses to start without *a* token present. This placeholder
# satisfies the presence check while keeping the real service user token out of the
# Sandbox entirely; the remote authenticates with the session-scoped connect token.
_PLACEHOLDER_TOKEN = "cog_sidecarmanaged00000000000000000000000000000000"
_TERMINAL_STATUSES = {
    SessionStatus.EXIT,
    SessionStatus.ERROR,
    SessionStatus.TERMINATED,
}
# A released suspended session reappears as phase=pending until the queue prunes it,
# but it is asleep, not awaiting a worker; claiming it produces an idle sandbox.
_UNSERVABLE_STATUSES = _TERMINAL_STATUSES | {SessionStatus.SUSPENDED}
# Consecutive suspended liveness readings tolerated before concluding the remote
# missed the session-end notification (the status can briefly read suspended while
# a healthy remote is still flushing its own clean exit).
_SUSPENDED_LIVENESS_STRIKES = 3
# The remote's own log line for a live gateway websocket, and the RPC log markers
# that prove Devin's brain actually attached to it. A websocket that connects but
# never receives an RPC is a dead session from the user's point of view ("your
# outpost machine is connecting" forever); only a fresh connection recovers it.
_GATEWAY_CONNECT_MARKER = "connected to outpost gateway"
_BRAIN_ATTACH_MARKERS = (
    "subscribe_remote_events",
    "hands_invoke",
    "prepare_tool",
    "execute_tool",
)


class _AttachState:
    """What the worker's own log stream proves about the gateway connection."""

    __slots__ = ("attached", "connected_at")

    def __init__(self) -> None:
        self.connected_at: float | None = None
        self.attached = False

    def observe(self, line: str) -> None:
        if self.attached:
            return
        if any(marker in line for marker in _BRAIN_ATTACH_MARKERS):
            self.attached = True
        elif _GATEWAY_CONNECT_MARKER in line:
            # A reconnect restarts the attach clock.
            self.connected_at = time.time()

    def timed_out(self, timeout_seconds: float) -> bool:
        return (
            not self.attached
            and self.connected_at is not None
            and time.time() - self.connected_at >= timeout_seconds
        )


_SNAPSHOT_KEY_PREFIX = "__modal_devin_snapshot__:"
_DISPATCH_KEY_PREFIX = "__modal_devin_dispatch__:"
_DISPATCH_EXPIRY_KEY = "expires_at"
_DISPATCH_TRACE_CONTEXT_KEY = "trace_context"
_SNAPSHOT_INDEX_REFRESH_KEY = "__modal_devin_snapshot_index_refreshed_at__"
_SNAPSHOT_INDEX_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60


class SnapshotStore(Protocol):
    def get(self, key: str, default: object = None) -> object: ...

    def put(self, key: str, value: object, *, skip_if_exists: bool = False) -> bool: ...

    def pop(self, key: str, default: object = ...) -> object: ...

    def keys(self) -> Iterable[object]: ...


def _session_store_key(prefix: str, session_id: str) -> str:
    return prefix + hashlib.sha256(session_id.encode()).hexdigest()


def _snapshot_key(session_id: str) -> str:
    return _session_store_key(_SNAPSHOT_KEY_PREFIX, session_id)


def _dispatch_key(session_id: str) -> str:
    return _session_store_key(_DISPATCH_KEY_PREFIX, session_id)


def _dispatch_lease_seconds(settings: WorkerSettings) -> int:
    """Bound duplicate dispatches while an invocation claims and starts its worker."""
    return math.ceil(
        settings.api_timeout_seconds
        + settings.sandbox_ready_timeout_seconds
        + settings.claim_connect_margin_seconds
    )


def _refresh_snapshot_index(snapshot_store: SnapshotStore) -> None:
    """Keep the Modal Dict index alive for snapshots with longer retention."""
    now = time.time()
    last_refresh = snapshot_store.get(_SNAPSHOT_INDEX_REFRESH_KEY)
    if isinstance(last_refresh, (int, float)) and (
        now - last_refresh < _SNAPSHOT_INDEX_REFRESH_INTERVAL_SECONDS
    ):
        return

    for key in snapshot_store.keys():  # noqa: SIM118 - remote Dict is not directly iterable
        if isinstance(key, str) and key.startswith(_SNAPSHOT_KEY_PREFIX):
            snapshot_store.get(key)
    snapshot_store.put(_SNAPSHOT_INDEX_REFRESH_KEY, now)


def _reserve_dispatch(
    snapshot_store: SnapshotStore,
    session_id: str,
    settings: WorkerSettings,
    trace_context: Mapping[str, str],
) -> bool:
    """Acquire a bounded queueing lease for one session dispatch."""
    key = _dispatch_key(session_id)
    now = time.time()
    current_expiry = _dispatch_expiry(snapshot_store.get(key))
    if isinstance(current_expiry, (int, float)) and current_expiry > now:
        return False
    if current_expiry is not None:
        snapshot_store.pop(key, None)
    return snapshot_store.put(
        key,
        {
            _DISPATCH_EXPIRY_KEY: now + _dispatch_lease_seconds(settings),
            _DISPATCH_TRACE_CONTEXT_KEY: dict(trace_context),
        },
        skip_if_exists=True,
    )


def _dispatch_expiry(lease: object) -> float | None:
    """Read both legacy numeric leases and trace-aware lease records."""
    if isinstance(lease, (int, float)):
        return float(lease)
    if isinstance(lease, dict):
        expiry = lease.get(_DISPATCH_EXPIRY_KEY)
        if isinstance(expiry, (int, float)):
            return float(expiry)
    return None


def _dispatch_trace_context(lease: object) -> dict[str, str] | None:
    """Return a validated serialized OTEL context from a dispatch lease."""
    if not isinstance(lease, dict):
        return None
    carrier = lease.get(_DISPATCH_TRACE_CONTEXT_KEY)
    if not isinstance(carrier, dict):
        return None
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in carrier.items()):
        return None
    return {
        key: value
        for key, value in carrier.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _new_acceptor_id(config: WorkerConfig) -> str:
    """Give each independently running Modal invocation its own claim identity."""
    return f"{config.acceptor_id}-{uuid.uuid4().hex[:12]}"


def _configure_logging(settings: WorkerSettings) -> None:
    """Apply the worker log level without changing the application's root logger."""
    logger.setLevel(settings.log_level.upper())
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False


def _release_safely(client: OutpostsClient, session_id: str, acceptor_id: str, reason: str) -> None:
    try:
        client.release(session_id, acceptor_id)
    except OutpostsAPIError as error:
        logger.warning("[%s] failed to release claim after %s: %s", session_id, reason, error)
    else:
        logger.info("[%s] released claim after %s", session_id, reason)


def _claim_deadline_epoch(deadline: str | None) -> float | None:
    if deadline is None:
        return None
    try:
        return float(deadline)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    except ValueError as error:
        raise ClaimDeadlineError(f"unsupported claim deadline {deadline!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _deadline_limited_timeout(
    maximum_seconds: int,
    *,
    deadline_epoch: float | None,
    reserve_seconds: int,
) -> int:
    if deadline_epoch is None:
        return maximum_seconds
    available = math.floor(deadline_epoch - time.time() - reserve_seconds)
    if available < 1:
        raise ClaimDeadlineError(
            "not enough time remains to connect before the server-assigned claim deadline"
        )
    return min(maximum_seconds, available)


def _new_sandbox(
    *,
    app: modal.App,
    image: modal.Image,
    settings: WorkerSettings,
    readiness_timeout_seconds: int,
    sandbox_options: Mapping[str, Any],
) -> modal.Sandbox:
    sandbox = modal.Sandbox.create(
        "sleep",
        "infinity",
        app=app,
        image=image,
        workdir=WORKDIR,
        timeout=_sandbox_lifetime_seconds(settings),
        readiness_probe=modal.sandbox.Probe.with_exec("test", "-d", WORKDIR),
        **sandbox_options,
    )
    try:
        sandbox.wait_until_ready(timeout=readiness_timeout_seconds)
    except BaseException:
        _terminate_failed_sandbox(sandbox)
        raise
    return sandbox


def _terminate_failed_sandbox(sandbox: modal.Sandbox) -> None:
    try:
        sandbox.terminate()
    except Exception:
        logger.debug("failed to terminate unusable Sandbox", exc_info=True)


def _create_sandbox(
    *,
    app: modal.App,
    base_image: modal.Image,
    session_id: str,
    snapshot_store: SnapshotStore,
    settings: WorkerSettings,
    readiness_timeout_seconds: int,
    sandbox_options: Mapping[str, Any],
) -> modal.Sandbox:
    snapshot_key = _snapshot_key(session_id)
    snapshot_id = snapshot_store.get(snapshot_key)
    if isinstance(snapshot_id, str):
        try:
            sandbox = _new_sandbox(
                app=app,
                image=modal.Image.from_id(snapshot_id),
                settings=settings,
                readiness_timeout_seconds=readiness_timeout_seconds,
                sandbox_options=sandbox_options,
            )
            logger.info("[%s] resumed from filesystem snapshot %s", session_id, snapshot_id)
            return sandbox
        except modal.exception.NotFoundError:
            snapshot_store.pop(snapshot_key, None)
            logger.warning(
                "[%s] snapshot %s expired; starting from the base image",
                session_id,
                snapshot_id,
            )

    return _new_sandbox(
        app=app,
        image=base_image,
        settings=settings,
        readiness_timeout_seconds=readiness_timeout_seconds,
        sandbox_options=sandbox_options,
    )


def _final_status(
    client: OutpostsClient,
    *,
    session_id: str,
    settings: WorkerSettings,
) -> tuple[bool, SessionStatus | None]:
    """Poll through API lag and return ``(known, status)`` for a safe final state."""
    last_status: SessionStatus | None = None
    received_status = False
    for attempt in range(1, settings.status_attempts + 1):
        try:
            status = client.session_status(session_id)
        except OutpostsAPIError as error:
            logger.warning(
                "[%s] status lookup %s/%s failed: %s",
                session_id,
                attempt,
                settings.status_attempts,
                error,
            )
        else:
            received_status = True
            last_status = status
            if status is None or status == SessionStatus.SUSPENDED or status in _TERMINAL_STATUSES:
                return True, status
            logger.info(
                "[%s] final status lookup %s/%s is still %s; waiting for propagation",
                session_id,
                attempt,
                settings.status_attempts,
                status,
            )
        if attempt < settings.status_attempts:
            time.sleep(settings.status_retry_delay_seconds * attempt)
    return received_status, last_status


def _snapshot(
    sandbox: modal.Sandbox,
    *,
    session_id: str,
    snapshot_store: SnapshotStore,
    settings: WorkerSettings,
) -> None:
    image = sandbox.snapshot_filesystem(
        timeout=settings.snapshot_timeout_seconds,
        ttl=settings.snapshot_ttl_seconds,
    )
    image_id = image.object_id
    if image_id is None:
        raise RuntimeError("Modal returned a filesystem snapshot without an object ID")
    snapshot_store.put(_snapshot_key(session_id), image_id)
    logger.info("[%s] stored filesystem snapshot %s", session_id, image_id)


def _watch_session_liveness(
    client: OutpostsClient,
    sandbox: modal.Sandbox,
    *,
    session_id: str,
    interval_seconds: float,
    stop: threading.Event,
    ended: threading.Event,
    attach_state: _AttachState,
    attach_timeout_seconds: float,
    attach_timed_out: threading.Event,
) -> None:
    """Kill the sandbox once the session ends without the remote noticing.

    The Outposts contract expects orchestrators to poll ``status.session_status``
    while the remote runs and terminate it themselves once the session reaches a
    terminal state or its queue entry disappears; otherwise a remote that missed
    the session-end notification idles until the function timeout.

    The same loop enforces the attach timeout: a remote whose gateway websocket
    connected but never received an RPC is unreachable from Devin's side, and only
    a fresh worker connection recovers it.
    """
    suspended_strikes = 0
    while not stop.wait(interval_seconds):
        if attach_state.timed_out(attach_timeout_seconds):
            logger.warning(
                "[%s] gateway connected but Devin has not attached within %.0f seconds; "
                "terminating the sandbox so a fresh worker reconnects",
                session_id,
                attach_timeout_seconds,
            )
            attach_timed_out.set()
            try:
                sandbox.terminate()
            except Exception:
                logger.exception("[%s] sandbox termination after attach timeout failed", session_id)
            return
        try:
            status = client.session_status(session_id)
        except (OutpostsAPIError, OutpostsProtocolError) as error:
            logger.warning("[%s] session liveness lookup failed: %s", session_id, error)
            continue
        if status == SessionStatus.SUSPENDED:
            suspended_strikes += 1
            if suspended_strikes < _SUSPENDED_LIVENESS_STRIKES:
                continue
        elif status is not None and status not in _TERMINAL_STATUSES:
            suspended_strikes = 0
            continue
        logger.warning(
            "[%s] session is %s while the worker is still running; terminating the sandbox",
            session_id,
            "gone from the queue" if status is None else status,
        )
        ended.set()
        try:
            sandbox.terminate()
        except Exception:
            logger.exception(
                "[%s] sandbox termination after external session end failed", session_id
            )
        return


def _run_claimed_session(
    *,
    app: modal.App,
    image: modal.Image,
    config: WorkerConfig,
    settings: WorkerSettings,
    client: OutpostsClient,
    snapshot_store: SnapshotStore,
    session_id: str,
    acceptor_id: str,
    claim_deadline: str | None,
    connect_token: str | None,
    gateway_url: str | None,
    remote_binary_sha: str | None,
    sandbox_options: Mapping[str, Any],
) -> None:
    sandbox: modal.Sandbox | None = None
    primary_error: BaseException | None = None
    deadline_epoch = _claim_deadline_epoch(claim_deadline)
    try:
        with observability.span(
            "modal-devin sandbox start",
            config=config,
            session_id=session_id,
            attributes={"modal_devin.acceptor.id": acceptor_id},
        ):
            sandbox_ready_timeout = _deadline_limited_timeout(
                settings.sandbox_ready_timeout_seconds,
                deadline_epoch=deadline_epoch,
                reserve_seconds=settings.claim_connect_margin_seconds,
            )
            sandbox = _create_sandbox(
                app=app,
                base_image=image,
                session_id=session_id,
                snapshot_store=snapshot_store,
                settings=settings,
                readiness_timeout_seconds=sandbox_ready_timeout,
                sandbox_options=sandbox_options,
            )
            _deadline_limited_timeout(
                1,
                deadline_epoch=deadline_epoch,
                reserve_seconds=settings.claim_connect_margin_seconds,
            )

        # Direct-serve mode: the CLI cannot parse this API's claim responses (fresh or
        # redundant, verified live 2026-07-19), so the documented claim-mode hand-off
        # is unusable and the orchestrator's own claim is handed over via the
        # session-scoped connect token instead.
        exec_env: dict[str, str | None] = {
            "DEVIN_API_URL": config.api_url,
            "DEVIN_OUTPOSTS_TOKEN": _PLACEHOLDER_TOKEN,
        }
        if connect_token is not None:
            exec_env["DEVIN_REMOTE_SESSION_TOKEN"] = connect_token
            if gateway_url is not None:
                exec_env["DEVIN_OUTPOST_GATEWAY_URL"] = gateway_url
        if remote_binary_sha is not None:
            # In direct-serve mode the CLI never reads the queue entry, so it must be
            # told about spec.remote_binary_sha or it boots the latest published
            # remote instead of the session's pin.
            exec_env["DEVIN_WORKER_REMOTE_SHA"] = remote_binary_sha

        with observability.span(
            "modal-devin worker process",
            config=config,
            session_id=session_id,
            attributes={"modal_devin.acceptor.id": acceptor_id},
        ):
            process = sandbox.exec(
                _DEVIN_BIN,
                "worker",
                "start",
                "--session",
                session_id,
                "--outpost",
                config.outpost_id,
                "--acceptor-id",
                acceptor_id,
                env=exec_env,
                stderr=StreamType.STDOUT,
                timeout=settings.session_timeout_seconds,
            )
            watchdog_stop = threading.Event()
            session_ended_externally = threading.Event()
            attach_state = _AttachState()
            attach_timed_out = threading.Event()
            watchdog = threading.Thread(
                target=_watch_session_liveness,
                args=(client, sandbox),
                kwargs={
                    "session_id": session_id,
                    "interval_seconds": settings.status_watchdog_interval_seconds,
                    "stop": watchdog_stop,
                    "ended": session_ended_externally,
                    "attach_state": attach_state,
                    "attach_timeout_seconds": settings.session_attach_timeout_seconds,
                    "attach_timed_out": attach_timed_out,
                },
                name=f"session-liveness-{session_id}",
                daemon=True,
            )
            watchdog.start()
            try:
                for line in process.stdout:
                    attach_state.observe(line)
                    logger.info("[%s] %s", session_id, line.rstrip())
                returncode = process.wait()
            except Exception:
                if not (session_ended_externally.is_set() or attach_timed_out.is_set()):
                    raise
                returncode = None
            finally:
                watchdog_stop.set()
            logger.info("[%s] Devin worker exited with status %s", session_id, returncode)

        if attach_timed_out.is_set():
            raise SessionAttachTimeoutError(session_id, settings.session_attach_timeout_seconds)

        if session_ended_externally.is_set():
            logger.info(
                "[%s] session already ended on the Devin side; skipping finalization",
                session_id,
            )
            return

        with observability.span(
            "modal-devin session finalize",
            config=config,
            session_id=session_id,
            attributes={"process.exit_code": returncode},
        ):
            known, status = _final_status(
                client,
                session_id=session_id,
                settings=settings,
            )
        safe_terminal = status is None or status in _TERMINAL_STATUSES
        if not known or (status != SessionStatus.SUSPENDED and not safe_terminal):
            logger.warning(
                "[%s] final session status is unsafe (%s); preserving a recovery snapshot",
                session_id,
                status if known else "unavailable",
            )
            _snapshot(
                sandbox,
                session_id=session_id,
                snapshot_store=snapshot_store,
                settings=settings,
            )
            raise SessionStatusUnknownError(
                f"could not establish a safe final state for session {session_id!r}"
            )

        if status == SessionStatus.SUSPENDED:
            _snapshot(
                sandbox,
                session_id=session_id,
                snapshot_store=snapshot_store,
                settings=settings,
            )
        else:
            snapshot_store.pop(_snapshot_key(session_id), None)

        if returncode != 0:
            raise WorkerExitedError(session_id, returncode)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if sandbox is not None:
            try:
                sandbox.terminate()
            except Exception:
                if primary_error is None:
                    raise
                logger.exception("[%s] Sandbox termination also failed", session_id)


def execute_session(
    *,
    app: modal.App,
    image: modal.Image,
    config: WorkerConfig,
    settings: WorkerSettings,
    session_id: str,
    token: str,
    sandbox_options: Mapping[str, Any],
) -> None:
    """Run one dispatched session with isolated claim ownership."""
    _configure_logging(settings)
    observability.configure_observability(config)
    snapshot_store = modal.Dict.from_name(
        config.snapshot_store_name,
        create_if_missing=True,
    )
    dispatch_lease = snapshot_store.pop(_dispatch_key(session_id), None)
    trace_context = _dispatch_trace_context(dispatch_lease)
    with (
        observability.attach_context(trace_context),
        observability.span(
            "modal-devin session lifecycle",
            config=config,
            session_id=session_id,
            attributes={"modal.function.name": "session"},
        ),
    ):
        _execute_session(
            app=app,
            image=image,
            config=config,
            settings=settings,
            session_id=session_id,
            token=token,
            sandbox_options=sandbox_options,
            snapshot_store=snapshot_store,
        )


def _execute_session(
    *,
    app: modal.App,
    image: modal.Image,
    config: WorkerConfig,
    settings: WorkerSettings,
    session_id: str,
    token: str,
    sandbox_options: Mapping[str, Any],
    snapshot_store: SnapshotStore,
) -> None:
    """Execute a session inside its attached scheduler trace context."""
    client = OutpostsClient(
        config.api_url,
        token,
        timeout=settings.api_timeout_seconds,
    )
    acceptor_id = _new_acceptor_id(config)
    claimed = False
    lifecycle_error: BaseException | None = None
    try:
        try:
            with observability.span(
                "modal-devin session claim",
                config=config,
                session_id=session_id,
                attributes={"modal_devin.acceptor.id": acceptor_id},
            ):
                claim = client.claim(session_id, acceptor_id)
        except ClaimConflict:
            logger.info("[%s] claim was acquired by another invocation", session_id)
            return
        claimed = True
        logger.info(
            "[%s] claimed as %s with deadline %s; starting worker lifecycle",
            session_id,
            acceptor_id,
            claim.deadline,
        )
        remote_binary_sha = claim.remote_binary_sha
        if remote_binary_sha is None:
            try:
                remote_binary_sha = client.remote_binary_sha(session_id)
            except OutpostsAPIError as error:
                logger.warning(
                    "[%s] could not read the pinned remote binary SHA; "
                    "the worker will boot the latest published remote: %s",
                    session_id,
                    error,
                )
        _run_claimed_session(
            app=app,
            image=image,
            config=config,
            settings=settings,
            client=client,
            snapshot_store=snapshot_store,
            session_id=session_id,
            acceptor_id=acceptor_id,
            claim_deadline=claim.deadline,
            connect_token=claim.connect_token,
            gateway_url=claim.gateway_url,
            remote_binary_sha=remote_binary_sha,
            sandbox_options=sandbox_options,
        )
    except BaseException as error:
        lifecycle_error = error
        logger.exception("[%s] worker lifecycle failed", session_id)
        raise
    finally:
        if claimed:
            with observability.span(
                "modal-devin session release",
                config=config,
                session_id=session_id,
                attributes={"modal_devin.acceptor.id": acceptor_id},
            ):
                _release_safely(
                    client,
                    session_id,
                    acceptor_id,
                    "worker failure" if lifecycle_error is not None else "confirmed session end",
                )


def dispatch_pending_sessions(
    *,
    config: WorkerConfig,
    settings: WorkerSettings,
    spawn_session: Callable[[str], object],
    token: str,
) -> None:
    """Discover pending sessions and dispatch one Modal function call per session."""
    _configure_logging(settings)
    observability.configure_observability(config)
    with observability.span(
        "modal-devin scheduler poll",
        config=config,
        attributes={"modal.function.name": "scheduler"},
    ):
        _dispatch_pending_sessions(
            config=config,
            settings=settings,
            spawn_session=spawn_session,
            token=token,
        )


def _dispatch_pending_sessions(
    *,
    config: WorkerConfig,
    settings: WorkerSettings,
    spawn_session: Callable[[str], object],
    token: str,
) -> None:
    """Perform one scheduler poll inside its root tracing span."""
    snapshot_store = modal.Dict.from_name(
        config.snapshot_store_name,
        create_if_missing=True,
    )
    _refresh_snapshot_index(snapshot_store)
    client = OutpostsClient(
        config.api_url,
        token,
        timeout=settings.api_timeout_seconds,
    )
    try:
        with observability.span("modal-devin list pending sessions", config=config):
            pending = client.pending_sessions(config.outpost_id)
    except OutpostsProtocolError:
        logger.exception("Outposts scheduler received an invalid response")
        raise
    except OutpostsAPIError:
        logger.exception("Outposts scheduler request failed")
        raise
    if not pending:
        return

    dispatch_failures: list[Exception] = []
    for session in pending:
        session_id = session.session_id
        if session.session_status in _UNSERVABLE_STATUSES:
            logger.info(
                "[%s] skipping dispatch; session is %s and not awaiting a worker",
                session_id,
                session.session_status,
            )
            continue
        with observability.span(
            "modal-devin session dispatch",
            config=config,
            session_id=session_id,
        ):
            trace_context = observability.get_context()
            if not _reserve_dispatch(snapshot_store, session_id, settings, trace_context):
                logger.info("[%s] an invocation is already queued", session_id)
                continue
            try:
                spawn_session(session_id)
            except Exception as error:
                snapshot_store.pop(_dispatch_key(session_id), None)
                logger.exception("[%s] dispatch failed", session_id)
                error.add_note(f"while dispatching session {session_id!r}")
                dispatch_failures.append(error)

    if dispatch_failures:
        raise ExceptionGroup("one or more session dispatches failed", dispatch_failures)
