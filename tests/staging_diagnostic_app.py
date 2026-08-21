"""Test-only FastAPI entry point with timing around login's database operations."""

from __future__ import annotations

import os
import time
from functools import wraps
from typing import Any, Callable

from backend import main as application


def timed_database_operation(name: str, operation: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(operation)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        print(f"DBSTEP_START name={name}", flush=True)
        started_at = time.perf_counter()
        try:
            return operation(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - started_at
            print(f"DBSTEP_END name={name} elapsed_seconds={elapsed:.3f}", flush=True)

    return wrapped


application.enforce_request_rate_limit = timed_database_operation(
    "login_rate_limit", application.enforce_request_rate_limit
)
application.authenticate_user = timed_database_operation(
    "authenticate_user", application.authenticate_user
)
application.create_session = timed_database_operation(
    "create_session", application.create_session
)
application.audit_event = timed_database_operation("audit_event", application.audit_event)

# This module is loaded only by the local integration subprocess.  It does not
# change the deployed application: Gemini is deterministic and audit/rate-limit
# source keys are marker-scoped solely so test data can be safely cleaned up.
_marker = os.environ.get("SENTINELPAY_TEST_MARKER", "")
if _marker:
    application.get_gemini_configuration = lambda: (None, application.DEFAULT_GEMINI_MODEL)
    application.request_source_fingerprint = lambda request: request.headers.get(
        "X-SentinelPay-Test-Source", _marker
    )

app = application.app
