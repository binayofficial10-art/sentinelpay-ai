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
| `PORT` | Production platform | Port supplied by Render or another hosting provider. |
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

## Deployment on Render

`render.yaml` is included for a single FastAPI web service. It serves both the dashboard and API from the same public HTTPS origin, so the browser calls `/transaction/check` without a cross-origin request. Connect the GitHub repository, create a Blueprint service, and set `GEMINI_API_KEY` as a secret environment variable in Render if Gemini is desired. The service uses:

```text
cd backend && uvicorn main:app --host 0.0.0.0 --port $PORT
```

Render health check path: `/health`.

### Render deployment steps

1. In Render, select **New** > **Blueprint** and connect `binayofficial10-art/sentinelpay-ai`.
2. Confirm the generated web service uses the repository `render.yaml`; Render installs `requirements.txt` and binds Uvicorn to its supplied `PORT`.
3. Leave `GEMINI_API_KEY` unset to use explainable rule-based scoring, or add it in Render's **Environment** settings as a secret. Never place it in GitHub or frontend files.
4. Deploy and open `https://<your-render-service>.onrender.com/frontend/`. Check `https://<your-render-service>.onrender.com/health` returns `{"status":"ok"}`.
5. Submit a transaction in the dashboard and confirm the result is returned by `POST /transaction/check` on the same URL origin.

For a deliberately separate frontend host, set the `sentinelpay-api-base-url` meta tag in `frontend/index.html` to the API's HTTPS URL and set `CORS_ALLOWED_ORIGINS` in Render to the exact frontend HTTPS origin. Do not use `*` for CORS.

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
