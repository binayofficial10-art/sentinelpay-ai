# SentinelPay AI

SentinelPay AI is an AI-assisted transaction fraud-risk detection prototype. It accepts transaction details, evaluates risk signals, and presents a risk score, risk level, decision, and explanation in a responsive browser dashboard.

> **Prototype notice:** SentinelPay AI is intended for learning, portfolio, college, and hackathon use. It is not a banking system and must not be the sole basis for payment, credit, fraud, or other high-impact decisions.

## 1. Project Overview

The application receives an amount, sender, receiver, location, device status, and transaction velocity. The FastAPI backend asks Gemini for a structured risk assessment when Gemini is configured and available. It returns:

- A risk score from 0 to 100
- A `LOW`, `MEDIUM`, or `HIGH` risk level
- An `ALLOW`, `REVIEW`, or `BLOCK` decision
- A short AI-generated explanation

If Gemini is unavailable, returns an error, reaches a quota limit, or produces invalid output, SentinelPay AI safely uses its built-in rule-based assessment instead. The response identifies whether it came from Gemini or the fallback.

## 2. Key Features

- Responsive HTML, CSS, and JavaScript dashboard for phone and laptop browsers
- Transaction form with amount, sender, receiver, location, device, and velocity fields
- FastAPI input validation for transaction requests
- Gemini REST `generateContent` assessment when configured
- Validated JSON responses with risk score clamping and allowed risk/decision values
- Rule-based fallback that keeps analysis available when Gemini cannot be used
- Recent transaction history with loading, empty, and error states
- Local SQLite storage and optional PostgreSQL storage through `DATABASE_URL`
- Same-origin frontend and API served together by FastAPI on Vercel
- `/health` endpoint for availability checks

## 3. System Architecture

```text
Browser (frontend/index.html, script.js, style.css)
        |
        | GET /frontend/ and POST /transaction/check
        v
FastAPI application (app.py -> backend.main:app)
        |
        +--> Gemini REST generateContent API (optional, server-side only)
        |       |
        |       +--> validated JSON assessment
        |
        +--> rule-based assessment (when Gemini is unavailable or invalid)
        |
        +--> transaction storage
                |- local SQLite during local development
                `- PostgreSQL when DATABASE_URL is configured in production
```

On Vercel, `app.py` is the Python entry point and imports the FastAPI application from `backend.main`. The frontend is mounted by FastAPI at `/frontend/`; `/` redirects there. The frontend uses its current origin by default, so deployed requests go to the same Vercel site rather than a localhost URL.

## 4. Technology Stack

- Python 3.12
- FastAPI and Uvicorn
- Pydantic request models
- Python standard-library HTTP client for the Gemini REST API
- Google Gemini `generateContent` API
- HTML, CSS, and vanilla JavaScript
- SQLite for local storage
- PostgreSQL support through `psycopg` when `DATABASE_URL` is configured
- Vercel Python runtime

## 5. How the System Works

1. A user enters transaction information in the dashboard.
2. The frontend sends a `POST` request to `/transaction/check` on the same origin.
3. FastAPI validates the request and builds a transaction-risk prompt.
4. When `GEMINI_API_KEY` is configured, the backend calls Gemini over its REST API.
5. Gemini's text response is parsed as JSON and validated before it can be returned.
6. A valid Gemini response becomes a Gemini-based assessment.
7. If Gemini is not configured, is unavailable, is rate-limited, or returns malformed data, the backend uses the deterministic rule-based fallback.
8. The completed assessment is returned to the browser and storage is attempted. A storage failure does not replace a successful analysis response.
9. The frontend displays the risk score, level, decision, explanation, source, and available recent history.

## 6. Risk Levels

- `LOW` indicates lower risk according to the assessment returned for that transaction.
- `MEDIUM` indicates a transaction that warrants additional attention or review.
- `HIGH` indicates a transaction with stronger risk indicators.

Gemini assessments are validated for one of these values. The fallback implementation calculates its result from amount, transaction velocity, and whether the device is marked as trusted; its exact scoring rules are defined in `backend/main.py`.

## 7. Decisions

- `ALLOW` indicates the assessment permits the transaction.
- `REVIEW` indicates the transaction should receive further review.
- `BLOCK` indicates the assessment recommends blocking the transaction.

The exact decision is produced by the implemented Gemini or rule-based risk-analysis logic. This prototype does not connect to a payment processor or enforce decisions automatically.

## 8. Gemini Integration

Gemini is called only by the backend through the Gemini REST `generateContent` API. The browser never receives the API key and `frontend/script.js` sends only transaction data to the application API.

- `GEMINI_API_KEY` is read from the server environment.
- `GEMINI_MODEL` selects the Gemini model; the current application default is `gemini-3.7-flash`.
- The request asks for JSON output and contains no automatic function calling, tools, or function declarations.
- The backend validates required fields, allowed values, score range, and explanation before using a Gemini response.
- Gemini errors are logged server-side without logging the API key; the client receives a safe rule-based fallback assessment.

## 9. Environment Variables

Use only placeholder values in local configuration. Do not commit real credentials.

```dotenv
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.7-flash
CORS_ALLOWED_ORIGINS=
DATABASE_URL=
```

| Variable | Required | Description |
| --- | --- | --- |
| `GEMINI_API_KEY` | No | Server-side Gemini credential. Without it, rule-based analysis remains available. |
| `GEMINI_MODEL` | No | Gemini model name. The current default is `gemini-3.7-flash`. |
| `CORS_ALLOWED_ORIGINS` | No | Comma-separated allowed origins for an intentionally separate frontend. Leave empty for the included same-origin frontend. |
| `DATABASE_URL` | No locally; needed for durable Vercel history | PostgreSQL connection string. Vercel must not use a file-based SQLite path for persistence. |
| `PORT` | Local runtime | Port passed to Uvicorn. Vercel manages the production runtime. |

For local use, copy `.env.example` to `backend/.env` and edit the copied file. `backend/main.py` loads that file without overriding environment variables already provided by Vercel. The local `.env` file is ignored by Git.

In Vercel, open the project **Settings** > **Environment Variables**, add `GEMINI_API_KEY` for the intended Production and/or Preview environments, and optionally add `GEMINI_MODEL`. Add a PostgreSQL `DATABASE_URL` for durable transaction history. Do not add the Gemini key to frontend settings or source files.

## 10. Local Development

From the repository root in PowerShell:

```powershell
py -3.12 -m venv backend\venv
.\backend\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example backend\.env
```

Put a valid key in `backend/.env` only if Gemini analysis is wanted. The app continues to work without a key by using the fallback.

Start the application:

```powershell
Set-Location backend
.\venv\Scripts\python.exe -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

