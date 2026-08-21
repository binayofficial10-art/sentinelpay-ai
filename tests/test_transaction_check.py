import json
import os
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
            patch.object(main, "api_key", "test-key"),
            patch.object(main, "gemini_model", "gemini-3.7-flash"),
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
            patch.object(main, "api_key", "test-key"),
            patch("backend.main.request_gemini", side_effect=error) as request_gemini,
            patch("backend.main.random.uniform", return_value=0),
            patch("backend.main.time.sleep") as sleep,
            patch("backend.main.save_transaction"),
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        self.assertEqual(request_gemini.call_count, main.GEMINI_MAX_RETRIES + 1)
        self.assertEqual(sleep.call_count, main.GEMINI_MAX_RETRIES)

    def test_gemini_429_retries_then_returns_fallback_200(self):
        error = main.GeminiRequestError("rate limited", status_code=429)
        with (
            patch.object(main, "api_key", "test-key"),
            patch("backend.main.request_gemini", side_effect=error) as request_gemini,
            patch("backend.main.random.uniform", return_value=0),
            patch("backend.main.time.sleep") as sleep,
            patch("backend.main.save_transaction"),
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        self.assertEqual(request_gemini.call_count, main.GEMINI_MAX_RETRIES + 1)
        self.assertEqual(sleep.call_count, main.GEMINI_MAX_RETRIES)

    def test_missing_gemini_key_returns_fallback_200(self):
        with (
            patch.object(main, "api_key", None),
            patch("backend.main.save_transaction"),
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")

    def test_database_unavailable_does_not_change_successful_fallback_response(self):
        with (
            patch.object(main, "api_key", None),
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
            patch.object(main, "api_key", None),
            patch.object(database, "DATABASE_URL", ""),
            patch.dict(os.environ, {"VERCEL": "1"}, clear=False),
            patch("backend.database.sqlite3.connect") as sqlite_connect,
        ):
            response = self.post_transaction()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "rule_based_fallback")
        sqlite_connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
