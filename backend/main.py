import json
import hashlib
import logging
import math
import os
import random
import re
import socket
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import Depends
from fastapi import HTTPException, Header
from fastapi import Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Local-only .env loading does not override Vercel environment variables and
# must run before the database module reads DATABASE_URL.
load_dotenv(Path(__file__).resolve().parent / ".env")

from backend.database import (
    DatabaseUnavailableError,
    ActionResourceNotFoundError,
    get_recent_transactions,
    get_transaction,
    get_transaction_by_idempotency_key,
    save_transaction,
    create_alert,
    get_alerts_for_user,
    update_alert_status,
    set_review_decision_idempotent,
    update_alert_status_idempotent,
    persistence_enabled,
)
from backend.auth import (
    AuthenticationError,
    DuplicateUserError,
    authenticate_user,
    create_session,
    create_user,
    delete_session,
    get_user_for_session,
)
from backend.security import (
    RateLimitExceeded,
    SecurityConfigurationError,
    audit_event,
    enforce_rate_limit,
    own_audit_events,
    source_fingerprint,
)


logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-3.7-flash"
GEMINI_API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
GEMINI_MAX_RETRIES = 2
GEMINI_RETRY_BASE_DELAY_SECONDS = 0.25
GEMINI_RETRY_JITTER_SECONDS = 0.1
GEMINI_REQUEST_TIMEOUT_SECONDS = 3
GEMINI_MAX_RETRY_AFTER_SECONDS = 1
SESSION_COOKIE_NAME = "sentinelpay_session"
MONEY_SCALE = Decimal("0.01")
MAX_AMOUNT = Decimal("999999999999.99")


def get_cors_allowed_origins() -> list[str]:
    """Read an explicit comma-separated CORS allowlist from the environment."""
    origins = [
        origin.strip().rstrip("/")
        for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]
    if "*" in origins:
        raise ValueError("CORS_ALLOWED_ORIGINS must contain explicit origins, never '*'.")
    if (os.getenv("VERCEL") or os.getenv("VERCEL_ENV")) and any(
        not origin.startswith("https://") for origin in origins
    ):
        raise ValueError("Production CORS_ALLOWED_ORIGINS entries must use HTTPS.")
    return origins


def get_frontend_api_base_url() -> str:
    """Return the optional configured API origin after strict URL validation."""
    value = os.getenv("FRONTEND_API_BASE_URL", "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("FRONTEND_API_BASE_URL must be an HTTP(S) origin without a path.")
    if (os.getenv("VERCEL") or os.getenv("VERCEL_ENV")) and parsed.scheme != "https":
        raise ValueError("FRONTEND_API_BASE_URL must use HTTPS in production.")
    return value


app = FastAPI(title="SentinelPay AI")
cors_allowed_origins = get_cors_allowed_origins()
if cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )


@app.middleware("http")
async def add_security_headers(request: FastAPIRequest, call_next: Any) -> Response:
    """Attach baseline browser protections without changing API response bodies."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    api_origin = get_frontend_api_base_url()
    connect_sources = "'self'" + (f" {api_origin}" if api_origin else "")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'; img-src 'self' data:; "
        f"script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src {connect_sources}",
    )
    if os.getenv("VERCEL") or os.getenv("VERCEL_ENV"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/frontend", StaticFiles(directory=frontend_dir, html=True), name="frontend")


class GeminiRequestError(Exception):
    """Gemini REST error with safe diagnostic details for server-side logs."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str = "",
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.retry_after_seconds = retry_after_seconds


def get_gemini_configuration() -> tuple[str | None, str]:
    """Read Gemini configuration only from the process environment.

    generateContent expects a bare model ID because the URL template adds the
    ``models/`` path segment itself. Reject copied assignment/path prefixes
    rather than making a misleading request to Gemini.
    """
    key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    if model.startswith("GEMINI_MODEL=") or model.startswith("models/") or "/" in model:
        raise GeminiRequestError(
            "GEMINI_MODEL must be a bare model name (for example, gemini-3.7-flash)"
        )
    return key, model


def is_transient_gemini_error(error: Exception) -> bool:
    """Return whether a bounded retry is appropriate for a Gemini REST request."""
    if isinstance(error, GeminiRequestError):
        return error.status_code in GEMINI_RETRYABLE_STATUS_CODES
    return isinstance(error, (URLError, TimeoutError, socket.timeout))


