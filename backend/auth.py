"""Server-side authentication helpers using hashed passwords and opaque sessions."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.database import (
    DatabaseUnavailableError,
    ensure_schema,
    get_connection,
    persistence_enabled,
    using_sqlite,
)

PASSWORD_ITERATIONS = 600_000
SESSION_LIFETIME = timedelta(days=7)
DUMMY_PASSWORD_HASH = "pbkdf2_sha256$600000$00000000000000000000000000000000$0000000000000000000000000000000000000000000000000000000000000000"


class AuthenticationError(RuntimeError):
    """Raised for an invalid or expired authentication session."""


class DuplicateUserError(RuntimeError):
    """Raised when an email address is already registered."""


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        ).hex()
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(candidate, digest_hex)


def create_user(email: str, password: str, *, role: str = "viewer") -> dict[str, Any]:
    if not persistence_enabled():
        raise DatabaseUnavailableError("Authentication storage is unavailable")
    if role not in {"viewer", "analyst", "admin"}:
        raise ValueError("Invalid application role")
    placeholder = "?" if using_sqlite() else "%s"
    try:
        with get_connection() as connection:
            ensure_schema(connection)
            existing_user = connection.execute(
                f"SELECT id FROM users WHERE email = {placeholder}", (normalize_email(email),)
            ).fetchone()
            if existing_user:
                raise DuplicateUserError("Email is already registered")
            row = connection.execute(
                f"INSERT INTO users (email, password_hash, role) VALUES ({placeholder}, {placeholder}, {placeholder}) "
                "RETURNING id, email, role, created_at",
                (normalize_email(email), hash_password(password), role),
            ).fetchone()
            return dict(row)
    except DuplicateUserError:
        raise
    except Exception as error:
        if isinstance(error, DatabaseUnavailableError):
            raise
        raise DatabaseUnavailableError("Authentication storage is unavailable") from error


def authenticate_user(email: str, password: str) -> dict[str, Any] | None:
    if not persistence_enabled():
        raise DatabaseUnavailableError("Authentication storage is unavailable")
    placeholder = "?" if using_sqlite() else "%s"
    with get_connection() as connection:
        ensure_schema(connection)
        row = connection.execute(
            f"SELECT id, email, password_hash, role, created_at FROM users WHERE email = {placeholder}",
            (normalize_email(email),),
        ).fetchone()
    user = dict(row) if row else None
    if not user:
        verify_password(password, DUMMY_PASSWORD_HASH)
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return {key: user[key] for key in ("id", "email", "role", "created_at")}


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int) -> tuple[str, datetime]:
    if not persistence_enabled():
        raise DatabaseUnavailableError("Authentication storage is unavailable")
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + SESSION_LIFETIME
    placeholder = "?" if using_sqlite() else "%s"
    value = expires_at.isoformat() if placeholder == "?" else expires_at
    with get_connection() as connection:
        ensure_schema(connection)
        connection.execute(
            f"INSERT INTO sessions (user_id, token_hash, expires_at) VALUES ({placeholder}, {placeholder}, {placeholder})",
            (user_id, token_digest(token), value),
        )
    return token, expires_at


def get_user_for_session(token: str | None) -> dict[str, Any]:
    if not token or not persistence_enabled():
        raise AuthenticationError("Authentication required")
    placeholder = "?" if using_sqlite() else "%s"
    with get_connection() as connection:
        ensure_schema(connection)
        row = connection.execute(
            "SELECT users.id, users.email, users.role, users.created_at, sessions.expires_at "
            "FROM sessions JOIN users ON users.id = sessions.user_id "
            f"WHERE sessions.token_hash = {placeholder}",
            (token_digest(token),),
        ).fetchone()
    if not row:
        raise AuthenticationError("Authentication required")
    user = dict(row)
    expires_at = user.pop("expires_at")
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    if expires_at <= datetime.now(timezone.utc):
        delete_session(token)
        raise AuthenticationError("Authentication required")
    return user


def delete_session(token: str | None) -> None:
    if not token or not persistence_enabled():
        return
    placeholder = "?" if using_sqlite() else "%s"
    with get_connection() as connection:
        ensure_schema(connection)
        connection.execute(f"DELETE FROM sessions WHERE token_hash = {placeholder}", (token_digest(token),))
