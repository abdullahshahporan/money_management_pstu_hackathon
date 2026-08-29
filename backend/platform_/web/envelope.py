"""The API response envelope (spec 22.1, 22.4).

Every response - success or failure - has the same outer shape, so a client
writes one parser and one error branch:

    {"success": true,  "data": {...}, "meta": {"requestId": "..."}}
    {"success": false, "error": {"code", "message", "retryable"}, "meta": {...}}

Clients branch on ``error.code``, never on ``message``. Codes are a contract;
messages are for humans and may be reworded at any time.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

__all__ = ["error_response", "success_envelope", "success_response"]


def success_envelope(
    data: Any, *, request_id: str | None = None, **meta: Any
) -> dict[str, Any]:
    envelope: dict[str, Any] = {"success": True, "data": data, "meta": {}}
    if request_id:
        envelope["meta"]["requestId"] = request_id
    envelope["meta"].update({k: v for k, v in meta.items() if v is not None})
    return envelope


def success_response(
    data: Any,
    *,
    status_code: int = 200,
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
    **meta: Any,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=success_envelope(data, request_id=request_id, **meta),
        headers=headers,
    )


def error_response(
    *,
    code: str,
    message: str,
    status_code: int,
    retryable: bool = False,
    request_id: str | None = None,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
    if details:
        error["details"] = details

    meta: dict[str, Any] = {}
    if request_id:
        meta["requestId"] = request_id

    return JSONResponse(
        status_code=status_code,
        content={"success": False, "error": error, "meta": meta},
        headers=headers,
    )
