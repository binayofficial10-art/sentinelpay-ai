import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi.responses import Response

import backend.main as main
from backend import auth, database
from backend.database import DatabaseUnavailableError


TRANSACTION = {"amount": 25000, "sender": "user123", "receiver": "merchant456", "location": "Bhubaneswar", "device": "trusted", "velocity": 8}
PASSWORD = "correct-horse-battery-staple"


def gemini_response() -> str:
    assessment = {"risk_score": 42, "risk_level": "MEDIUM", "decision": "REVIEW", "explanation": "Gemini found a velocity risk signal."}
    return json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(assessment)}]}}]})


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "sentinelpay-test.db"
        self.database_patch = patch.object(database, "DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
        self.environment_patch = patch.dict(os.environ, {"VERCEL": "", "VERCEL_ENV": "", "GEMINI_API_KEY": ""}, clear=False)
        self.database_patch.start()
        self.environment_patch.start()
        main._auth_attempts.clear()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        self.environment_patch.stop()
        self.database_patch.stop()
        self.temporary_directory.cleanup()


class TransactionCheckTests(DatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = auth.create_user("user@example.com", PASSWORD)
        token, _ = auth.create_session(self.user["id"])
        self.client.cookies.set(main.SESSION_COOKIE_NAME, token)

    def post_transaction(self):
        return self.client.post("/transaction/check", json=TRANSACTION)

    def test_unauthenticated_transaction_request_returns_401(self):
        self.client.cookies.clear()
        self.assertEqual(self.post_transaction().status_code, 401)
        self.assertEqual(self.client.get("/transactions").status_code, 401)

    def test_gemini_success_returns_200_and_gemini_provider(self):
        with (patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "gemini-3.7-flash"}), patch("backend.main.request_gemini", return_value=gemini_response())):
            response = self.post_transaction()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["analysis_source"], "gemini")
        self.assertEqual(response.json()["provider"], "gemini")

    def test_transaction_check_persists_and_transactions_endpoint_returns_own_record(self):
        self.assertEqual(self.post_transaction().status_code, 200)
        response = self.client.get("/transactions")
        self.assertEqual(response.status_code, 200)
        history = response.json()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["user_id"], self.user["id"])
        self.assertEqual(history[0]["merchant"], TRANSACTION["receiver"])
        self.assertEqual(history[0]["provider"], "rule_based_fallback")

    def test_user_cannot_retrieve_another_users_transaction(self):
        self.post_transaction()
        transaction_id = self.client.get("/transactions").json()[0]["id"]
        other_user = auth.create_user("other@example.com", PASSWORD)
        other_token, _ = auth.create_session(other_user["id"])
        other_client = TestClient(main.app)
        other_client.cookies.set(main.SESSION_COOKIE_NAME, other_token)
        self.assertEqual(other_client.get("/transactions").json(), [])
        self.assertEqual(other_client.get(f"/transactions/{transaction_id}").status_code, 404)

    def test_invalid_transaction_input_returns_422(self):
        self.assertEqual(self.client.post("/transaction/check", json={**TRANSACTION, "amount": -1}).status_code, 422)

    def test_client_supplied_user_id_is_rejected(self):
        response = self.client.post("/transaction/check", json={**TRANSACTION, "user_id": 999999})
        self.assertEqual(response.status_code, 422)

    def test_gemini_503_retries_then_returns_fallback_200(self):
        error = main.GeminiRequestError("service unavailable", status_code=503)
        with (patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch("backend.main.request_gemini", side_effect=error) as request_gemini, patch("backend.main.time.sleep") as sleep):
            response = self.post_transaction()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        self.assertEqual(request_gemini.call_count, main.GEMINI_MAX_RETRIES + 1)
        self.assertEqual(sleep.call_count, main.GEMINI_MAX_RETRIES)

    def test_gemini_429_retries_then_returns_fallback_200(self):
        error = main.GeminiRequestError("rate limited", status_code=429)
        with (patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch("backend.main.request_gemini", side_effect=error) as request_gemini, patch("backend.main.time.sleep") as sleep):
            response = self.post_transaction()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        self.assertEqual(request_gemini.call_count, main.GEMINI_MAX_RETRIES + 1)
        self.assertEqual(sleep.call_count, main.GEMINI_MAX_RETRIES)

    def test_timeout_and_network_failures_return_fallback(self):
        for error in (socket.timeout("timed out"), URLError("unavailable")):
            with (patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch("backend.main.request_gemini", side_effect=error), patch("backend.main.time.sleep")):
                response = self.post_transaction()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["provider"], "rule_based_fallback")

    def test_database_failure_does_not_crash_authenticated_analysis(self):
        with patch("backend.main.save_transaction", side_effect=DatabaseUnavailableError("unavailable")):
            response = self.post_transaction()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")

    def test_authenticated_history_database_failure_returns_503(self):
        with patch("backend.main.get_recent_transactions", side_effect=DatabaseUnavailableError("unavailable")):
            response = self.client.get("/transactions")
        self.assertEqual(response.status_code, 503)