def gemini_retry_delay(attempt: int, error: Exception) -> float:
    """Use capped retries with exponential backoff and a small random jitter."""
    retry_after = getattr(error, "retry_after_seconds", None)
    if retry_after is not None:
        return min(max(retry_after, 0), GEMINI_MAX_RETRY_AFTER_SECONDS)
    return (GEMINI_RETRY_BASE_DELAY_SECONDS * (2 ** attempt)) + random.uniform(
        0, GEMINI_RETRY_JITTER_SECONDS
    )


def parse_retry_after(value: str | None) -> float | None:
    """Parse HTTP Retry-After seconds or date without extending request time."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())


class Transaction(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    amount: Decimal = Field(gt=0, le=MAX_AMOUNT, max_digits=14, decimal_places=2)
    sender: str = Field(min_length=1, max_length=255)
    receiver: str = Field(min_length=1, max_length=255)
    location: str = Field(min_length=1, max_length=255)
    device: str = Field(min_length=1, max_length=64)
    velocity: int = Field(ge=0)
    merchant: str | None = Field(default=None, min_length=1, max_length=255)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    transaction_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: Any) -> Decimal:
        """Accept JSON numbers/strings but reject precision that cannot be stored exactly."""
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as error:
            raise ValueError("Amount must be a valid decimal value.") from error
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Amount must be greater than zero.")
        if amount.as_tuple().exponent < -2:
            raise ValueError("Amount cannot have more than two decimal places.")
        return amount.quantize(MONEY_SCALE)

    @property
    def amount_minor(self) -> int:
        return int(self.amount * 100)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern=r"^(APPROVE|BLOCK)$")


class AlertStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern=r"^(ACKNOWLEDGED|RESOLVED)$")


def deterministic_signals(transaction: Any) -> list[dict[str, Any]]:
    """Return the observable inputs used by Sentinel's deterministic rules."""
    value = (lambda key: getattr(transaction, key)) if not isinstance(transaction, dict) else transaction.get
    amount_minor = int(value("amount_minor")) if isinstance(transaction, dict) and transaction.get("amount_minor") is not None else int(Decimal(str(value("amount"))) * 100)
    amount = format_money_minor(amount_minor)
    velocity, device = int(value("velocity")), str(value("device")).lower()
    amount_points = 30 if amount_minor >= 5_000_000 else 20 if amount_minor >= 2_500_000 else 0
    velocity_points = 30 if velocity >= 10 else 15 if velocity >= 5 else 0
    device_points = 0 if device == "trusted" else 20
    return [
        {"signal": "Transaction amount", "observed_value": amount, "risk_contribution": amount_points, "reason": "Amount meets a deterministic threshold." if amount_points else "Amount is below deterministic thresholds."},
        {"signal": "Transaction velocity", "observed_value": velocity, "risk_contribution": velocity_points, "reason": "Velocity meets a deterministic threshold." if velocity_points else "Velocity is below deterministic thresholds."},
        {"signal": "Device trust", "observed_value": device, "risk_contribution": device_points, "reason": "Device is not marked trusted." if device_points else "Device is marked trusted."},
    ]


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=12, max_length=256)


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {"id": user["id"], "email": user["email"], "role": user["role"], "created_at": user["created_at"]}


