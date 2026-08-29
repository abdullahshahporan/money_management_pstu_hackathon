"""FastAPI application factory.

Spec 17.1: the API tier is stateless. Nothing user-specific lives in process
memory, so any replica can serve any request and the load balancer needs no
sticky sessions. Everything held here is process-scoped infrastructure -
connection pools and stateless services - built once at startup.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.container import build_container
from apps.api.routes import accounts, auth, money_requests, overdraft, safepay, system, transfers
from platform_.config import get_settings
from platform_.observability.logging import configure_logging
from platform_.web.middleware import RequestContextMiddleware, register_exception_handlers

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    settings = app.state.settings
    logger.info("api_starting", extra={"config": settings.redacted()})
    yield
    # Spec 20.3: drain cleanly. Starlette has already stopped accepting new
    # requests and awaited the in-flight ones by the time we reach here, so
    # closing the pool now cannot interrupt a transaction mid-commit.
    logger.info("api_stopping")
    app.state.container.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        environment=settings.environment,
        instance_id=settings.instance_id,
    )

    app = FastAPI(
        title="Money Movement API",
        version="1.0.0",
        description=(
            "Closed-ecosystem money movement platform. Every balance change is a "
            "balanced double-entry ledger posting committed in a single ACID "
            "transaction, and every money mutation requires an Idempotency-Key."
        ),
        lifespan=lifespan,
        # Spec 22.1: the OpenAPI document is generated from the same Pydantic
        # models that validate requests, so the contract cannot drift.
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
    )

    app.state.settings = settings
    app.state.container = build_container(settings)

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        # Spec 21.3: a narrow allowlist, never "*" with credentials.
        allow_origins=[
            "http://localhost:5173",
            "http://localhost:8080",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key",
                       "X-Request-Id", "X-Engineering-Key"],
        expose_headers=["X-Request-Id"],
    )

    register_exception_handlers(app)

    for router in (
        auth.router,
        accounts.router,
        transfers.router,
        money_requests.router,
        safepay.router,
        overdraft.router,
        system.router,
    ):
        app.include_router(router, prefix=API_PREFIX)

    return app


app = create_app()
