"""Complete staging-only API write suite using a local real-HTTP subprocess."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import sys
from threading import Barrier
import uuid
from typing import Any

from tests.staging_http_harness import HttpResponse, IntegrationFailure, StagingHttpHarness


class Suite:
    def __init__(self, harness: StagingHttpHarness) -> None:
        self.harness, self.passed, self.failed, self.skipped = harness, 0, 0, 0

    def check(self, step: str, condition: bool, detail: str = "") -> None:
        if not condition:
            self.failed += 1
            raise IntegrationFailure(f"assertion={step} {detail}")
        self.passed += 1
        self.harness.emit(f"PASS step={step}")

    def expect(self, step: str, response: HttpResponse, status: int) -> Any:
        self.check(step, response.status == status, f"expected_http={status} actual_http={response.status} body={response.body[:1000]}")
        return response.json_body


def transaction(marker: str, name: str, **changes: Any) -> dict[str, Any]:
    value = {"amount": "10.00", "sender": marker, "receiver": "staging-merchant", "location": "Delhi", "device": "trusted", "velocity": 0, "transaction_id": f"{marker}-{name}"}
    value.update(changes)
    return value


def session_cookie(opener: Any) -> str:
    """Read the test client's already-authenticated session without logging it."""
    for handler in opener.handlers:
        cookie_jar = getattr(handler, "cookiejar", None)
        if cookie_jar is not None:
            for cookie in cookie_jar:
                if cookie.name == "sentinelpay_session":
                    return f"{cookie.name}={cookie.value}"
    raise IntegrationFailure("authenticated analyst session cookie is unavailable")