def idempotency_hash(transaction: Transaction) -> str:
    data = transaction_payload(transaction)
    data.pop("transaction_id", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def format_money_minor(amount_minor: int) -> str:
    """The only money representation emitted by the JSON API: a fixed-scale string."""
    return format(Decimal(amount_minor).scaleb(-2), ".2f")


def transaction_payload(transaction: Transaction) -> dict[str, Any]:
    """Create a JSON-safe transaction payload without leaking Decimal instances."""
    payload = transaction.model_dump(mode="json")
    payload["amount"] = format_money_minor(transaction.amount_minor)
    return payload


def action_idempotency_hash(*, action: str, resource_id: int, payload: dict[str, Any]) -> str:
    encoded = json.dumps({"action": action, "resource_id": resource_id, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stored_result(transaction: dict[str, Any]) -> dict[str, Any]:
    payload = {key: transaction[key] for key in ("amount", "sender", "receiver", "location", "device", "velocity", "merchant", "currency")}
    payload["transaction_id"] = transaction.get("idempotency_key")
    timestamp = transaction["transaction_timestamp"]
    return {"transaction": payload, "risk_score": transaction["risk_score"], "risk_level": transaction["risk_level"], "decision": transaction["decision"], "explanation": transaction["explanation"], "analysis_source": transaction["analysis_source"], "provider": transaction["provider"], "analysis_mode": "AI" if transaction["analysis_source"] == "gemini" else "FALLBACK", "confidence": None, "processing_time_ms": transaction.get("processing_time_ms"), "signals": deterministic_signals(transaction), "timeline": [{"stage": "Transaction received", "timestamp": timestamp}, {"stage": "Decision: " + transaction["decision"], "timestamp": transaction["created_at"]}]}


def request_source_fingerprint(request: FastAPIRequest) -> str:
    """Use Vercel's forwarded client address only in its trusted deployment path."""
    if os.getenv("VERCEL") or os.getenv("VERCEL_ENV"):
        source = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    else:
        source = request.client.host if request.client else "unknown"
    return source_fingerprint(source)


def request_metadata(request: FastAPIRequest) -> dict[str, str]:
    return {"method": request.method, "path": request.url.path}


def enforce_request_rate_limit(
    request: FastAPIRequest, *, scope: str, user_id: int | None = None
) -> str:
    try:
        source_hash = request_source_fingerprint(request)
        subject = f"user:{user_id}" if user_id is not None else f"source:{source_hash}"
        enforce_rate_limit(scope, subject)
        return source_hash
    except RateLimitExceeded as error:
        source_hash = request_source_fingerprint(request)
        audit_event(
            "rate_limit_violation",
            success=False,
            user_id=user_id,
            source_hash=source_hash,
            metadata={**request_metadata(request), "scope": scope},
        )
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from error
    except (DatabaseUnavailableError, SecurityConfigurationError) as error:
        logger.error("Security rate-limit storage is unavailable: %s", type(error).__name__)
        raise HTTPException(status_code=503, detail="Security controls are temporarily unavailable.") from error


def require_authenticated_user(request: FastAPIRequest) -> dict[str, Any]:
    try:
        return get_user_for_session(request.cookies.get(SESSION_COOKIE_NAME))
    except AuthenticationError as error:
        try:
            audit_event(
                "unauthorized_request",
                success=False,
                source_hash=request_source_fingerprint(request),
                metadata=request_metadata(request),
            )
        except SecurityConfigurationError:
            logger.error("Security source fingerprint configuration is unavailable.")
        raise HTTPException(status_code=401, detail="Authentication required.") from error
    except DatabaseUnavailableError as error:
        logger.exception("Authentication database is unavailable.")
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable.") from error


def require_analyst(user: dict[str, Any] = Depends(require_authenticated_user)) -> dict[str, Any]:
    if user.get("role") not in {"analyst", "admin"}:
        raise HTTPException(status_code=403, detail="Analyst authorization required.")
    return user


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV")),
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
        path="/",
    )


@app.get("/")
def home():
    return RedirectResponse(url="/frontend/")


@app.get("/frontend-config.js", include_in_schema=False)
def frontend_config():
    """Expose only the optional public API origin to the static frontend.

    The included Vercel deployment is same-origin, so this remains empty by
    default.  Credentials and other server configuration are never sent here.
    """
    api_base_url = get_frontend_api_base_url()
    return Response(
        content=f"window.SENTINELPAY_API_BASE_URL = {json.dumps(api_base_url)};\n",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/system-status")
def system_status(request: FastAPIRequest, user: dict[str, Any] = Depends(require_authenticated_user)):
    """Return only safe, observable platform state; never configuration values."""
    enforce_request_rate_limit(request, scope="authenticated", user_id=user["id"])
    from backend.database import persistence_enabled, get_connection
    database_state = "DATABASE CONNECTED"
    try:
        if not persistence_enabled():
            database_state = "DATABASE DEGRADED"
        else:
            with get_connection() as connection:
                connection.execute("SELECT 1")
    except DatabaseUnavailableError:
        database_state = "DATABASE DEGRADED"
    # Configuration is not a health check. Gemini availability is verified for
    # each request and a deterministic fallback remains authoritative on error.
    ai_state = "AI ENGINE CONFIGURED" if os.getenv("GEMINI_API_KEY") else "AI ENGINE UNAVAILABLE"
    return {"ai": {"status": ai_state}, "database": {"status": database_state}, "fallback_engine": {"status": "RULE ENGINE ACTIVE"}, "authentication": {"status": "AUTHENTICATION ACTIVE"}, "audit_logging": {"status": "AUDIT LOGGING ACTIVE"}, "cors": {"status": "RESTRICTED" if cors_allowed_origins else "SAME-ORIGIN"}}


@app.post("/auth/register", status_code=201)
def register(credentials: Credentials, request: FastAPIRequest, response: Response):
    source_hash = enforce_request_rate_limit(request, scope="register")
    try:
        user = create_user(credentials.email, credentials.password)
        token, _ = create_session(user["id"])
    except DuplicateUserError as error:
        audit_event("registration", success=False, source_hash=source_hash, metadata=request_metadata(request))
        raise HTTPException(status_code=409, detail="Unable to register with those credentials.") from error
    except DatabaseUnavailableError as error:
        logger.exception("Authentication database is unavailable.")
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable.") from error
    set_session_cookie(response, token)
    audit_event("registration", success=True, user_id=user["id"], source_hash=source_hash, metadata=request_metadata(request))
    return {"user": public_user(user)}


@app.post("/auth/login")
def login(credentials: Credentials, request: FastAPIRequest, response: Response):
    source_hash = enforce_request_rate_limit(request, scope="login")
    try:
        user = authenticate_user(credentials.email, credentials.password)
        if user is None:
            audit_event("login", success=False, source_hash=source_hash, metadata=request_metadata(request))
            raise HTTPException(status_code=401, detail="Invalid email or password.")
        token, _ = create_session(user["id"])
    except DatabaseUnavailableError as error:
        logger.exception("Authentication database is unavailable.")
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable.") from error
    set_session_cookie(response, token)
    audit_event("login", success=True, user_id=user["id"], source_hash=source_hash, metadata=request_metadata(request))
    return {"user": public_user(user)}


@app.post("/auth/logout", status_code=204)
def logout(request: FastAPIRequest, response: Response, user: dict[str, Any] = Depends(require_authenticated_user)):
    source_hash = enforce_request_rate_limit(request, scope="authenticated", user_id=user["id"])
    try:
        delete_session(request.cookies.get(SESSION_COOKIE_NAME))
    except DatabaseUnavailableError as error:
        logger.exception("Authentication database is unavailable.")
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable.") from error
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    audit_event("logout", success=True, user_id=user["id"], source_hash=source_hash, metadata=request_metadata(request))


@app.get("/auth/me")
def current_user(request: FastAPIRequest, user: dict[str, Any] = Depends(require_authenticated_user)):
    enforce_request_rate_limit(request, scope="authenticated", user_id=user["id"])
    return {"user": public_user(user)}


@app.get("/transactions")
def list_transactions(request: FastAPIRequest, user: dict[str, Any] = Depends(require_authenticated_user)):
    enforce_request_rate_limit(request, scope="authenticated", user_id=user["id"])
    try:
        return get_recent_transactions(user["id"])
    except DatabaseUnavailableError as error:
        logger.exception("Transaction history database is unavailable.")
        raise HTTPException(status_code=503, detail="Transaction history is temporarily unavailable.") from error


@app.get("/transactions/{transaction_id}")
def read_transaction(transaction_id: int, request: FastAPIRequest, user: dict[str, Any] = Depends(require_authenticated_user)):
    enforce_request_rate_limit(request, scope="authenticated", user_id=user["id"])
    try:
        transaction = get_transaction(transaction_id, user["id"])
    except DatabaseUnavailableError as error:
        logger.exception("Transaction history database is unavailable.")
        raise HTTPException(status_code=503, detail="Transaction history is temporarily unavailable.") from error

    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    transaction["signals"] = deterministic_signals(transaction)
    transaction["analysis_mode"] = "AI" if transaction["analysis_source"] == "gemini" else "FALLBACK"
    transaction["confidence"] = None
    transaction["timeline"] = [{"stage": "Transaction received", "timestamp": transaction["transaction_timestamp"]}, {"stage": "Risk engine completed", "timestamp": transaction["created_at"]}, {"stage": f"Decision: {transaction.get('review_decision') or transaction['decision']}", "timestamp": transaction.get("reviewed_at") or transaction["created_at"]}]
    return transaction


@app.get("/analytics")
def analytics(request: FastAPIRequest, user: dict[str, Any] = Depends(require_authenticated_user)):
    enforce_request_rate_limit(request, scope="authenticated", user_id=user["id"])
    try:
        transactions = get_recent_transactions(user["id"], limit=500)
    except DatabaseUnavailableError as error:
        raise HTTPException(status_code=503, detail="Analytics are temporarily unavailable.") from error
    total = len(transactions)
    distribution = {level: sum(t["risk_level"] == level for t in transactions) for level in ("LOW", "MEDIUM", "HIGH", "CRITICAL")}
    timings = [t["processing_time_ms"] for t in transactions if t.get("processing_time_ms") is not None]
    return {"total": total, "allowed": sum(t["decision"] == "ALLOW" for t in transactions), "review": sum(t["decision"] == "REVIEW" for t in transactions), "blocked": sum(t["decision"] == "BLOCK" for t in transactions), "high_risk": distribution["HIGH"] + distribution["CRITICAL"], "average_risk_score": round(sum(t["risk_score"] for t in transactions) / total, 1) if total else None, "ai_usage": sum(t["analysis_source"] == "gemini" for t in transactions), "fallback_usage": sum(t["analysis_source"] != "gemini" for t in transactions), "average_processing_time_ms": round(sum(timings) / len(timings), 1) if timings else None, "risk_distribution": distribution}


@app.post("/transactions/{transaction_id}/review")
def review_transaction(transaction_id: int, review: ReviewDecision, request: FastAPIRequest, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"), user: dict[str, Any] = Depends(require_analyst)):
    enforce_request_rate_limit(request, scope="authenticated", user_id=user["id"])
    try:
        if idempotency_key:
            transaction = set_review_decision_idempotent(transaction_id=transaction_id, user_id=user["id"], decision=review.decision, idempotency_key=idempotency_key, request_hash=action_idempotency_hash(action="manual_review", resource_id=transaction_id, payload=review.model_dump()), source_hash=request_source_fingerprint(request), metadata={**request_metadata(request), "decision": review.decision, "transaction_id": transaction_id})
        else:
            from backend.database import set_review_decision
            transaction = set_review_decision(transaction_id, user["id"], review.decision)
    except DatabaseUnavailableError as error:
        raise HTTPException(status_code=503, detail="Review workflow is temporarily unavailable.") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ActionResourceNotFoundError:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    if not idempotency_key:
        audit_event("manual_review", success=True, user_id=user["id"], source_hash=request_source_fingerprint(request), metadata={**request_metadata(request), "decision": review.decision, "transaction_id": transaction_id})
    return transaction


@app.get("/audit-events")
def list_audit_events(request: FastAPIRequest, user: dict[str, Any] = Depends(require_authenticated_user)):
    enforce_request_rate_limit(request, scope="authenticated", user_id=user["id"])
    try:
        return own_audit_events(user["id"])
    except DatabaseUnavailableError as error:
        logger.exception("Audit log database is unavailable.")
        raise HTTPException(status_code=503, detail="Audit history is temporarily unavailable.") from error


@app.get("/alerts")
def list_alerts(request: FastAPIRequest, user: dict[str, Any] = Depends(require_authenticated_user)):
    enforce_request_rate_limit(request, scope="authenticated", user_id=user["id"])
    try:
        return get_alerts_for_user(user["id"])
    except DatabaseUnavailableError as error:
        raise HTTPException(status_code=503, detail="Alerts are temporarily unavailable.") from error


@app.post("/alerts/{alert_id}")
def change_alert_status(alert_id: int, update: AlertStatusUpdate, request: FastAPIRequest, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"), user: dict[str, Any] = Depends(require_analyst)):
    enforce_request_rate_limit(request, scope="authenticated", user_id=user["id"])
    try:
        if idempotency_key:
            alert = update_alert_status_idempotent(alert_id=alert_id, user_id=user["id"], status=update.status, idempotency_key=idempotency_key, request_hash=action_idempotency_hash(action="alert_status_change", resource_id=alert_id, payload=update.model_dump()), source_hash=request_source_fingerprint(request), metadata={**request_metadata(request), "alert_id": alert_id, "status": update.status})
        else:
            alert = update_alert_status(alert_id, user["id"], update.status)
    except DatabaseUnavailableError as error:
        raise HTTPException(status_code=503, detail="Alerts are temporarily unavailable.") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ActionResourceNotFoundError:
        raise HTTPException(status_code=404, detail="Alert not found.")
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found.")
    if not idempotency_key:
        audit_event("alert_status_change", success=True, user_id=user["id"], source_hash=request_source_fingerprint(request), metadata={**request_metadata(request), "alert_id": alert_id, "status": update.status})
    return alert


def rule_based_assessment(transaction: Transaction) -> dict[str, Any]:
    """Provide a deterministic assessment when Gemini cannot be used."""
    score = 0

    if transaction.amount_minor >= 5_000_000:
        score += 30
    elif transaction.amount_minor >= 2_500_000:
        score += 20

    if transaction.velocity >= 10:
        score += 30
    elif transaction.velocity >= 5:
        score += 15

    if transaction.device.lower() != "trusted":
        score += 20

    if score >= 70:
        risk_level, decision = "HIGH", "BLOCK"
    elif score >= 40:
        risk_level, decision = "MEDIUM", "REVIEW"
    else:
        risk_level, decision = "LOW", "ALLOW"

    return {
        "risk_score": score,
        "risk_level": risk_level,
        "decision": decision,
        "explanation": "Rule-based fraud assessment because Gemini is unavailable.",
        "analysis_source": "rule_based",
        "provider": "rule_based_fallback",
    }


def apply_deterministic_policy(transaction: Transaction, analysis: dict[str, Any]) -> dict[str, Any]:
    """Prevent AI output from weakening the deterministic fraud policy floor."""
    policy = rule_based_assessment(transaction)
    severity = {"ALLOW": 0, "REVIEW": 1, "BLOCK": 2}
    if severity[analysis["decision"]] >= severity[policy["decision"]]:
        return analysis
    analysis = {**analysis}
    analysis["risk_score"] = max(analysis["risk_score"], policy["risk_score"])
    analysis["risk_level"] = policy["risk_level"]
    analysis["decision"] = policy["decision"]
    analysis["explanation"] = (
        f"{analysis['explanation']} Deterministic transaction policy requires "
        f"{policy['decision']}."
    )
    return analysis


def build_gemini_prompt(transaction: Transaction) -> str:
    """Build a narrowly scoped prompt whose output is a JSON fraud assessment."""
    return f"""
You are SentinelPay AI, a financial transaction fraud-risk engine.

Analyze this transaction:
Amount: INR {format_money_minor(transaction.amount_minor)}
Sender: {transaction.sender}
Receiver: {transaction.receiver}
Location: {transaction.location}
Device: {transaction.device}
Transaction velocity: {transaction.velocity}

Evaluate amount, velocity, device trust, location anomalies, and overall fraud indicators.

Return JSON only, with exactly these fields and no markdown:
{{
  "risk_score": number between 0 and 100,
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "decision": "ALLOW" | "REVIEW" | "BLOCK",
  "explanation": "short explanation"
}}

Use LOW/ALLOW for scores 0-39, MEDIUM/REVIEW for 40-69, and HIGH/BLOCK for 70-100.
"""


def build_gemini_request_payload(transaction: Transaction) -> dict[str, Any]:
    """Build the Gemini REST generateContent JSON body without tools or AFC."""
    return {
        "contents": [{"parts": [{"text": build_gemini_prompt(transaction)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }


def extract_gemini_json(response_text: str | None) -> dict[str, Any]:
    """Parse JSON from Gemini, accepting an accidental Markdown code fence."""
    if not response_text or not response_text.strip():
        raise ValueError("Gemini returned an empty response body")

    text = response_text.strip()
    fenced_match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced_match:
        text = fenced_match.group(1).strip()

    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Gemini response JSON must be an object")
    return parsed


def validate_gemini_assessment(analysis: dict[str, Any]) -> dict[str, Any]:
    """Normalize and validate Gemini output before returning it to the client."""
    required_fields = {"risk_score", "risk_level", "decision", "explanation"}
    missing_fields = required_fields.difference(analysis)
    if missing_fields:
        raise ValueError(f"Gemini response is missing required fields: {sorted(missing_fields)}")

    try:
        risk_score = float(analysis["risk_score"])
    except (TypeError, ValueError) as error:
        raise ValueError("Gemini risk_score must be numeric") from error

    if not math.isfinite(risk_score):
        raise ValueError("Gemini risk_score must be finite")

    risk_level = str(analysis["risk_level"]).upper().strip()
    decision = str(analysis["decision"]).upper().strip()
    explanation_value = analysis["explanation"]
    if not isinstance(explanation_value, str):
        raise ValueError("Gemini explanation must be a string")
    explanation = explanation_value.strip()

    if risk_level not in {"LOW", "MEDIUM", "HIGH"}:
        raise ValueError(f"Gemini returned invalid risk_level: {risk_level!r}")
    if decision not in {"ALLOW", "REVIEW", "BLOCK"}:
        raise ValueError(f"Gemini returned invalid decision: {decision!r}")
    if not explanation:
        raise ValueError("Gemini explanation must not be empty")

    return {
        "risk_score": max(0, min(100, int(risk_score))),
        "risk_level": risk_level,
        "decision": decision,
        "explanation": explanation,
        "analysis_source": "gemini",
        "provider": "gemini",
    }


def parse_gemini_response(response_body: str) -> dict[str, Any]:
    """Extract the model text from a Gemini REST response and validate it."""
    try:
        response_json = json.loads(response_body)
        response_text = response_json["candidates"][0]["content"]["parts"][0]["text"]
        return validate_gemini_assessment(extract_gemini_json(response_text))
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise GeminiRequestError(
            "Gemini REST response did not contain a valid assessment at candidates[0].content.parts[0].text",
            status_code=200,
            response_body=response_body,
        ) from error


def request_gemini(transaction: Transaction, *, api_key: str, gemini_model: str) -> str:
    """Call Gemini generateContent over REST and return its raw JSON response body."""
    request = Request(
        GEMINI_API_URL_TEMPLATE.format(model=gemini_model),
        data=json.dumps(build_gemini_request_payload(transaction)).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=GEMINI_REQUEST_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8")
    except HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        retry_after_seconds = parse_retry_after(
            error.headers.get("Retry-After") if error.headers else None
        )
        raise GeminiRequestError(
            f"Gemini REST request failed with HTTP {error.code}",
            status_code=error.code,
            response_body=response_body,
            retry_after_seconds=retry_after_seconds,
        ) from error
    except URLError:
        raise


def generate_gemini_assessment(
    transaction: Transaction, *, api_key: str, gemini_model: str
) -> dict[str, Any]:
    """Request and validate Gemini output with bounded retries for transient failures."""
    for attempt in range(GEMINI_MAX_RETRIES + 1):
        try:
            return parse_gemini_response(
                request_gemini(transaction, api_key=api_key, gemini_model=gemini_model)
            )
        except Exception as error:
            if attempt < GEMINI_MAX_RETRIES and is_transient_gemini_error(error):
                delay = gemini_retry_delay(attempt, error)
                logger.warning(
                    "Gemini REST request failed transiently; retrying after %.2fs. model=%s status=%s key_configured=%s detail=%s",
                    delay,
                    gemini_model,
                    getattr(error, "status_code", None),
                    True,
                    type(error).__name__,
                )
                time.sleep(delay)
                continue
            raise


def log_gemini_failure(error: Exception, *, gemini_model: str | None, key_configured: bool) -> None:
    """Log actionable Gemini REST diagnostics without ever logging the API key."""
    logger.error(
        "Gemini REST assessment failed; using rule-based fallback. model=%s key_configured=%s status=%s error_type=%s detail=%s",
        gemini_model,
        key_configured,
        getattr(error, "status_code", None),
        type(error).__name__,
        error,
        exc_info=True,
    )


@app.post("/transaction/check")
def check_transaction(
    transaction: Transaction, request: FastAPIRequest, user: dict[str, Any] = Depends(require_authenticated_user)
):
    started_at = time.perf_counter()
    source_hash = enforce_request_rate_limit(request, scope="transaction_check", user_id=user["id"])
    request_hash = idempotency_hash(transaction) if transaction.transaction_id else None
    if not persistence_enabled():
        raise HTTPException(status_code=503, detail="Transaction processing is temporarily unavailable.")
    if transaction.transaction_id:
        try:
            existing = get_transaction_by_idempotency_key(transaction.transaction_id, user["id"])
        except DatabaseUnavailableError as error:
            raise HTTPException(status_code=503, detail="Transaction processing is temporarily unavailable.") from error
        if existing is not None:
            if existing.get("idempotency_request_hash") != request_hash:
                raise HTTPException(status_code=409, detail="Idempotency key was already used with a different transaction.")
            return stored_result(existing)
    try:
        api_key, gemini_model = get_gemini_configuration()
    except GeminiRequestError as error:
        log_gemini_failure(error, gemini_model=None, key_configured=bool(os.getenv("GEMINI_API_KEY")))
        analysis = rule_based_assessment(transaction)
        fallback_reason = "configuration_error"
    else:
        if not api_key:
            logger.info("GEMINI_API_KEY is not configured; using rule-based fallback.")
            analysis = rule_based_assessment(transaction)
            fallback_reason = "not_configured"
        else:
            try:
                analysis = generate_gemini_assessment(
                    transaction, api_key=api_key, gemini_model=gemini_model
                )
            except Exception as error:
                log_gemini_failure(error, gemini_model=gemini_model, key_configured=True)
                analysis = rule_based_assessment(transaction)
                fallback_reason = type(error).__name__

    analysis = apply_deterministic_policy(transaction, analysis)

    processing_time_ms = round((time.perf_counter() - started_at) * 1000, 1)
    now = datetime.now(timezone.utc).isoformat()
    analysis_mode = "AI" if analysis["analysis_source"] == "gemini" else "FALLBACK"
    result = {
        "transaction": transaction_payload(transaction),
        "risk_score": analysis["risk_score"],
        "risk_level": analysis["risk_level"],
        "decision": analysis["decision"],
        "explanation": analysis["explanation"],
        "analysis_source": analysis["analysis_source"],
        "provider": analysis["provider"],
        "analysis_mode": analysis_mode,
        "confidence": None,
        "processing_time_ms": processing_time_ms,
        "signals": deterministic_signals(transaction),
        "timeline": [{"stage": "Transaction received", "timestamp": now}, {"stage": "Validation completed", "timestamp": now}, {"stage": "Risk signals evaluated", "timestamp": now}, {"stage": "AI analysis" if analysis_mode == "AI" else "Fallback analysis", "timestamp": now}, {"stage": f"Decision: {analysis['decision']}", "timestamp": now}],
    }
    try:
        stored = save_transaction(
            {
                **transaction_payload(transaction),
                "amount_minor": transaction.amount_minor,
                "user_id": user["id"],
                "session_id": str(user["id"]),
                "currency": transaction.currency,
                "merchant": transaction.merchant or transaction.receiver,
                "transaction_timestamp": datetime.now(timezone.utc),
                "risk_score": result["risk_score"],
                "risk_level": result["risk_level"],
                "decision": result["decision"],
                "provider": result["provider"],
                "explanation": result["explanation"],
                "ai_explanation": result["explanation"],
                "analysis_source": result["analysis_source"],
                "processing_time_ms": processing_time_ms,
                "idempotency_key": transaction.transaction_id,
                "idempotency_request_hash": request_hash,
            },
            alert={
                "severity": result["risk_level"],
                "title": f"{result['risk_level']} risk transaction",
                "reason": result["explanation"],
            } if result["risk_level"] in {"MEDIUM", "HIGH", "CRITICAL"} else None,
            audit={
                "event_type": "transaction_check",
                "source_hash": source_hash,
                "metadata": {
                    **request_metadata(request),
                    "analysis_source": analysis["analysis_source"],
                    "fallback_reason": fallback_reason if analysis["analysis_source"] != "gemini" else None,
                },
            },
        )
        if stored is None and transaction.transaction_id:
            existing = get_transaction_by_idempotency_key(transaction.transaction_id, user["id"])
            if existing is None:
                raise DatabaseUnavailableError("Idempotent transaction result was not available")
            if existing.get("idempotency_request_hash") != request_hash:
                raise HTTPException(status_code=409, detail="Idempotency key was already used with a different transaction.")
            return stored_result(existing)
        if stored is None:
            raise DatabaseUnavailableError("Transaction persistence did not return a stored record")
    except HTTPException:
        raise
    except DatabaseUnavailableError as error:
        logger.exception("Transaction persistence is unavailable; refusing to report success.")
        raise HTTPException(status_code=503, detail="Transaction processing is temporarily unavailable.") from error
    except Exception as error:
        logger.exception("Transaction persistence failed; refusing to report success.")
        raise HTTPException(status_code=503, detail="Transaction processing is temporarily unavailable.") from error

    return result