FastAPI serves both the API and frontend, so no separate frontend server is required. Open [http://127.0.0.1:8000/frontend/](http://127.0.0.1:8000/frontend/) (or [http://127.0.0.1:8000/](http://127.0.0.1:8000/), which redirects to the dashboard).

Without `DATABASE_URL`, local history is stored in `backend/sentinelpay.db`. That SQLite database is ignored by Git.

## 11. API

### `GET /`

**Purpose:** Redirect to the SentinelPay AI dashboard.

Example response: an HTTP redirect to `/frontend/`.

### `GET /frontend/`

**Purpose:** Serve the dashboard's HTML and static assets.

Example response: the SentinelPay AI web application.

### `GET /health`

**Purpose:** Simple service health check.

Example response:

```json
{ "status": "ok" }
```

### `POST /transaction/check`

**Purpose:** Analyze a transaction and return a Gemini or rule-based risk assessment.

Example request:

```json
{
  "amount": 25000,
  "sender": "user123",
  "receiver": "merchant456",
  "location": "Bhubaneswar",
  "device": "trusted",
  "velocity": 8
}
```

Example response:

```json
{
  "transaction": {
    "amount": 25000,
    "sender": "user123",
    "receiver": "merchant456",
    "location": "Bhubaneswar",
    "device": "trusted",
    "velocity": 8
  },
  "risk_score": 35,
  "risk_level": "LOW",
  "decision": "ALLOW",
  "explanation": "Rule-based fraud assessment because Gemini is unavailable.",
  "analysis_source": "rule_based",
  "provider": "rule_based_fallback"
}
```

`analysis_source` is `gemini` only after a valid Gemini response; otherwise it is `rule_based`. The `provider` field is `gemini` or `rule_based_fallback` and identifies which assessment generated the response.

### `GET /transactions`

**Purpose:** Return up to 50 persisted transactions, newest first.

Example response:

```json
[
  {
    "id": 1,
    "amount": 25000.0,
    "sender": "user123",
    "receiver": "merchant456",
    "location": "Bhubaneswar",
    "device": "trusted",
    "velocity": 8,
    "risk_score": 35,
    "risk_level": "LOW",
    "decision": "ALLOW",
    "ai_explanation": "Rule-based fraud assessment because Gemini is unavailable.",
    "analysis_source": "rule_based",
    "created_at": "2026-01-01 12:00:00"
  }
]
```

When Vercel has no usable PostgreSQL `DATABASE_URL`, this endpoint returns an empty list because persistence is disabled safely.

### `GET /transactions/{id}`

**Purpose:** Return one persisted transaction by numeric ID.

Example request: `GET /transactions/1`

Example response: one transaction object in the format returned by `GET /transactions`. If the record does not exist, the API returns `404`.

## 12. Deployment

The project is configured as one Vercel deployment. `vercel.json` includes the `frontend/**` files with the `app.py` Python function; `app.py` imports `backend.main:app`.

1. In [Vercel](https://vercel.com/new), choose **Add New** > **Project** and import `binayofficial10-art/sentinelpay-ai`.
2. Keep the repository root as the project root. Vercel installs `requirements.txt` and runs the Python entry point in `app.py`.
3. In **Settings** > **Environment Variables**, add `GEMINI_API_KEY` for Production and/or Preview. Optionally set `GEMINI_MODEL=gemini-3.7-flash`.
4. For persistent production history, configure a PostgreSQL `DATABASE_URL`. Without it, analysis endpoints continue to work but history is not persisted in Vercel's serverless filesystem.
5. Deploy the project.
6. Test `https://<your-domain>/health`, `https://<your-domain>/`, and `https://<your-domain>/frontend/`.
7. Submit the transaction form from both a phone and laptop browser. Confirm the displayed result identifies Gemini or the fallback source.

The included frontend calls the same HTTPS origin. For a deliberately separate frontend host, set its HTTPS origin in `CORS_ALLOWED_ORIGINS` and configure the `sentinelpay-api-base-url` metadata value in the frontend; do not use wildcard CORS for that deployment.

## 13. Testing

Automated API tests are in `tests/test_transaction_check.py`. They cover a successful Gemini assessment, exhausted Gemini `429` and `503` retries falling back successfully, a missing Gemini key, a database write failure, and Vercel's no-SQLite persistence guard. During deployment verification, also use the following checks:

- Submit a low-risk transaction and inspect the displayed assessment.
- Submit a medium-risk transaction and inspect the displayed assessment.
- Submit a high-risk transaction and inspect the displayed assessment.
- With a valid configured Gemini key, verify that a valid Gemini response reports `analysis_source: "gemini"`.
- Without a key, with an unavailable Gemini service, or with a quota/rate-limit response, verify that the request still returns an assessment with `analysis_source: "rule_based"`.
- After deployment, verify `/health`, `/frontend/`, `POST /transaction/check`, and history behavior against the configured database.

## 14. Error Handling

- If `GEMINI_API_KEY` is absent, the backend uses the rule-based assessment.
- Gemini HTTP, network, malformed-response, invalid-model, authentication, and quota/rate-limit failures are logged server-side with useful diagnostic details but without the API key.
- Gemini `429`, `500`, `502`, `503`, and `504` failures use a small bounded retry count with exponential backoff and jitter. If Gemini still fails, the backend returns the rule-based fallback.
- The `/transaction/check` endpoint keeps a completed assessment even if saving transaction history fails.
- If a configured history database cannot be reached, history endpoints return a `503` response. On Vercel without an external database, history is safely disabled and returns no records rather than using local SQLite storage.
- Standard FastAPI/Pydantic validation rejects invalid transaction request fields.

## 15. Security Notes

- Never commit API keys, passwords, tokens, certificates, or `.env` files.
- Store Gemini and database credentials in local ignored environment files or the Vercel environment-variable settings.
- Do not expose Gemini credentials to JavaScript, HTML, browser network requests, or application logs.
- Validate and sanitize all production-facing input according to the application's risk and privacy requirements.
- Protect production endpoints with appropriate authentication, authorization, rate limiting, monitoring, and audit controls before handling real financial data.

## 16. Project Structure

```text
sentinelpay-ai/
├── .env.example              # Safe environment-variable template
├── .gitignore                # Excludes secrets, environments, caches, and local databases
├── .python-version           # Python version for deployment/runtime tools
├── app.py                    # Vercel FastAPI entry point
├── requirements.txt          # Python dependencies
├── vercel.json               # Vercel function configuration
├── render.yaml               # Render deployment configuration
├── backend/
│   ├── database.py           # SQLite/PostgreSQL transaction-history layer
│   └── main.py               # FastAPI routes and Gemini/fallback analysis
└── frontend/
    ├── index.html            # Dashboard markup
    ├── script.js             # Form submission, results, and history logic
    └── style.css             # Responsive dashboard styling
```

## 17. Future Improvements

The following are future improvements, not current features:

- Authentication and role-based access controls
- Additional, validated fraud signals and model evaluation against labeled data
- A managed persistent production database with retention and privacy controls
- Monitoring, metrics, alerting, and structured audit logs
- API rate limiting and abuse protection
- Analytics and review dashboards
- Human-review workflows and notifications

## 18. Demo Flow

1. Open the SentinelPay AI dashboard at `/frontend/`.
2. Enter a transaction amount, parties, location, device status, and velocity.
3. Select **Check Transaction**.
4. Explain the returned score, risk level, decision, explanation, and Gemini/fallback source.
5. Show the recent transaction history, or explain that production history requires a configured PostgreSQL database on Vercel.
