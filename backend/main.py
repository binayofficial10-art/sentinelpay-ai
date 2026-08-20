import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


# Local-only .env loading does not override Vercel environment variables.
load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def get_cors_allowed_origins() -> list[str]:
    """Read an explicit comma-separated CORS allowlist from the environment."""
    return [
        origin.strip().rstrip("/")
        for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]


app = FastAPI(title="SentinelPay AI")
cors_allowed_origins = get_cors_allowed_origins()
if cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/frontend", StaticFiles(directory=frontend_dir, html=True), name="frontend")

api_key = os.getenv("GEMINI_API_KEY")
gemini_model = os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL


class GeminiRequestError(Exception):
    """Gemini REST error with safe diagnostic details for server-side logs."""

    def __init__(self, message: str, *, status_code: int | None = None, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def is_transient_gemini_error(error: Exception) -> bool:
    """Return whether one short retry is appropriate for a Gemini REST request."""
    if isinstance(error, GeminiRequestError):
        return error.status_code in GEMINI_RETRYABLE_STATUS_CODES
    return isinstance(error, URLError)


class Transaction(BaseModel):
    amount: float = Field(ge=0)
    sender: str = Field(min_length=1)
    receiver: str = Field(min_length=1)
    location: str = Field(min_length=1)
    device: str = Field(min_length=1)
    velocity: int = Field(ge=0)


@app.get("/")
def home():
    return RedirectResponse(url="/frontend/")


@app.get("/health")
def health():
    return {"status": "ok"}


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
        "analysis_source": "fallback",
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


def request_gemini(transaction: Transaction) -> str:
    """Call Gemini generateContent over REST and return its raw JSON response body."""
    if not api_key:
        raise GeminiRequestError("GEMINI_API_KEY is not configured")

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
        with urlopen(request, timeout=15) as response:
            return response.read().decode("utf-8")
    except HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        raise GeminiRequestError(
            f"Gemini REST request failed with HTTP {error.code}",
            status_code=error.code,
            response_body=response_body,
        ) from error
    except URLError:
        raise


def generate_gemini_assessment(transaction: Transaction) -> dict[str, Any]:
    """Request and validate Gemini output, retrying only temporary REST failures once."""
    for attempt in range(2):
        try:
            return parse_gemini_response(request_gemini(transaction))
        except Exception as error:
            if attempt == 0 and is_transient_gemini_error(error):
                logger.warning(
                    "Gemini REST request failed transiently; retrying once. model=%s status=%s key_configured=%s detail=%s",
                    gemini_model,
                    getattr(error, "status_code", None),
                    bool(api_key),
                    getattr(error, "response_body", str(error)),
                )
                time.sleep(0.25)
                continue
            raise


def log_gemini_failure(error: Exception) -> None:
    """Log actionable Gemini REST diagnostics without ever logging the API key."""
    logger.error(
        "Gemini REST assessment failed; using rule-based fallback. model=%s key_configured=%s status=%s error_type=%s response_body=%s detail=%s",
        gemini_model,
        bool(api_key),
        getattr(error, "status_code", None),
        type(error).__name__,
        getattr(error, "response_body", ""),
        error,
        exc_info=True,
    )


@app.post("/transaction/check")
def check_transaction(transaction: Transaction):
    if not api_key:
        logger.info("GEMINI_API_KEY is not configured; using rule-based fallback.")
        analysis = rule_based_assessment(transaction)
    else:
        try:
            analysis = generate_gemini_assessment(transaction)
        except Exception as error:
            log_gemini_failure(error)
            analysis = rule_based_assessment(transaction)

    return {
        "transaction": transaction.model_dump(),
        "risk_score": analysis["risk_score"],
        "risk_level": analysis["risk_level"],
        "decision": analysis["decision"],
        "explanation": analysis["explanation"],
        "analysis_source": analysis["analysis_source"],
    }
