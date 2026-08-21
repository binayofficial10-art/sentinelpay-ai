"""Shared rate limiting and privacy-minimised security audit helpers."""

import hashlib
import hmac
import os
from typing import Any

from backend.database import (
    DatabaseUnavailableError,
    consume_rate_limit,
    get_audit_events_for_user,
    write_audit_event,
)


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: int):
        super().__init__("Rate limit exceeded")
        self.retry_after_seconds = retry_after_seconds


class SecurityConfigurationError(RuntimeError):
    """Raised when a production-safe security configuration is missing."""


def configured_positive_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        parsed = int(value)
    except ValueError as error:
        raise SecurityConfigurationError(f"{name} must be a positive integer") from error
    if parsed <= 0:
        raise SecurityConfigurationError(f"{name} must be a positive integer")
    return parsed


def rate_limit_settings(scope: str) -> tuple[int, int]:
    defaults = {
        "register": (5, 60),
        "login": (10, 60),
        "transaction_check": (30, 60),
        "authenticated": (120, 60),
    }
    default_limit, default_window = defaults[scope]
    env_prefix = f"RATE_LIMIT_{scope.upper()}"
    return (
        configured_positive_int(f"{env_prefix}_MAX_REQUESTS", default_limit),
        configured_positive_int(f"{env_prefix}_WINDOW_SECONDS", default_window),
    )


def source_fingerprint(source: str | None) -> str:
    """Return a non-reversible source identifier without retaining raw IP data."""
    source = source or "unknown"
    secret = os.getenv("SECURITY_HASH_SECRET", "")
    if secret:
        return hmac.new(secret.encode("utf-8"), source.encode("utf-8"), hashlib.sha256).hexdigest()
    if os.getenv("VERCEL") or os.getenv("VERCEL_ENV"):
        raise SecurityConfigurationError("SECURITY_HASH_SECRET is required in production")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def enforce_rate_limit(scope: str, subject: str) -> None:
    maximum, window_seconds = rate_limit_settings(scope)
    request_count, retry_after_seconds = consume_rate_limit(
        scope=scope,
        subject_key=subject,
        window_seconds=window_seconds,
    )
    if request_count > maximum:
        raise RateLimitExceeded(retry_after_seconds)


def audit_event(
    event_type: str,
    *,
    success: bool,
    user_id: int | None = None,
    source_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit persistence; audit outages never expose internals to clients."""
    try:
        write_audit_event(
            event_type=event_type,
            success=success,
            user_id=user_id,
            source_hash=source_hash,
            metadata=metadata or {},
        )
    except DatabaseUnavailableError:
        # The request path logs a generic server-side message where useful.  Audit
        # loss must not turn an otherwise successful user operation into a 500.
        return


def own_audit_events(user_id: int) -> list[dict[str, Any]]:
    return get_audit_events_for_user(user_id)
