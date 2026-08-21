import json
import os
import socket
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi.responses import Response

import backend.main as main
from backend import auth, database, security
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
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        self.environment_patch.stop()
        self.database_patch.stop()
        self.temporary_directory.cleanup()


class TransactionCheckTests(DatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = auth.create_user("user@example.com", PASSWORD, role="analyst")
        token, _ = auth.create_session(self.user["id"])
        self.token = token
        self.client.cookies.set(main.SESSION_COOKIE_NAME, token)

    def post_transaction(self):
        return self.client.post("/transaction/check", json={**TRANSACTION, "transaction_id": f"txn-{uuid.uuid4().hex}"})

    def test_unauthenticated_transaction_request_returns_401(self):
        self.client.cookies.clear()
        self.assertEqual(self.post_transaction().status_code, 401)
        self.assertEqual(self.client.get("/transactions").status_code, 401)
        with database.get_connection() as connection:
            events = connection.execute("SELECT event_type FROM audit_events").fetchall()
        self.assertIn("unauthorized_request", [event["event_type"] for event in events])

    def test_gemini_success_returns_200_and_gemini_provider(self):
        with (patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "gemini-3.7-flash"}), patch("backend.main.request_gemini", return_value=gemini_response())):
            response = self.post_transaction()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["analysis_source"], "gemini")
        self.assertEqual(response.json()["provider"], "gemini")

    def test_deterministic_policy_cannot_be_weakened_by_ai_output(self):
        low_risk_ai = {"risk_score": 0, "risk_level": "LOW", "decision": "ALLOW", "explanation": "AI allowed it."}
        with (patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch("backend.main.request_gemini", return_value=json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(low_risk_ai)}]}}]}))):
            response = self.client.post("/transaction/check", json={**TRANSACTION, "amount": "50000.00", "velocity": 10, "device": "new", "transaction_id": "policy-floor-key"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"], "BLOCK")
        self.assertEqual(response.json()["risk_level"], "HIGH")

    def test_assessment_includes_real_structured_signals_and_processing_time(self):
        response = self.post_transaction()
        payload = response.json()
        self.assertIsInstance(payload["signals"], list)
        self.assertEqual(payload["signals"][0]["signal"], "Transaction amount")
        self.assertIsInstance(payload["processing_time_ms"], float)
        self.assertEqual(payload["analysis_mode"], "FALLBACK")
        transaction_id = self.client.get("/transactions").json()[0]["id"]
        detail = self.client.get(f"/transactions/{transaction_id}").json()
        self.assertEqual(detail["signals"][1]["signal"], "Transaction velocity")
        self.assertEqual(detail["confidence"], None)

    def test_transaction_check_persists_and_transactions_endpoint_returns_own_record(self):
        self.assertEqual(self.post_transaction().status_code, 200)
        response = self.client.get("/transactions")
        self.assertEqual(response.status_code, 200)
        history = response.json()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["user_id"], self.user["id"])
        self.assertEqual(history[0]["merchant"], TRANSACTION["receiver"])
        self.assertEqual(history[0]["provider"], "rule_based_fallback")

    def test_idempotency_key_returns_original_result_without_duplicate_transaction(self):
        payload = {**TRANSACTION, "transaction_id": "txn-idempotency-001"}
        first = self.client.post("/transaction/check", json=payload)
        second = self.client.post("/transaction/check", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["risk_score"], second.json()["risk_score"])
        self.assertEqual(len(self.client.get("/transactions").json()), 1)

    def test_idempotency_key_rejects_a_different_transaction_payload(self):
        key = "txn-idempotency-002"
        self.assertEqual(self.client.post("/transaction/check", json={**TRANSACTION, "transaction_id": key}).status_code, 200)
        changed = self.client.post("/transaction/check", json={**TRANSACTION, "amount": 50000, "transaction_id": key})
        self.assertEqual(changed.status_code, 409)

    def test_different_idempotency_keys_create_distinct_transactions(self):
        for key in ("txn-idempotency-003", "txn-idempotency-004"):
            self.assertEqual(self.client.post("/transaction/check", json={**TRANSACTION, "transaction_id": key}).status_code, 200)
        self.assertEqual(len(self.client.get("/transactions").json()), 2)

    def test_concurrent_duplicate_idempotency_requests_create_one_transaction(self):
        payload = {**TRANSACTION, "transaction_id": "txn-idempotency-concurrent"}
        def post_once():
            client = TestClient(main.app)
            client.cookies.set(main.SESSION_COOKIE_NAME, self.token)
            return client.post("/transaction/check", json=payload).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(lambda _: post_once(), range(2)))
        self.assertEqual(statuses, [200, 200])
        self.assertEqual(len(self.client.get("/transactions").json()), 1)

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
        self.assertEqual(self.client.post("/transaction/check", json={**TRANSACTION, "transaction_id": "bad key"}).status_code, 422)

    def test_transaction_idempotency_key_is_required(self):
        self.assertEqual(self.client.post("/transaction/check", json=TRANSACTION).status_code, 422)

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

    def test_malformed_gemini_response_falls_back_and_records_safe_reason(self):
        with (patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch("backend.main.request_gemini", return_value='{"candidates": []}')):
            response = self.post_transaction()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        events = self.client.get("/audit-events").json()
        event = next(item for item in events if item["event_type"] == "transaction_check")
        self.assertEqual(event["metadata"]["analysis_source"], "rule_based")
        self.assertEqual(event["metadata"]["fallback_reason"], "GeminiRequestError")

    def test_database_failure_fails_closed_without_a_success_response(self):
        with patch("backend.main.save_transaction", side_effect=DatabaseUnavailableError("unavailable")):
            response = self.post_transaction()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Transaction processing is temporarily unavailable.")

    def test_decimal_amounts_are_exact_and_json_safe(self):
        for amount, expected in (("0.10", "0.10"), ("0.30", "0.30"), ("999.99", "999.99"), ("999999999999.99", "999999999999.99")):
            response = self.client.post("/transaction/check", json={**TRANSACTION, "amount": amount, "transaction_id": f"money-{amount.replace('.', '-')}-key"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["transaction"]["amount"], expected)
        persisted = self.client.get("/transactions").json()
        self.assertEqual({item["amount"] for item in persisted}, {"0.10", "0.30", "999.99", "999999999999.99"})

    def test_float_artifact_and_invalid_money_amounts_are_rejected(self):
        for amount in (0, -1, "0.001", 0.1 + 0.2, "1000000000000.00"):
            self.assertEqual(self.client.post("/transaction/check", json={**TRANSACTION, "amount": amount, "transaction_id": "money-invalid-key"}).status_code, 422)

    def test_disabled_persistence_fails_closed_before_analysis(self):
        with patch("backend.main.persistence_enabled", return_value=False), patch("backend.main.generate_gemini_assessment") as assessment:
            response = self.post_transaction()
        self.assertEqual(response.status_code, 503)
        assessment.assert_not_called()

    def test_authenticated_history_database_failure_returns_503(self):
        with patch("backend.main.get_recent_transactions", side_effect=DatabaseUnavailableError("unavailable")):
            response = self.client.get("/transactions")
        self.assertEqual(response.status_code, 503)

    def test_transaction_rate_limit_returns_429_and_audits_violation(self):
        with patch.dict(os.environ, {"RATE_LIMIT_TRANSACTION_CHECK_MAX_REQUESTS": "1", "RATE_LIMIT_TRANSACTION_CHECK_WINDOW_SECONDS": "60"}, clear=False):
            self.assertEqual(self.post_transaction().status_code, 200)
            response = self.post_transaction()
        self.assertEqual(response.status_code, 429)
        self.assertTrue(response.headers.get("retry-after"))
        events = self.client.get("/audit-events").json()
        self.assertIn("transaction_check", [event["event_type"] for event in events])
        self.assertIn("rate_limit_violation", [event["event_type"] for event in events])

    def test_analytics_system_status_and_authorized_manual_review(self):
        self.assertEqual(self.post_transaction().status_code, 200)
        transaction_id = self.client.get("/transactions").json()[0]["id"]
        analytics = self.client.get("/analytics")
        self.assertEqual(analytics.status_code, 200)
        self.assertEqual(analytics.json()["total"], 1)
        self.assertEqual(self.client.get("/system-status").status_code, 200)
        reviewed = self.client.post(f"/transactions/{transaction_id}/review", json={"decision": "APPROVE"})
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.json()["review_decision"], "APPROVE")
        self.assertIn("manual_review", [event["event_type"] for event in self.client.get("/audit-events").json()])

    def test_manual_review_idempotency_prevents_duplicate_audit_actions(self):
        self.post_transaction()
        transaction_id = self.client.get("/transactions").json()[0]["id"]
        headers = {"Idempotency-Key": "action-review-idempotency-001"}
        first = self.client.post(f"/transactions/{transaction_id}/review", json={"decision": "APPROVE"}, headers=headers)
        second = self.client.post(f"/transactions/{transaction_id}/review", json={"decision": "APPROVE"}, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        events = [event for event in self.client.get("/audit-events").json() if event["event_type"] == "manual_review"]
        self.assertEqual(len(events), 1)

    def test_manual_review_cannot_access_another_users_transaction(self):
        self.post_transaction()
        transaction_id = self.client.get("/transactions").json()[0]["id"]
        other_user = auth.create_user("reviewer@example.com", PASSWORD, role="analyst")
        token, _ = auth.create_session(other_user["id"])
        other_client = TestClient(main.app)
        other_client.cookies.set(main.SESSION_COOKIE_NAME, token)
        self.assertEqual(other_client.post(f"/transactions/{transaction_id}/review", json={"decision": "BLOCK"}).status_code, 404)

    def test_viewer_cannot_make_manual_decision_or_change_alert_status(self):
        self.client.post("/transaction/check", json={**TRANSACTION, "device": "new", "velocity": 10, "transaction_id": "viewer-alert-key"})
        transaction_id = self.client.get("/transactions").json()[0]["id"]
        viewer = auth.create_user("viewer@example.com", PASSWORD)
        viewer_token, _ = auth.create_session(viewer["id"])
        viewer_client = TestClient(main.app)
        viewer_client.cookies.set(main.SESSION_COOKIE_NAME, viewer_token)
        self.assertEqual(viewer_client.post(f"/transactions/{transaction_id}/review", json={"decision": "BLOCK"}).status_code, 403)
        alert_id = self.client.get("/alerts").json()[0]["id"]
        self.assertEqual(viewer_client.post(f"/alerts/{alert_id}", json={"status": "RESOLVED"}).status_code, 403)

    def test_alerts_are_created_and_status_changes_are_authorized_and_audited(self):
        response = self.client.post("/transaction/check", json={**TRANSACTION, "device": "new", "velocity": 10, "transaction_id": "alert-workflow-key"})
        self.assertEqual(response.status_code, 200)
        alerts = self.client.get("/alerts")
        self.assertEqual(alerts.status_code, 200)
        alert_id = alerts.json()[0]["id"]
        updated = self.client.post(f"/alerts/{alert_id}", json={"status": "ACKNOWLEDGED"})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["status"], "ACKNOWLEDGED")
        self.assertIn("alert_status_change", [event["event_type"] for event in self.client.get("/audit-events").json()])


