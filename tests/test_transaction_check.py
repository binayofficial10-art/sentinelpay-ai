import json
import os
import socket
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.main as main
from backend import database
from backend.database import DatabaseUnavailableError


TRANSACTION = {
    "amount": 25000,
    "sender": "user123",
    "receiver": "merchant456",
    "location": "Bhubaneswar",
    "device": "trusted",
    "velocity": 8,
}


def gemini_response() -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "risk_score": 42,
                                        "risk_level": "MEDIUM",
                                        "decision": "REVIEW",
                                        "explanation": "Gemini found a velocity risk signal.",
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }
    )


class TransactionCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def post_transaction(self):
        return self.client.post("/transaction/check", json=TRANSACTION)

    def test_gemini_success_returns_200_and_gemini_provider(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "gemini-3.7-flash"}),
            patch("backend.main.request_gemini", return_value=gemini_response()),
            patch("backend.main.save_transaction"),
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["analysis_source"], "gemini")
        self.assertEqual(response.json()["provider"], "gemini")

    def test_gemini_503_retries_then_returns_fallback_200(self):
        error = main.GeminiRequestError("service unavailable", status_code=503)
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch("backend.main.request_gemini", side_effect=error) as request_gemini,
            patch("backend.main.random.uniform", return_value=0),
            patch("backend.main.time.sleep") as sleep,
            patch("backend.main.save_transaction"),
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        self.assertEqual(request_gemini.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_gemini_429_retries_then_returns_fallback_200(self):
        error = main.GeminiRequestError("rate limited", status_code=429)
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch("backend.main.request_gemini", side_effect=error) as request_gemini,
            patch("backend.main.random.uniform", return_value=0),
            patch("backend.main.time.sleep") as sleep,
            patch("backend.main.save_transaction"),
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        self.assertEqual(request_gemini.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_retry_after_is_capped_to_keep_request_short(self):
        error = main.GeminiRequestError("rate limited", status_code=429, retry_after_seconds=120)

        self.assertEqual(main.gemini_retry_delay(0, error), main.GEMINI_MAX_RETRY_AFTER_SECONDS)

    def test_missing_gemini_key_returns_fallback_200(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}),
            patch("backend.main.save_transaction"),
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")

    def test_database_unavailable_does_not_change_successful_fallback_response(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}),
            patch(
                "backend.main.save_transaction",
                side_effect=DatabaseUnavailableError("database unavailable"),
            ),
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")

    def test_vercel_does_not_open_sqlite_for_transaction_persistence(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}),
            patch.object(database, "DATABASE_URL", ""),
            patch.dict(os.environ, {"VERCEL": "1"}, clear=False),
            patch("backend.database.sqlite3.connect") as sqlite_connect,
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        sqlite_connect.assert_not_called()

    def test_invalid_api_key_returns_fallback_without_retry(self):
        error = main.GeminiRequestError("unauthorized", status_code=401)
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch("backend.main.request_gemini", side_effect=error) as request_gemini,
            patch("backend.main.save_transaction"),
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        request_gemini.assert_called_once()

    def test_invalid_request_returns_fallback_without_retry(self):
        error = main.GeminiRequestError("bad request", status_code=400)
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch("backend.main.request_gemini", side_effect=error) as request_gemini,
            patch("backend.main.save_transaction"),
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        request_gemini.assert_called_once()

    def test_invalid_model_returns_fallback_without_retry(self):
        error = main.GeminiRequestError("not found", status_code=404)
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch("backend.main.request_gemini", side_effect=error) as request_gemini,
            patch("backend.main.save_transaction"),
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        request_gemini.assert_called_once()

    def test_timeout_returns_fallback_with_one_short_retry(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch("backend.main.request_gemini", side_effect=socket.timeout("timed out")) as request_gemini,
            patch("backend.main.time.sleep") as sleep,
            patch("backend.main.save_transaction"),
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        self.assertEqual(request_gemini.call_count, 2)
        sleep.assert_called_once()

    def test_malformed_model_value_returns_fallback_and_persists_transaction(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "models/gemini-3.7-flash"}),
            patch("backend.main.request_gemini") as request_gemini,
            patch("backend.main.save_transaction") as save_transaction,
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["analysis_source"], "rule_based")
        request_gemini.assert_not_called()
        self.assertEqual(save_transaction.call_args.args[0]["analysis_source"], "rule_based")

    def test_gemini_failure_uses_fallback_and_persists_transaction(self):
        error = main.GeminiRequestError("service unavailable", status_code=503)
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch("backend.main.request_gemini", side_effect=error),
            patch("backend.main.time.sleep"),
            patch("backend.main.save_transaction") as save_transaction,
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        self.assertEqual(save_transaction.call_args.args[0]["analysis_source"], "rule_based")


if __name__ == "__main__":
    unittest.main()
