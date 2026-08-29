"""HTTP middleware: request identity, access logging, exception mapping.

Spec 22.1: ``X-Request-Id`` is accepted from the caller (Nginx sets it) or
generated, and echoed on the response, so one identifier ties together the
proxy log, the application log, the audit row and the outbox event.
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from platform_.kernel.errors import AppError, RateLimitedError
from platform_.observability.logging import set_request_id
from platform_.web.envelope import error_response

logger = logging.getLogger(__name__)

__all__ = ["RequestContextMiddleware", "register_exception_handlers"]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, logs the outcome, and times the request."""

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        request_id = request.headers.get("X-Request-Id") or _new_request_id()
        request.state.request_id = request_id
        set_request_id(request_id)

        started = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            # Log with a stack trace, but never return the trace to the client
            # (spec 21.3) - stack traces leak file paths and library versions.
            logger.exception(
                "request_failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-Id"] = request_id

        logger.info(
            "request_completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
        return response


def _new_request_id() -> str:
    from platform_.kernel.ids import new_ulid

    return new_ulid()


def register_exception_handlers(app: FastAPI) -> None:
    """Map every exception type onto the documented status codes (spec 22.4)."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):  # noqa: ANN202
        headers = None
        if isinstance(exc, RateLimitedError):
            headers = {"Retry-After": str(exc.retry_after_seconds)}

        # An expected refusal, not a crash: log at info/warning, no traceback.
        logger.warning(
            "request_rejected",
            extra={
                "error_code": exc.code,
                "path": request.url.path,
                "status": exc.http_status,
            },
        )
        return error_response(
            code=exc.code,
            message=exc.message,
            status_code=exc.http_status,
            retryable=exc.retryable,
            request_id=getattr(request.state, "request_id", None),
            details=exc.details or None,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):  # noqa: ANN202
        # 400 for a malformed request (spec 22.4). Field errors are echoed so
        # the client can correct them, with the offending values omitted -
        # they may contain a PIN.
        fields = [
            {
                "field": ".".join(str(part) for part in err.get("loc", []) if part != "body"),
                "issue": err.get("msg", "invalid"),
            }
            for err in exc.errors()
        ]
        return error_response(
            code="VALIDATION_ERROR",
            message="The request was malformed.",
            status_code=400,
            request_id=getattr(request.state, "request_id", None),
            details={"fields": fields},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # noqa: ANN202, ARG001
        logger.exception("unhandled_exception", extra={"path": request.url.path})
        return error_response(
            code="INTERNAL_ERROR",
            message="An unexpected error occurred.",
            status_code=500,
            retryable=True,
            request_id=getattr(request.state, "request_id", None),
        )