class AuthenticationTests(DatabaseTestCase):
    def test_frontend_serves_the_authentication_shell(self):
        response = self.client.get("/frontend/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="loginForm"', response.text)
        self.assertIn('id="registerForm"', response.text)
        self.assertIn('id="logoutButton"', response.text)

    def test_cors_configuration_requires_explicit_https_origins_in_production(self):
        with patch.dict(os.environ, {"VERCEL": "1", "CORS_ALLOWED_ORIGINS": "https://app.example.com/"}, clear=False):
            self.assertEqual(main.get_cors_allowed_origins(), ["https://app.example.com"])
        with patch.dict(os.environ, {"VERCEL": "1", "CORS_ALLOWED_ORIGINS": "*"}, clear=False):
            with self.assertRaises(ValueError):
                main.get_cors_allowed_origins()
        with patch.dict(os.environ, {"VERCEL": "1", "CORS_ALLOWED_ORIGINS": "http://app.example.com"}, clear=False):
            with self.assertRaises(ValueError):
                main.get_cors_allowed_origins()

    def test_frontend_config_exposes_only_the_optional_public_api_origin(self):
        with patch.dict(os.environ, {"FRONTEND_API_BASE_URL": "https://api.example.com/", "DATABASE_URL": "sensitive-value"}, clear=False):
            response = self.client.get("/frontend-config.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn('window.SENTINELPAY_API_BASE_URL = "https://api.example.com"', response.text)
        self.assertNotIn("sensitive-value", response.text)

    def test_register_login_me_and_logout(self):
        credentials = {"email": "account@example.com", "password": PASSWORD}
        registered = self.client.post("/auth/register", json=credentials)
        self.assertEqual(registered.status_code, 201)
        self.assertEqual(registered.json()["user"]["email"], credentials["email"])
        self.assertEqual(self.client.get("/auth/me").status_code, 200)
        self.assertEqual(self.client.post("/auth/logout").status_code, 204)
        self.assertEqual(self.client.get("/auth/me").status_code, 401)
        self.assertEqual(self.client.post("/auth/login", json=credentials).status_code, 200)

    def test_duplicate_registration_and_invalid_password_are_safe(self):
        credentials = {"email": "duplicate@example.com", "password": PASSWORD}
        self.assertEqual(self.client.post("/auth/register", json=credentials).status_code, 201)
        self.assertEqual(self.client.post("/auth/register", json=credentials).status_code, 409)
        self.assertEqual(self.client.post("/auth/login", json={**credentials, "password": "wrong-password-value"}).status_code, 401)

    def test_invalid_registration_input_returns_422(self):
        response = self.client.post("/auth/register", json={"email": "not-an-email", "password": "short"})
        self.assertEqual(response.status_code, 422)

    def test_production_session_cookie_is_secure_and_http_only(self):
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=False):
            response = Response()
            main.set_session_cookie(response, "test-session-token")
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=lax", cookie)

    def test_authentication_rate_limit_returns_429(self):
        for _ in range(main.AUTH_RATE_LIMIT):
            self.client.post("/auth/login", json={"email": "missing@example.com", "password": PASSWORD})
        response = self.client.post("/auth/login", json={"email": "missing@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 429)


if __name__ == "__main__":
    unittest.main()
