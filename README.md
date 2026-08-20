# SentinelPay AI

SentinelPay AI is a portfolio demonstration of transaction fraud-risk assessment. It accepts a transaction, evaluates risk signals, and presents a risk score, level, decision, explanation, live statistics, and recent history in a responsive web interface.

> **Prototype warning:** This project is for demonstration and learning only. It is not a production banking system and must not be used as the sole basis for fraud, credit, payments, or other high-impact decisions.

## Problem and solution

Manual transaction review is slow and difficult to scale. SentinelPay AI demonstrates a single web service that collects basic transaction signals (amount, velocity, device, and location), evaluates them with Gemini when available, and safely falls back to transparent rule-based scoring if the AI service is unavailable.

## Features

- Responsive transaction-analysis dashboard for desktop and mobile browsers
- Form validation and a single guarded submit flow (button click or Enter)
- `POST /transaction/check` API with structured validation
- Gemini-powered assessment when `GEMINI_API_KEY` is configured and the service succeeds
- Deterministic rule-based fallback when Gemini is unavailable
- Clear frontend indication of Gemini versus fallback assessments
- Risk score, risk level, decision, explanation, statistics, and transaction history
- Same-origin frontend/API deployment from one FastAPI service
- Health endpoint for deployment monitoring

## Architecture

```text
Browser
  │ GET /frontend/
  ▼
FastAPI + StaticFiles
  │ POST /transaction/check
  ├── Gemini API (optional)
  └── Rule-based fallback
```

## Tech stack

- Python, FastAPI, Uvicorn
- Pydantic request validation
- Google Gen AI SDK (Gemini)
- HTML, CSS, vanilla JavaScript

## Local setup

From the repository root:

```powershell
py -3.12 -m venv backend\venv
.\backend\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example backend\.env
```

Set `GEMINI_API_KEY` in `backend/.env` if you want Gemini analysis. It is optional; without it, the application remains fully usable with the rule-based fallback.

Start the service:

```powershell
Set-Location backend
.\venv\Scripts\python.exe -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) or [http://127.0.0.1:8000/frontend/](http://127.0.0.1:8000/frontend/). For local use, start FastAPI as shown above so the frontend and API share one origin.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | No | Gemini API credential. Keep it only in `backend/.env` locally or your deployment secret manager. |
| `GEMINI_MODEL` | No | Gemini model name; defaults to `gemini-3.6-flash`. |
| `PORT` | Local server only | Port supplied to Uvicorn when running locally. Vercel manages the production runtime. |
| `CORS_ALLOWED_ORIGINS` | No | Comma-separated HTTPS frontend origins. Leave empty for the included same-origin frontend. |

Never put `GEMINI_API_KEY` in frontend code, documentation examples, or Git commits.

## API

### `POST /transaction/check`

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

Example response fields:

```json
{
  "transaction": { "amount": 25000, "sender": "user123", "receiver": "merchant456", "location": "Bhubaneswar", "device": "trusted", "velocity": 8 },
  "risk_score": 35,
  "risk_level": "LOW",
  "decision": "ALLOW",
  "explanation": "Rule-based fraud assessment because Gemini is unavailable.",
  "analysis_source": "rule_based"
}
```

`analysis_source` is `gemini` only after a valid Gemini response; otherwise it is `rule_based`.

### Other endpoints

- `GET /` redirects to the frontend.
- `GET /frontend/` serves the web application.
- `GET /health` returns `{ "status": "ok" }`.

## Risk scoring

The fallback score is intentionally simple and explainable: higher amounts, rapid transaction velocity, and non-trusted devices add risk points. Scores map to `LOW`/`ALLOW`, `MEDIUM`/`REVIEW`, or `HIGH`/`BLOCK`. These thresholds are demo rules, not validated fraud models.

## Deployment on Vercel

The repository deploys as one Vercel FastAPI application. `api/index.py` exposes the existing `backend.main:app`; FastAPI serves both `/frontend/` and the API from the same Vercel HTTPS origin. The browser therefore posts to `/transaction/check` on its current origin, with no production localhost URL and no CORS configuration required for the included frontend.

### Vercel deployment steps

1. Sign in at [Vercel](https://vercel.com/new), select **Add New** > **Project**, and import the GitHub repository `binayofficial10-art/sentinelpay-ai`.
2. Leave the project root as the repository root. Vercel detects `api/index.py`, installs the root `requirements.txt`, and uses Python `3.12` from `.python-version`.
3. In **Settings** > **Environment Variables**, add `GEMINI_API_KEY` only if Gemini analysis is desired. Mark it for the desired Production/Preview environments; do not put it in source control or a frontend setting.
4. Optionally add `GEMINI_MODEL` to select a supported Gemini model. If no key is supplied, the app safely uses its rule-based assessment.
5. Click **Deploy**. On later updates, Vercel deploys new commits from the connected repository.
6. Open `https://<your-vercel-domain>/health` and confirm it returns `{"status":"ok"}`.
7. Open `https://<your-vercel-domain>/frontend/` on both a phone and a laptop. Submit the transaction form and confirm it returns an assessment.

For a deliberately separate frontend host, set the `sentinelpay-api-base-url` meta tag in `frontend/index.html` to the API's HTTPS URL and set `CORS_ALLOWED_ORIGINS` in Vercel to that exact frontend HTTPS origin. Do not use `*` for CORS.

## Usage

1. Open the dashboard at `/frontend/`.
2. Enter the transaction amount, parties, location, device status, and velocity.
3. Select **Check Transaction**.
4. Review the score, decision, explanation, and whether Gemini or fallback scoring supplied the result.

## Limitations

- No real payment provider, transaction feed, or persistent database
- No authentication, authorization, audit log, rate limiting, or model monitoring
- Demo scoring inputs are limited and not calibrated against real fraud outcomes
- Browser history and summary statistics reset after refresh

## Future roadmap

- Add unit and API test coverage
- Add authenticated dashboards, role-based access, rate limiting, and audit logs
- Store encrypted transaction history with privacy controls and retention policies
- Evaluate models against labeled data and monitor drift, bias, and performance
- Add human-review queues, alerts, and observability

## Security notes

`.gitignore` excludes local `.env` files, credentials, virtual environments, caches, dependencies, and build output. Before every commit, review `git status` and `git diff --cached`; never stage keys, tokens, passwords, or private credentials.