class AuthenticationTests(DatabaseTestCase):
    def test_role_values_match_the_database_role_allowlist(self):
        self.assertEqual(auth.create_user("analyst@example.com", PASSWORD, role="analyst")["role"], "analyst")
        self.assertEqual(auth.create_user("admin@example.com", PASSWORD, role="admin")["role"], "admin")
        with self.assertRaises(ValueError):
            auth.create_user("invalid-role@example.com", PASSWORD, role="operator")

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

    def test_frontend_api_origin_is_validated_and_allowed_by_csp(self):
        with patch.dict(os.environ, {"FRONTEND_API_BASE_URL": "https://api.example.com"}, clear=False):
            response = self.client.get("/health")
        self.assertIn("connect-src 'self' https://api.example.com", response.headers["content-security-policy"])
        with patch.dict(os.environ, {"FRONTEND_API_BASE_URL": "https://api.example.com/v1"}, clear=False):
            with self.assertRaises(ValueError):
                main.get_frontend_api_base_url()

    def test_security_headers_are_added_without_changing_api_response(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])

    def test_system_status_does_not_claim_gemini_is_online_from_key_presence(self):
        user = auth.create_user("status@example.com", PASSWORD)
        token, _ = auth.create_session(user["id"])
        self.client.cookies.set(main.SESSION_COOKIE_NAME, token)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "configured-not-verified"}, clear=False):
            response = self.client.get("/system-status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ai"]["status"], "AI ENGINE CONFIGURED")

    def test_register_login_me_and_logout(self):
        credentials = {"email": "account@example.com", "password": PASSWORD}
        registered = self.client.post("/auth/register", json=credentials)
        self.assertEqual(registered.status_code, 201)
        self.assertEqual(registered.json()["user"]["email"], credentials["email"])
        self.assertEqual(self.client.get("/auth/me").status_code, 200)
        self.assertEqual(self.client.post("/auth/logout").status_code, 204)
        self.assertEqual(self.client.get("/auth/me").status_code, 401)
        self.assertEqual(self.client.post("/auth/login", json=credentials).status_code, 200)
        events = self.client.get("/audit-events").json()
        self.assertIn("registration", [event["event_type"] for event in events])
        self.assertIn("logout", [event["event_type"] for event in events])
        self.assertIn("login", [event["event_type"] for event in events])

    def test_duplicate_registration_and_invalid_password_are_safe(self):
        credentials = {"email": "duplicate@example.com", "password": PASSWORD}
        self.assertEqual(self.client.post("/auth/register", json=credentials).status_code, 201)
        self.assertEqual(self.client.post("/auth/register", json=credentials).status_code, 409)
        self.assertEqual(self.client.post("/auth/login", json={**credentials, "password": "wrong-password-value"}).status_code, 401)
        with database.get_connection() as connection:
            events = connection.execute("SELECT event_type, success FROM audit_events WHERE event_type = 'login'").fetchall()
        self.assertEqual(events[-1]["success"], 0)

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

    def test_login_rate_limit_returns_429(self):
        with patch.dict(os.environ, {"RATE_LIMIT_LOGIN_MAX_REQUESTS": "2", "RATE_LIMIT_LOGIN_WINDOW_SECONDS": "60"}, clear=False):
            for _ in range(2):
                self.client.post("/auth/login", json={"email": "missing@example.com", "password": PASSWORD})
            response = self.client.post("/auth/login", json={"email": "missing@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 429)
        self.assertTrue(response.headers.get("retry-after"))

    def test_registration_rate_limit_returns_429(self):
        with patch.dict(os.environ, {"RATE_LIMIT_REGISTER_MAX_REQUESTS": "1", "RATE_LIMIT_REGISTER_WINDOW_SECONDS": "60"}, clear=False):
            self.assertEqual(self.client.post("/auth/register", json={"email": "first@example.com", "password": PASSWORD}).status_code, 201)
            response = self.client.post("/auth/register", json={"email": "second@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 429)

    def test_audit_events_are_isolated_per_user(self):
        first = self.client.post("/auth/register", json={"email": "first@example.com", "password": PASSWORD})
        self.assertEqual(first.status_code, 201)
        self.assertTrue(self.client.get("/audit-events").json())
        second = auth.create_user("second@example.com", PASSWORD)
        token, _ = auth.create_session(second["id"])
        other_client = TestClient(main.app)
        other_client.cookies.set(main.SESSION_COOKIE_NAME, token)
        self.assertEqual(other_client.get("/audit-events").json(), [])

    def test_rate_limit_database_failure_returns_safe_503(self):
        with patch("backend.security.consume_rate_limit", side_effect=DatabaseUnavailableError("unavailable")):
            response = self.client.post("/auth/login", json={"email": "missing@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 503)

    def test_audit_database_failure_does_not_break_login(self):
        credentials = {"email": "account@example.com", "password": PASSWORD}
        self.assertEqual(self.client.post("/auth/register", json=credentials).status_code, 201)
        self.assertEqual(self.client.post("/auth/logout").status_code, 204)
        with patch("backend.security.write_audit_event", side_effect=DatabaseUnavailableError("unavailable")):
            response = self.client.post("/auth/login", json=credentials)
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