def concurrent_idempotency_bursts(
    harness: StagingHttpHarness, suite: Suite, analyst_client: Any, marker: str, user_id: int
) -> None:
    """Exercise two bounded same-key request races over the real HTTP transport."""
    import psycopg

    concurrency = 5
    harness.emit(f"START step=concurrent_idempotency_bursts concurrency={concurrency} rounds=2")
    cookie = session_cookie(analyst_client)
    for round_number in (1, 2):
        key = f"{marker}-concurrent-{round_number}"
        source = f"{marker}-concurrent-source-{round_number}"
        payload = transaction(marker, f"concurrent-{round_number}")
        barrier = Barrier(concurrency)

        def submit(worker: int) -> HttpResponse | Exception:
            try:
                barrier.wait(timeout=10)
                return harness.request(
                    f"concurrent_idempotency_round_{round_number}_worker_{worker}",
                    "POST",
                    "/transaction/check",
                    json_body=payload,
                    headers={"Cookie": cookie, "X-SentinelPay-Test-Source": source},
                )
            except Exception as error:  # Captured and asserted below; never hidden.
                return error

        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="sentinelpay-idempotency") as workers:
            responses = list(workers.map(submit, range(concurrency), timeout=60))

        suite.check(
            f"concurrent_idempotency_round_{round_number}_no_request_failures",
            all(isinstance(response, HttpResponse) for response in responses),
            repr([response for response in responses if isinstance(response, Exception)]),
        )
        typed_responses = [response for response in responses if isinstance(response, HttpResponse)]
        suite.check(
            f"concurrent_idempotency_round_{round_number}_all_successful",
            all(response.status == 200 for response in typed_responses),
            repr([(response.status, response.body[:300]) for response in typed_responses if response.status != 200]),
        )
        bodies = [response.json_body for response in typed_responses]
        suite.check(
            f"concurrent_idempotency_round_{round_number}_responses_consistent",
            all(
                body["transaction"]["transaction_id"] == key
                and body["risk_score"] == bodies[0]["risk_score"]
                and body["decision"] == bodies[0]["decision"]
                for body in bodies
            ),
        )
        with psycopg.connect(harness.staging_url) as connection:
            transaction_count, hash_count, alert_count = connection.execute(
                "SELECT count(*), count(DISTINCT idempotency_request_hash), "
                "(SELECT count(*) FROM alerts a JOIN transactions t ON t.id=a.transaction_id "
                " WHERE t.user_id=%s AND t.idempotency_key=%s) "
                "FROM transactions WHERE user_id=%s AND idempotency_key=%s",
                (user_id, key, user_id, key),
            ).fetchone()
            audit_count = connection.execute(
                "SELECT count(*) FROM audit_events WHERE user_id=%s AND event_type='transaction_check' AND source_hash=%s",
                (user_id, source),
            ).fetchone()[0]
        suite.check(f"concurrent_idempotency_round_{round_number}_one_durable_transaction", transaction_count == 1, f"rows={transaction_count}")
        suite.check(f"concurrent_idempotency_round_{round_number}_one_idempotency_hash", hash_count == 1, f"hashes={hash_count}")
        suite.check(f"concurrent_idempotency_round_{round_number}_no_duplicate_alert", alert_count == 0, f"alerts={alert_count}")
        suite.check(f"concurrent_idempotency_round_{round_number}_one_business_audit", audit_count == 1, f"audit_rows={audit_count}")
        retry = harness.request(
            f"concurrent_idempotency_round_{round_number}_retry", "POST", "/transaction/check",
            json_body=payload, headers={"Cookie": cookie, "X-SentinelPay-Test-Source": source},
        )
        retry_body = suite.expect(f"concurrent_idempotency_round_{round_number}_retry", retry, 200)
        suite.check(
            f"concurrent_idempotency_round_{round_number}_retry_consistent",
            retry_body["transaction"]["transaction_id"] == key and retry_body["risk_score"] == bodies[0]["risk_score"],
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("affected", "full"), required=True)
    arguments = parser.parse_args()
    marker = f"sentinelpay-staging-it-{uuid.uuid4().hex}"
    harness = StagingHttpHarness(marker)
    suite = Suite(harness)
    exit_code = 1
    try:
        harness.verify_staging_schema()
        harness.start()
        harness.emit(f"MARKER {marker}")
        analyst_client = harness.new_client()
        viewer_client = harness.new_client()

        suite.expect("unauthenticated_transaction", harness.request("unauthenticated_transaction", "POST", "/transaction/check", json_body=transaction(marker, "unauth")), 401)
        suite.expect("analyst_registration", harness.request("analyst_registration", "POST", "/auth/register", json_body={"email": f"{marker}-analyst@example.test", "password": "correct-horse-battery-staple"}, opener=analyst_client), 201)
        import psycopg
        with psycopg.connect(harness.staging_url) as connection:
            connection.execute("UPDATE users SET role='analyst' WHERE email=%s", (f"{marker}-analyst@example.test",))
        analyst_login = suite.expect("analyst_login", harness.request("analyst_login", "POST", "/auth/login", json_body={"email": f"{marker}-analyst@example.test", "password": "correct-horse-battery-staple"}, headers={"X-SentinelPay-Test-Source": f"{marker}-analyst"}, opener=analyst_client), 200)
        suite.expect("invalid_credentials", harness.request("invalid_credentials", "POST", "/auth/login", json_body={"email": f"{marker}-analyst@example.test", "password": "wrong-password-value"}, headers={"X-SentinelPay-Test-Source": f"{marker}-invalid"}), 401)

        high = transaction(marker, "high", amount="50000.00", device="new", velocity=10)
        high_result = suite.expect("high_risk_create", harness.request("high_risk_create", "POST", "/transaction/check", json_body=high, opener=analyst_client), 200)
        suite.check("high_risk_response_schema", isinstance(high_result, dict) and {"transaction", "risk_score", "risk_level", "decision", "signals"}.issubset(high_result))
        suite.check("high_risk_policy", high_result["risk_level"] == "HIGH" and high_result["decision"] == "BLOCK" and 0 <= high_result["risk_score"] <= 100)
        history = suite.expect("history_after_create", harness.request("history_after_create", "GET", "/transactions", opener=analyst_client), 200)
        high_id = next(row["id"] for row in history if row["idempotency_key"] == high["transaction_id"])
        stored = suite.expect("transaction_read_after_write", harness.request("transaction_read_after_write", "GET", f"/transactions/{high_id}", opener=analyst_client), 200)
        suite.check("transaction_persisted", stored["amount"] == "50000.00" and stored["amount_minor"] == 5_000_000)
        replay = suite.expect("transaction_idempotency_replay", harness.request("transaction_idempotency_replay", "POST", "/transaction/check", json_body=high, opener=analyst_client), 200)
        suite.check("transaction_idempotency_same_result", replay["risk_score"] == high_result["risk_score"])
        suite.expect("transaction_idempotency_conflict", harness.request("transaction_idempotency_conflict", "POST", "/transaction/check", json_body={**high, "amount": "50001.00"}, opener=analyst_client), 409)
        concurrent_idempotency_bursts(harness, suite, analyst_client, marker, analyst_login["user"]["id"])

        if arguments.scenario == "affected":
            harness.emit("PASS step=affected_real_http_integration_complete")
            exit_code = 0
            return exit_code

        low = suite.expect("low_risk_create", harness.request("low_risk_create", "POST", "/transaction/check", json_body=transaction(marker, "low"), opener=analyst_client), 200)
        suite.check("low_risk_policy", low["risk_level"] == "LOW" and low["decision"] == "ALLOW" and 0 <= low["risk_score"] <= 100)
        review_tx = transaction(marker, "review", amount="25000.00", device="new", velocity=5)
        review_result = suite.expect("review_risk_create", harness.request("review_risk_create", "POST", "/transaction/check", json_body=review_tx, opener=analyst_client), 200)
        suite.check("review_risk_policy", review_result["risk_level"] == "MEDIUM" and review_result["decision"] == "REVIEW")
        suite.check("risk_signals_consistent", sum(item["risk_contribution"] for item in review_result["signals"]) == review_result["risk_score"])

        suite.expect("malformed_json", harness.request("malformed_json", "POST", "/transaction/check", raw_body=b'{"amount":', opener=analyst_client), 422)
        suite.expect("missing_required_field", harness.request("missing_required_field", "POST", "/transaction/check", json_body={"amount": "1.00"}, opener=analyst_client), 422)
        suite.expect("invalid_field_type", harness.request("invalid_field_type", "POST", "/transaction/check", json_body=transaction(marker, "type", velocity="fast"), opener=analyst_client), 422)
        suite.expect("zero_amount", harness.request("zero_amount", "POST", "/transaction/check", json_body=transaction(marker, "zero", amount="0.00"), opener=analyst_client), 422)
        suite.expect("negative_amount", harness.request("negative_amount", "POST", "/transaction/check", json_body=transaction(marker, "negative", amount="-0.01"), opener=analyst_client), 422)
        suite.expect("extreme_amount_rejection", harness.request("extreme_amount_rejection", "POST", "/transaction/check", json_body=transaction(marker, "extreme", amount="1000000000000.00"), opener=analyst_client), 422)
        exact = suite.expect("exact_money_create", harness.request("exact_money_create", "POST", "/transaction/check", json_body=transaction(marker, "money", amount="0.10"), opener=analyst_client), 200)
        suite.check("exact_money_response", exact["transaction"]["amount"] == "0.10")
        suite.expect("exact_money_rejection", harness.request("exact_money_rejection", "POST", "/transaction/check", json_body=transaction(marker, "fraction", amount="0.001"), opener=analyst_client), 422)
        unknown = suite.expect("unknown_party_treated_as_data", harness.request("unknown_party_treated_as_data", "POST", "/transaction/check", json_body=transaction(marker, "unknown", sender="unknown-sender", receiver="unknown-receiver"), opener=analyst_client), 200)
        suite.check("unknown_party_persisted_as_data", unknown["transaction"]["receiver"] == "unknown-receiver")
        injection = suite.expect("sql_injection_treated_as_data", harness.request("sql_injection_treated_as_data", "POST", "/transaction/check", json_body=transaction(marker, "sql", sender="x'; DROP TABLE users;--"), opener=analyst_client), 200)
        suite.check("sql_injection_returned_as_data", injection["transaction"]["sender"] == "x'; DROP TABLE users;--")
        xss = suite.expect("xss_treated_as_data", harness.request("xss_treated_as_data", "POST", "/transaction/check", json_body=transaction(marker, "xss", receiver="<script>alert(1)</script>"), opener=analyst_client), 200)
        suite.check("xss_returned_as_data", xss["transaction"]["receiver"] == "<script>alert(1)</script>")
        oversized = harness.request("oversized_input", "POST", "/transaction/check", json_body=transaction(marker, "oversized", sender="x" * 256), opener=analyst_client)
        suite.expect("oversized_input", oversized, 422)
        suite.check("no_sensitive_exception_detail", "traceback" not in oversized.body.lower() and "postgres" not in oversized.body.lower())

        review_rows = suite.expect("history_for_review", harness.request("history_for_review", "GET", "/transactions", opener=analyst_client), 200)
        review_id = next(row["id"] for row in review_rows if row["idempotency_key"] == review_tx["transaction_id"])
        review_key = f"{marker}-review-action"
        reviewed = suite.expect("review_create", harness.request("review_create", "POST", f"/transactions/{review_id}/review", json_body={"decision": "APPROVE"}, headers={"Idempotency-Key": review_key}, opener=analyst_client), 200)
        suite.check("review_persisted", reviewed["review_decision"] == "APPROVE")
        suite.expect("review_idempotency_replay", harness.request("review_idempotency_replay", "POST", f"/transactions/{review_id}/review", json_body={"decision": "APPROVE"}, headers={"Idempotency-Key": review_key}, opener=analyst_client), 200)
        missing_key = f"{marker}-missing-review"
        suite.expect("missing_review_rollback", harness.request("missing_review_rollback", "POST", "/transactions/999999999/review", json_body={"decision": "APPROVE"}, headers={"Idempotency-Key": missing_key}, opener=analyst_client), 404)
        with psycopg.connect(harness.staging_url) as connection:
            remaining = connection.execute("SELECT count(*) FROM action_idempotency WHERE idempotency_key=%s", (missing_key,)).fetchone()[0]
        suite.check("missing_review_rollback_verified", remaining == 0, f"leftover_rows={remaining}")

        alerts = suite.expect("alerts_after_high_risk", harness.request("alerts_after_high_risk", "GET", "/alerts", opener=analyst_client), 200)
        suite.check("alert_persistence", bool(alerts))
        alert_key = f"{marker}-alert-action"
        suite.expect("alert_status_change", harness.request("alert_status_change", "POST", f"/alerts/{alerts[0]['id']}", json_body={"status": "ACKNOWLEDGED"}, headers={"Idempotency-Key": alert_key}, opener=analyst_client), 200)
        suite.expect("alert_idempotency_replay", harness.request("alert_idempotency_replay", "POST", f"/alerts/{alerts[0]['id']}", json_body={"status": "ACKNOWLEDGED"}, headers={"Idempotency-Key": alert_key}, opener=analyst_client), 200)

        viewer = suite.expect("viewer_registration", harness.request("viewer_registration", "POST", "/auth/register", json_body={"email": f"{marker}-viewer@example.test", "password": "correct-horse-battery-staple"}, headers={"X-SentinelPay-Test-Source": f"{marker}-viewer-register"}, opener=viewer_client), 201)
        suite.check("viewer_role", viewer["user"]["role"] == "viewer")
        suite.expect("viewer_transaction_create", harness.request("viewer_transaction_create", "POST", "/transaction/check", json_body=transaction(marker, "viewer"), opener=viewer_client), 200)
        suite.expect("viewer_rbac_rejection", harness.request("viewer_rbac_rejection", "POST", f"/transactions/{review_id}/review", json_body={"decision": "BLOCK"}, opener=viewer_client), 403)

        rate_headers = {"X-SentinelPay-Test-Source": f"{marker}-rate"}
        suite.expect("login_rate_limit_first_request", harness.request("login_rate_limit_first_request", "POST", "/auth/login", json_body={"email": f"{marker}-viewer@example.test", "password": "correct-horse-battery-staple"}, headers=rate_headers), 200)
        suite.expect("login_rate_limit_rejection", harness.request("login_rate_limit_rejection", "POST", "/auth/login", json_body={"email": f"{marker}-viewer@example.test", "password": "correct-horse-battery-staple"}, headers=rate_headers), 429)
        events = suite.expect("audit_event_read", harness.request("audit_event_read", "GET", "/audit-events", opener=analyst_client), 200)
        suite.check("audit_event_persistence", {"transaction_check", "manual_review", "alert_status_change"}.issubset({item["event_type"] for item in events}))
        suite.expect("server_usable_after_failures", harness.request("server_usable_after_failures", "GET", "/health", opener=analyst_client), 200)
        harness.emit("PASS step=full_real_http_staging_integration_complete")
        exit_code = 0
    except IntegrationFailure as error:
        harness.emit(f"FAIL step=integration detail={error}")
    except Exception as error:
        # The test runner must surface its own failures with context; it does
        # not treat them as application success or suppress their traceability.
        harness.emit(f"FAIL step=test_harness error_type={type(error).__name__} detail={error}")
    finally:
        try:
            harness.cleanup_marker_data()
        except Exception as error:
            harness.emit(f"FAIL step=marker_data_cleanup detail={error}")
            exit_code = 1
        harness.close()
        harness.emit(f"SUMMARY passed={suite.passed} failed={suite.failed} skipped={suite.skipped} requests={len(harness.requests)}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
