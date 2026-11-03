"""Validated configuration and Modal resource naming."""

from __future__ import annotations

import hashlib
import math
import os
import re
import urllib.parse
from dataclasses import dataclass, fields
from typing import Any, Self, cast

from modal_devin._exceptions import ConfigurationError

DEFAULT_API_URL = "https://api.devin.ai"
DEFAULT_SCHEDULER_INTERVAL_SECONDS = 3
DEFAULT_SESSION_TIMEOUT_SECONDS = 12 * 60 * 60
DEFAULT_API_TIMEOUT_SECONDS = 30.0
DEFAULT_SNAPSHOT_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_STATUS_ATTEMPTS = 3
DEFAULT_STATUS_RETRY_DELAY_SECONDS = 1.0
# Observed attach latency is bimodal: a healthy remote attaches within
# milliseconds of the gateway connecting, or it never attaches at all. There is
# no legitimate slow-but-eventually-successful case to protect against, so both
# values stay just long enough to absorb jitter, not to wait out a real delay.
DEFAULT_STATUS_WATCHDOG_INTERVAL_SECONDS = 10.0
DEFAULT_SESSION_ATTACH_TIMEOUT_SECONDS = 20.0
DEFAULT_SANDBOX_READY_TIMEOUT_SECONDS = 120
DEFAULT_SNAPSHOT_TIMEOUT_SECONDS = 120
DEFAULT_CLAIM_CONNECT_MARGIN_SECONDS = 15

_TERMINATION_MARGIN_SECONDS = 30

_RESOURCE_COMPONENT_RE = re.compile(r"[^a-z0-9._-]+")
_ENV_PREFIX = "MODAL_DEVIN_"


def _resource_slug(value: str, *, max_length: int = 32) -> str:
    """Return a readable, bounded Modal resource-name component."""
    lowercase = value.strip().lower()
    normalized = _RESOURCE_COMPONENT_RE.sub("-", lowercase).strip("-._")
    if not normalized:
        normalized = "worker"
    if normalized == lowercase and len(normalized) <= max_length:
        return normalized
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    prefix = normalized[: max_length - len(digest) - 1].rstrip("-._") or "worker"
    return f"{prefix}-{digest}"


