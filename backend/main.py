import json
import logging
import math
import os
import random
import re
import socket
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

# Local-only .env loading does not override Vercel environment variables and
# must run before the database module reads DATABASE_URL.
load_dotenv(Path(__file__).resolve().parent / ".env")

from backend.database import (
    DatabaseUnavailableError,
    get_recent_transactions,
    get_transaction,
    save_transaction,
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
AUTH_RATE_LIMIT = 10
AUTH_RATE_WINDOW_SECONDS = 60
_auth_attempts: dict[str, list[float]] = {}


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
    model_config = ConfigDict(extra="forbid")

    amount: float = Field(ge=0)
    sender: str = Field(min_length=1)
    receiver: str = Field(min_length=1)
    location: str = Field(min_length=1)
    device: str = Field(min_length=1)
    velocity: int = Field(ge=0)


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=12, max_length=256)


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {"id": user["id"], "email": user["email"], "created_at": user["created_at"]}


def check_auth_rate_limit(request: FastAPIRequest) -> None:
    client_host = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = [timestamp for timestamp in _auth_attempts.get(client_host, []) if now - timestamp < AUTH_RATE_WINDOW_SECONDS]
    if len(attempts) >= AUTH_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many authentication attempts. Please try again later.")
    attempts.append(now)
    _auth_attempts[client_host] = attempts


def require_authenticated_user(request: FastAPIRequest) -> dict[str, Any]:
    try:
        return get_user_for_session(request.cookies.get(SESSION_COOKIE_NAME))
    except AuthenticationError as error:
        raise HTTPException(status_code=401, detail="Authentication required.") from error
    except DatabaseUnavailableError as error:
        logger.exception("Authentication database is unavailable.")
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable.") from error


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
    api_base_url = os.getenv("FRONTEND_API_BASE_URL", "").strip().rstrip("/")
    return Response(
        content=f"window.SENTINELPAY_API_BASE_URL = {json.dumps(api_base_url)};\n",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/register", status_code=201)
def register(credentials: Credentials, request: FastAPIRequest, response: Response):
    check_auth_rate_limit(request)
    try:
        user = create_user(credentials.email, credentials.password)
        token, _ = create_session(user["id"])
    except DuplicateUserError as error:
        raise HTTPException(status_code=409, detail="Unable to register with those credentials.") from error
    except DatabaseUnavailableError as error:
        logger.exception("Authentication database is unavailable.")
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable.") from error
    set_session_cookie(response, token)
    return {"user": public_user(user)}


@app.post("/auth/login")
def login(credentials: Credentials, request: FastAPIRequest, response: Response):
    check_auth_rate_limit(request)
    try:
        user = authenticate_user(credentials.email, credentials.password)
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid email or password.")
        token, _ = create_session(user["id"])
    except DatabaseUnavailableError as error:
        logger.exception("Authentication database is unavailable.")
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable.") from error
    set_session_cookie(response, token)
    return {"user": public_user(user)}


@app.post("/auth/logout", status_code=204)
def logout(request: FastAPIRequest, response: Response):
    try:
        delete_session(request.cookies.get(SESSION_COOKIE_NAME))
    except DatabaseUnavailableError as error:
        logger.exception("Authentication database is unavailable.")
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable.") from error
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


@app.get("/auth/me")
def current_user(user: dict[str, Any] = Depends(require_authenticated_user)):
    return {"user": public_user(user)}


@app.get("/transactions")
def list_transactions(user: dict[str, Any] = Depends(require_authenticated_user)):
    try:
        return get_recent_transactions(user["id"])
    except DatabaseUnavailableError as error:
        logger.exception("Transaction history database is unavailable.")
        raise HTTPException(status_code=503, detail="Transaction history is temporarily unavailable.") from error


@app.get("/transactions/{transaction_id}")
def read_transaction(transaction_id: int, user: dict[str, Any] = Depends(require_authenticated_user)):
    try:
        transaction = get_transaction(transaction_id, user["id"])
    except DatabaseUnavailableError as error:
        logger.exception("Transaction history database is unavailable.")
        raise HTTPException(status_code=503, detail="Transaction history is temporarily unavailable.") from error

    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    return transaction


def rule_based_assessment(transaction: Transaction) -> dict[str, Any]:
    """Provide a deterministic assessment when Gemini cannot be used."""
    score = 0

    if transaction.amount >= 50000:
        score += 30
    elif transaction.amount >= 25000:
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


def build_gemini_prompt(transaction: Transaction) -> str:
    """Build a narrowly scoped prompt whose output is a JSON fraud assessment."""
    return f"""
You are SentinelPay AI, a financial transaction fraud-risk engine.

Analyze this transaction:
Amount: INR {transaction.amount}
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
def check_transaction(transaction: Transaction, user: dict[str, Any] = Depends(require_authenticated_user)):
    try:
        api_key, gemini_model = get_gemini_configuration()
    except GeminiRequestError as error:
        log_gemini_failure(error, gemini_model=None, key_configured=bool(os.getenv("GEMINI_API_KEY")))
        analysis = rule_based_assessment(transaction)
    else:
        if not api_key:
            logger.info("GEMINI_API_KEY is not configured; using rule-based fallback.")
            analysis = rule_based_assessment(transaction)
        else:
            try:
                analysis = generate_gemini_assessment(
                    transaction, api_key=api_key, gemini_model=gemini_model
                )
            except Exception as error:
                log_gemini_failure(error, gemini_model=gemini_model, key_configured=True)
                analysis = rule_based_assessment(transaction)

    result = {
        "transaction": transaction.model_dump(),
        "risk_score": analysis["risk_score"],
        "risk_level": analysis["risk_level"],
        "decision": analysis["decision"],
        "explanation": analysis["explanation"],
        "analysis_source": analysis["analysis_source"],
        "provider": analysis["provider"],
    }
    try:
        save_transaction(
            {
                **transaction.model_dump(),
                "user_id": user["id"],
                "session_id": str(user["id"]),
                "currency": "INR",
                "merchant": transaction.receiver,
                "transaction_timestamp": datetime.now(timezone.utc),
                "risk_score": result["risk_score"],
                "risk_level": result["risk_level"],
                "decision": result["decision"],
                "provider": result["provider"],
                "explanation": result["explanation"],
                "ai_explanation": result["explanation"],
                "analysis_source": result["analysis_source"],
            }
        )
    except Exception:
        logger.exception("Transaction analysis could not be persisted.")

    return result
