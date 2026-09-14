"""Validated runtime settings for database infrastructure."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigurationError(ValueError):
    """Raised when an infrastructure setting is unsafe or malformed."""


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class DatabaseSettings:
    pool_min_size: int
    pool_max_size: int
    pool_timeout_seconds: int
    connect_timeout_seconds: int
    statement_timeout_ms: int
    idle_transaction_timeout_ms: int

    @classmethod
    def from_environment(cls) -> "DatabaseSettings":
        minimum = _positive_int("DB_POOL_MIN_SIZE", 1)
        maximum = _positive_int("DB_POOL_MAX_SIZE", 5)
        if minimum > maximum:
            raise ConfigurationError("DB_POOL_MIN_SIZE must not exceed DB_POOL_MAX_SIZE")
        return cls(
            pool_min_size=minimum,
            pool_max_size=maximum,
            pool_timeout_seconds=_positive_int("DB_POOL_TIMEOUT", 10),
            connect_timeout_seconds=_positive_int("DB_CONNECT_TIMEOUT", 5),
            statement_timeout_ms=_positive_int("DB_STATEMENT_TIMEOUT", 15000),
            idle_transaction_timeout_ms=_positive_int("DB_IDLE_TRANSACTION_TIMEOUT", 30000),
        )