def _validate_api_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError("api_url must be an absolute http:// or https:// URL")
    if parsed.username or parsed.password:
        raise ConfigurationError("api_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("api_url must not contain a query string or fragment")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Operational settings for a :class:`modal_devin.Worker`.

    Use :meth:`from_env` to read ``MODAL_DEVIN_*`` overrides. Keeping this separate
    from the worker identity makes configuration explicit and straightforward to test.
    """

    scheduler_interval_seconds: int = DEFAULT_SCHEDULER_INTERVAL_SECONDS
    session_timeout_seconds: int = DEFAULT_SESSION_TIMEOUT_SECONDS
    api_timeout_seconds: float = DEFAULT_API_TIMEOUT_SECONDS
    snapshot_ttl_seconds: int | None = DEFAULT_SNAPSHOT_TTL_SECONDS
    status_attempts: int = DEFAULT_STATUS_ATTEMPTS
    status_retry_delay_seconds: float = DEFAULT_STATUS_RETRY_DELAY_SECONDS
    status_watchdog_interval_seconds: float = DEFAULT_STATUS_WATCHDOG_INTERVAL_SECONDS
    session_attach_timeout_seconds: float = DEFAULT_SESSION_ATTACH_TIMEOUT_SECONDS
    sandbox_ready_timeout_seconds: int = DEFAULT_SANDBOX_READY_TIMEOUT_SECONDS
    snapshot_timeout_seconds: int = DEFAULT_SNAPSHOT_TIMEOUT_SECONDS
    claim_connect_margin_seconds: int = DEFAULT_CLAIM_CONNECT_MARGIN_SECONDS
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        positive = (
            "scheduler_interval_seconds",
            "session_timeout_seconds",
            "api_timeout_seconds",
            "status_attempts",
            "status_watchdog_interval_seconds",
            "session_attach_timeout_seconds",
            "sandbox_ready_timeout_seconds",
            "snapshot_timeout_seconds",
            "claim_connect_margin_seconds",
        )
        for name in positive:
            if getattr(self, name) <= 0:
                raise ConfigurationError(f"{name} must be greater than zero")
        if self.snapshot_ttl_seconds is not None and self.snapshot_ttl_seconds <= 0:
            raise ConfigurationError("snapshot_ttl_seconds must be positive or None")
        if self.status_retry_delay_seconds < 0:
            raise ConfigurationError("status_retry_delay_seconds must not be negative")
        valid_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if self.log_level.upper() not in valid_levels:
            raise ConfigurationError(
                "log_level must be one of CRITICAL, ERROR, WARNING, INFO, or DEBUG"
            )

    @classmethod
    def from_env(cls) -> Self:
        """Build settings from ``MODAL_DEVIN_*`` environment variables.

        Set ``MODAL_DEVIN_SNAPSHOT_TTL_SECONDS=none`` to retain snapshots indefinitely.
        """
        values: dict[str, object] = {}
        integer_fields = {
            "scheduler_interval_seconds",
            "session_timeout_seconds",
            "status_attempts",
            "sandbox_ready_timeout_seconds",
            "snapshot_timeout_seconds",
            "claim_connect_margin_seconds",
        }
        float_fields = {
            "api_timeout_seconds",
            "status_retry_delay_seconds",
            "status_watchdog_interval_seconds",
            "session_attach_timeout_seconds",
        }
        for field in fields(cls):
            raw = os.environ.get(_ENV_PREFIX + field.name.upper())
            if raw is None:
                continue
            try:
                if field.name in integer_fields:
                    values[field.name] = int(raw)
                elif field.name in float_fields:
                    values[field.name] = float(raw)
                elif field.name == "snapshot_ttl_seconds":
                    values[field.name] = None if raw.lower() in {"none", "null"} else int(raw)
                else:
                    values[field.name] = raw
            except ValueError as error:
                env_name = _ENV_PREFIX + field.name.upper()
                raise ConfigurationError(f"invalid value for {env_name}: {raw!r}") from error
        return cls(**cast(dict[str, Any], values))


def _status_budget_seconds(settings: WorkerSettings) -> float:
    request_budget = settings.status_attempts * settings.api_timeout_seconds
    retry_delay_budget = settings.status_retry_delay_seconds * sum(
        range(1, settings.status_attempts)
    )
    return request_budget + retry_delay_budget


def _sandbox_lifetime_seconds(settings: WorkerSettings) -> int:
    """Maximum Sandbox lifetime including work, startup, and recovery."""
    return math.ceil(
        settings.session_timeout_seconds
        + settings.sandbox_ready_timeout_seconds
        + _status_budget_seconds(settings)
        + settings.snapshot_timeout_seconds
        + _TERMINATION_MARGIN_SECONDS
    )


def _session_function_timeout_seconds(settings: WorkerSettings) -> int:
    """Minimum outer Function timeout around one complete Sandbox lifecycle."""
    claim_and_release_budget = 2 * settings.api_timeout_seconds
    return math.ceil(_sandbox_lifetime_seconds(settings) + claim_and_release_budget)


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Identity and validated runtime configuration for one worker deployment."""

    name: str
    outpost_id: str
    api_url: str = DEFAULT_API_URL

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ConfigurationError("name must not be empty")
        if not self.outpost_id.strip():
            raise ConfigurationError("outpost_id must not be empty")
        object.__setattr__(self, "api_url", _validate_api_url(self.api_url))

    @property
    def resource_slug(self) -> str:
        return _resource_slug(self.name)

    @property
    def acceptor_id(self) -> str:
        return f"modal-{self.resource_slug}"

    @property
    def app_name(self) -> str:
        return f"modal-devin-{self.resource_slug}"

    @property
    def snapshot_store_name(self) -> str:
        return f"modal-devin-{self.resource_slug}-snapshots"
