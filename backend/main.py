import os
import json
import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from google import genai
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger(__name__)


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
gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

client = genai.Client(api_key=api_key) if api_key else None


def is_gemini_quota_error(error: Exception) -> bool:
    """Return whether a Gemini request failed because the service quota is exhausted."""
    status_code = getattr(error, "status_code", None)
    error_code = getattr(error, "code", None)
    return (
        status_code == 429
        or error_code == 429
        or "RESOURCE_EXHAUSTED" in str(error).upper()
    )


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


def rule_based_assessment(transaction: Transaction) -> dict:
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
    }


@app.post("/transaction/check")
def check_transaction(transaction: Transaction):

    prompt = f"""
You are SentinelPay AI, a financial transaction fraud-risk engine.

Analyze this transaction:

Amount: ₹{transaction.amount}
Sender: {transaction.sender}
Receiver: {transaction.receiver}
Location: {transaction.location}
Device: {transaction.device}
Transaction velocity: {transaction.velocity}

Evaluate:
- Transaction amount
- Transaction velocity
- Device trust
- Location anomalies
- Overall fraud indicators

Return ONLY valid JSON in exactly this format:

{{
    "risk_score": 0,
    "risk_level": "LOW",
    "decision": "ALLOW",
    "explanation": "Short explanation"
}}

Rules:
- risk_score must be between 0 and 100
- LOW = 0-39
- MEDIUM = 40-69
- HIGH = 70-100
- decision must be ALLOW, REVIEW, or BLOCK
- HIGH risk should normally result in BLOCK
- MEDIUM risk should normally result in REVIEW
- LOW risk should normally result in ALLOW
"""

    # Try Gemini
    try:
        if client is None:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        response = client.models.generate_content(
            model=gemini_model,
            contents=prompt,
            config={
                "response_mime_type": "application/json"
            }
        )

        analysis = json.loads(response.text)

        required_fields = {"risk_score", "risk_level", "decision", "explanation"}
        if not required_fields.issubset(analysis):
            raise ValueError("Gemini response is missing required analysis fields")

        analysis["risk_score"] = max(0, min(100, int(analysis["risk_score"])))
        analysis["risk_level"] = str(analysis["risk_level"]).upper()
        analysis["decision"] = str(analysis["decision"]).upper()
        analysis["explanation"] = str(analysis["explanation"])
        if analysis["risk_level"] not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("Gemini returned an invalid risk level")
        if analysis["decision"] not in {"ALLOW", "REVIEW", "BLOCK"}:
            raise ValueError("Gemini returned an invalid decision")
        analysis["analysis_source"] = "gemini"

    except Exception as e:
        if is_gemini_quota_error(e):
            logger.warning(
                "Gemini quota exhausted for model %s; using rule-based fallback.",
                gemini_model,
            )
        else:
            logger.warning(
                "Gemini analysis unavailable (%s); using rule-based fallback.",
                type(e).__name__,
            )

        analysis = rule_based_assessment(transaction)

    return {
        "transaction": transaction.model_dump(),
        "risk_score": analysis["risk_score"],
        "risk_level": analysis["risk_level"],
        "decision": analysis["decision"],
        "explanation": analysis["explanation"],
        "analysis_source": analysis["analysis_source"],
    }
