"""Structured JSON logging (spec 23.1).

Every line carries the request id, so a single transfer can be traced from the
load balancer through the use case to the outbox relay. Spec 23.1 is also
explicit about what must *not* appear: no secrets, no raw passwords, no tokens,
no full sensitive payloads. The redacting filter below enforces that even when
a caller forgets.
"""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger.json import JsonFormatter

__all__ = ["configure_logging", "current_request_id", "set_request_id"]

# Propagates the request id to every log line in the same task without
# threading it through every function signature.
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# Anything whose *name* matches is replaced wholesale. Matching on the key
# rather than the value means a token is redacted even in a shape we did not
# anticipate.
_SENSITIVE_KEYS = re.compile(
    r"(password|passwd|pin|secret|token|authorization|cookie|api[_-]?key)",
    re.IGNORECASE,
)
_REDACTED = "***redacted***"


def set_request_id(request_id: str | None) -> None:
    _request_id.set(request_id)


def current_request_id() -> str | None:
    return _request_id.get()


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _SENSITIVE_KEYS.search(str(key)) else _redact(val, depth + 1))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in value]
    return value


# Attributes that belong to LogRecord itself. Rewriting any of these corrupts
# the record: replacing `args` (a tuple) with a list breaks the `msg % args`
# formatting that every printf-style logger in the ecosystem relies on.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "msg", "message", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)


class RequestContextFilter(logging.Filter):
    """Attaches the request id and redacts sensitive *extra* fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED_RECORD_ATTRS:
                continue
            if _SENSITIVE_KEYS.search(key):
                record.__dict__[key] = _REDACTED
            elif isinstance(value, (dict, list, tuple)):
                record.__dict__[key] = _redact(value)
        return True


def _install_collision_safe_make_record() -> None:
    """Stop a reserved key in ``extra`` from raising inside logging.

    ``Logger.makeRecord`` raises KeyError if ``extra`` contains a name that
    LogRecord already owns - ``message``, ``name``, ``args`` and friends. That
    turned a harmless log line in the notification consumer into an exception,
    which nacked the message to the dead-letter queue. A logging call must
    never be able to take down a worker that is handling money, so colliding
    keys are renamed rather than allowed to explode.
    """
    original = logging.Logger.makeRecord

    def make_record(self, name, level, fn, lno, msg, args, exc_info,  # noqa: ANN001, ANN202, PLR0913
                    func=None, extra=None, sinfo=None):
        if extra:
            safe = {
                (f"extra_{key}" if key in _RESERVED_RECORD_ATTRS else key): value
                for key, value in extra.items()
            }
        else:
            safe = extra
        return original(self, name, level, fn, lno, msg, args, exc_info, func, safe, sinfo)

    logging.Logger.makeRecord = make_record  # type: ignore[method-assign]


def configure_logging(*, level: str = "INFO", service: str = "money-movement",
                      environment: str = "development", instance_id: str = "local") -> None:
    _install_collision_safe_make_record()

    formatter = JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level"},
        static_fields={
            "service": service,
            "environment": environment,
            "instance": instance_id,
        },
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RequestContextFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # SQLAlchemy and uvicorn are chatty at INFO and would bury the events we
    # actually care about.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
