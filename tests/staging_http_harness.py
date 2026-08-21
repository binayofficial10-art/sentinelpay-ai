"""Reusable real-HTTP harness for explicit, staging-only integration runs."""

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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


ROOT = Path(__file__).resolve().parent.parent
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 45.0
WRITE_TIMEOUT_SECONDS = 45.0
REQUEST_TIMEOUT_SECONDS = 45.0


class IntegrationFailure(RuntimeError):
    """A bounded, observable integration failure."""


@dataclass
class HttpResponse:
    status: int
    body: str
    json_body: Any | None
    elapsed_seconds: float


def redact(value: str) -> str:
    return re.sub(r"postgres(?:ql)?://[^\s]+", "[REDACTED_DATABASE_URL]", value)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class StagingHttpHarness:
    """Owns one local Uvicorn child and marker-isolated staging test data."""

    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.staging_url = os.environ.get("STAGING_DATABASE_URL", "").strip()
        if not self.staging_url:
            raise IntegrationFailure("STAGING_DATABASE_URL is unavailable in this process")
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.server: subprocess.Popen[str] | None = None
        self.server_log_path = Path(tempfile.gettempdir()) / f"{marker}.server.log"
        self.client_log_path = Path(tempfile.gettempdir()) / f"{marker}.client.log"
        self.server_log: Any | None = None
        self.client_log: Any | None = None
        self.requests: list[HttpResponse] = []
        self.started_processes = 0
        self.terminated_processes = 0
        self.orphan_processes = 0

    def emit(self, message: str) -> None:
        safe = redact(message)
        print(safe, flush=True)
        if self.client_log is not None and not self.client_log.closed:
            self.client_log.write(safe + "\n")
            self.client_log.flush()

    def verify_staging_schema(self) -> None:
        import psycopg

        with psycopg.connect(self.staging_url) as connection:
            connection.execute("BEGIN READ ONLY")
            ready = connection.execute(
                "SELECT to_regclass('public.users') IS NOT NULL "
                "AND to_regclass('public.transactions') IS NOT NULL "
                "AND to_regclass('public.action_idempotency') IS NOT NULL"
            ).fetchone()[0]
            connection.rollback()
        if not ready:
            raise IntegrationFailure("required staging schema is unavailable")
        self.emit("PASS step=staging_target_and_schema_verified")

    def start(self) -> None:
        self.client_log = self.client_log_path.open("w", encoding="utf-8")
        self.emit(f"CLIENT_LOG_FILE {self.client_log_path}")
        self.emit(f"SERVER_LOG_FILE {self.server_log_path}")
        environment = os.environ.copy()
        environment["DATABASE_URL"] = self.staging_url
        environment["GEMINI_API_KEY"] = ""
        environment["SENTINELPAY_TEST_MARKER"] = self.marker
        environment["RATE_LIMIT_LOGIN_MAX_REQUESTS"] = "1"
        environment["RATE_LIMIT_LOGIN_WINDOW_SECONDS"] = "60"
        existing_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
        self.server_log = self.server_log_path.open("w", encoding="utf-8")
        self.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "tests.staging_diagnostic_app:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=ROOT, env=environment, stdout=self.server_log, stderr=subprocess.STDOUT, text=True,
        )
        self.started_processes = 1
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        last_error = "not ready"
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise IntegrationFailure(f"local server exited during startup; see {self.server_log_path}")
            try:
                response = self.request("local_server_health", "GET", "/health", record=False)
                if response.status == 200:
                    self.emit("PASS step=local_server_ready")
                    return
                last_error = f"health_status={response.status}"
            except IntegrationFailure as error:
                last_error = str(error)
            time.sleep(0.2)
        raise IntegrationFailure(f"local server readiness timed out after {REQUEST_TIMEOUT_SECONDS:.0f}s: {last_error}")

    @staticmethod
    def new_client() -> Any:
        """Return an isolated real HTTP cookie jar for one simulated user."""
        return build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, step: str, method: str, path: str, *, json_body: Any | None = None, raw_body: bytes | None = None, headers: dict[str, str] | None = None, opener: Any | None = None, record: bool = True) -> HttpResponse:
        if json_body is not None and raw_body is not None:
            raise ValueError("supply json_body or raw_body, not both")
        payload = json.dumps(json_body).encode("utf-8") if json_body is not None else raw_body
        request_headers = dict(headers or {})
        if payload is not None and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = "application/json"
        request_headers.setdefault("X-SentinelPay-Test-Source", self.marker)
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.perf_counter()
        self.emit(f"START step={step} method={method} path={path} started_at={started_at} connect_timeout_seconds={CONNECT_TIMEOUT_SECONDS:.1f} read_timeout_seconds={READ_TIMEOUT_SECONDS:.1f} write_timeout_seconds={WRITE_TIMEOUT_SECONDS:.1f} request_timeout_seconds={REQUEST_TIMEOUT_SECONDS:.1f}")
        try:
            # urllib has one socket deadline; this bounds connect/read/write, and
            # the elapsed check below also enforces the end-to-end request limit.
            request = Request(self.base_url + path, data=payload, headers=request_headers, method=method)
            with (opener or self.opener).open(request, timeout=max(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS, WRITE_TIMEOUT_SECONDS)) as result:
                status, body = int(result.status), result.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            status, body = int(error.code), error.read().decode("utf-8", errors="replace")
        except (TimeoutError, socket.timeout) as error:
            elapsed = time.perf_counter() - started
            self.emit(f"END step={step} method={method} path={path} error=timeout elapsed_seconds={elapsed:.3f}")
            raise IntegrationFailure(f"{step}: request timeout after {elapsed:.3f}s") from error
        except URLError as error:
            elapsed = time.perf_counter() - started
            self.emit(f"END step={step} method={method} path={path} error={type(error.reason).__name__} elapsed_seconds={elapsed:.3f}")
            raise IntegrationFailure(f"{step}: network error {type(error.reason).__name__}") from error
        elapsed = time.perf_counter() - started
        if elapsed > REQUEST_TIMEOUT_SECONDS:
            self.emit(f"END step={step} method={method} path={path} error=request_timeout elapsed_seconds={elapsed:.3f}")
            raise IntegrationFailure(f"{step}: request exceeded {REQUEST_TIMEOUT_SECONDS:.0f}s")
        safe_body = redact(body)
        self.emit(f"END step={step} method={method} path={path} status={status} completion_at={datetime.now(timezone.utc).isoformat()} elapsed_seconds={elapsed:.3f} response_body={safe_body[:2000]}")
        try:
            parsed: Any | None = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = None
        response = HttpResponse(status, body, parsed, elapsed)
        if record:
            self.requests.append(response)
        return response

    def cleanup_marker_data(self) -> None:
        """Delete only rows owned by this unique test marker; never broad data."""
        import psycopg

        with psycopg.connect(self.staging_url) as connection:
            connection.execute("DELETE FROM rate_limit_buckets WHERE subject_key LIKE %s", (f"%{self.marker}%",))
            connection.execute("DELETE FROM audit_events WHERE source_hash LIKE %s", (f"{self.marker}%",))
            connection.execute("DELETE FROM users WHERE email LIKE %s", (f"{self.marker}-%",))
            remaining = connection.execute("SELECT (SELECT count(*) FROM users WHERE email LIKE %s) + (SELECT count(*) FROM transactions t JOIN users u ON u.id=t.user_id WHERE u.email LIKE %s)", (f"{self.marker}-%", f"{self.marker}-%")).fetchone()[0]
        if remaining:
            raise IntegrationFailure(f"marker cleanup left {remaining} user/transaction rows")
        self.emit("PASS step=marker_data_cleanup_verified")

    def close(self) -> None:
        try:
            if self.server is not None and self.server.poll() is None:
                self.server.terminate()
                try:
                    self.server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.server.kill()
                    self.server.wait(timeout=5)
                self.terminated_processes = 1
            if self.server is not None and self.server.poll() is None:
                self.orphan_processes = 1
        finally:
            if self.server_log is not None:
                self.server_log.close()
            if self.server_log_path.exists():
                for line in self.server_log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    self.emit(f"SERVER_LOG {line}")
        if self.client_log is not None and not self.client_log.closed:
            self.client_log.close()
