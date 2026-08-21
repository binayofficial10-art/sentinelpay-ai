"""Real-HTTP diagnostic for the affected staging transaction flow only."""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
from datetime import datetime, timezone
from urllib.parse import urlsplit
import urllib.request
import uuid
from pathlib import Path
from typing import Any


REQUEST_TIMEOUT_SECONDS = 45.0
ROOT = Path(__file__).resolve().parent.parent
CLIENT_LOG: Any | None = None


def emit(message: str) -> None:
    print(message, flush=True)
    if CLIENT_LOG is not None:
        CLIENT_LOG.write(f"{message}\n")
        CLIENT_LOG.flush()


def fail(step: str, detail: str) -> None:
    emit(f"FAIL step={step} detail={detail}")
    raise RuntimeError(detail)


def request(opener: urllib.request.OpenerDirector, step: str, method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    path = urlsplit(url).path
    started_at_utc = datetime.now(timezone.utc).isoformat()
    emit(
        f"START step={step} method={method} path={path} started_at={started_at_utc} "
        f"timeout_seconds={REQUEST_TIMEOUT_SECONDS}",
    )
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    http_request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    started = time.perf_counter()
    try:
        with opener.open(http_request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status, raw = response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read().decode("utf-8", errors="replace")
    except TimeoutError:
        elapsed = time.perf_counter() - started
        emit(
            f"END step={step} method={method} path={path} completion_at={datetime.now(timezone.utc).isoformat()} "
            f"error=timeout elapsed_seconds={elapsed:.3f} timeout_seconds={REQUEST_TIMEOUT_SECONDS}"
        )
        fail(step, f"timeout method={method} path={path} elapsed_seconds={elapsed:.3f}")
    except urllib.error.URLError as error:
        elapsed = time.perf_counter() - started
        emit(
            f"END step={step} method={method} path={path} completion_at={datetime.now(timezone.utc).isoformat()} "
            f"error_type={type(error.reason).__name__} elapsed_seconds={elapsed:.3f}"
        )
        fail(step, f"network_error method={method} path={path} error={error.reason} elapsed_seconds={elapsed:.3f}")
    elapsed = time.perf_counter() - started
    safe_body = re.sub(r"postgres(?:ql)?://[^\s]+", "[REDACTED_DATABASE_URL]", raw)
    emit(
        f"END step={step} method={method} path={path} status={status} completion_at={datetime.now(timezone.utc).isoformat()} "
        f"elapsed_seconds={elapsed:.3f} response_body={safe_body[:2000]}"
    )
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        fail(step, f"invalid_json_response method={method} path={path} status={status}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    global CLIENT_LOG
    staging_url = os.environ.get("STAGING_DATABASE_URL", "").strip()
    if not staging_url:
        fail("staging_environment", "STAGING_DATABASE_URL is unavailable")

    import psycopg
    from backend.auth import hash_password

    marker = f"sentinelpay-staging-http-{uuid.uuid4().hex}"
    with psycopg.connect(staging_url) as connection:
        connection.execute("BEGIN READ ONLY")
        ready = connection.execute(
            "SELECT to_regclass('public.users') IS NOT NULL "
            "AND to_regclass('public.transactions') IS NOT NULL"
        ).fetchone()[0]
        connection.rollback()
    if not ready:
        fail("staging_schema", "required staging schema is unavailable")
    client_log_path = Path(tempfile.gettempdir()) / f"{marker}.client.log"
    CLIENT_LOG = client_log_path.open("w", encoding="utf-8")
    emit(f"CLIENT_LOG_FILE {client_log_path}")
    emit("PASS step=staging_target_and_schema_verified")
    emit(f"MARKER {marker}")

    password = "correct-horse-battery-staple"
    email = f"{marker}-analyst@example.test"
    with psycopg.connect(staging_url) as connection:
        connection.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (%s, %s, 'analyst')",
            (email, hash_password(password)),
        )
    emit("PASS step=analyst_fixture_created")

    port = free_port()
    server_environment = os.environ.copy()
    server_environment["DATABASE_URL"] = staging_url
    # load_dotenv() does not overwrite an existing blank process value.
    server_environment["GEMINI_API_KEY"] = ""
    server_environment["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")
    server_log_path = Path(tempfile.gettempdir()) / f"{marker}.server.log"
    server_log = server_log_path.open("w", encoding="utf-8")
    emit(f"SERVER_LOG_FILE {server_log_path}")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.staging_diagnostic_app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=server_environment,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        while True:
            try:
                health_status, _ = request(opener, "local_server_health", "GET", f"{base_url}/health")
                if health_status == 200:
                    break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        login_status, _ = request(
            opener, "analyst_login", "POST", f"{base_url}/auth/login", {"email": email, "password": password}
        )
        if login_status != 200:
            fail("analyst_login", f"expected_http=200 actual_http={login_status}")
        transaction = {
            "amount": "50000.00", "sender": marker, "receiver": "staging-merchant",
            "location": "Delhi", "device": "new", "velocity": 10,
            "transaction_id": f"{marker}-high",
        }
        create_status, _ = request(opener, "transaction_create", "POST", f"{base_url}/transaction/check", transaction)
        if create_status != 200:
            fail("transaction_create", f"expected_http=200 actual_http={create_status}")
        replay_status, _ = request(opener, "transaction_idempotency_replay", "POST", f"{base_url}/transaction/check", transaction)
        if replay_status != 200:
            fail("transaction_idempotency_replay", f"expected_http=200 actual_http={replay_status}")
        emit("PASS step=affected_real_http_integration_complete")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        server_log.close()
        for line in server_log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            safe_line = re.sub(r"postgres(?:ql)?://[^\s]+", "[REDACTED_DATABASE_URL]", line)
            emit(f"SERVER_LOG {safe_line}")
        emit("PASS step=local_test_server_terminated")
        CLIENT_LOG.close()


if __name__ == "__main__":
    sys.exit(main())
