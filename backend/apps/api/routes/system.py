"""Health probes, metrics and the protected engineering endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from apps.api.dependencies import ContainerDep, RequestId, require_engineering_key
from platform_.web.envelope import success_response

router = APIRouter(tags=["system"])


@router.get("/health/live", summary="Process liveness")
def liveness():
    """Is the process alive?

    Spec 23.4: liveness must NOT check dependencies. If it failed because
    PostgreSQL were briefly unreachable, the orchestrator would kill and
    restart healthy processes during a database blip and turn a recoverable
    incident into an outage.
    """
    return {"status": "alive"}


@router.get("/health/ready", summary="Readiness to receive traffic")
def readiness(container: ContainerDep, response: Response):
    """Can this instance safely serve requests?

    For a financial API that means the authoritative database is reachable.
    Failing readiness takes this replica out of the load balancer rotation
    while leaving the process running (spec 18.5).
    """
    try:
        with container.session_factory() as session:
            session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        response.status_code = 503
        return {"status": "not_ready", "database": "unreachable"}
    return {"status": "ready", "database": "reachable", "instance": container.settings.instance_id}


@router.get("/engineering/reconcile", summary="Integrity report (protected)")
def reconcile(
    container: ContainerDep,
    request_id: RequestId,
    _: None = Depends(require_engineering_key),
):
    """Recompute every financial invariant from the raw ledger.

    This is the evidence behind "no money was created or destroyed". It does
    not trust any application state: it re-derives each account's balance from
    its ledger entries and compares. Every counter must be zero.
    """
    report = container.unit_of_work.run(
        lambda session: container.reconciliation_service.run(session)
    )
    return success_response(report.to_dict(), request_id=request_id)


@router.get("/engineering/outbox", summary="Outbox backlog (protected)")
def outbox_status(
    container: ContainerDep,
    request_id: RequestId,
    _: None = Depends(require_engineering_key),
):
    """Backlog depth and oldest unpublished age (spec 23.2).

    A growing backlog means the relay or the broker is unhealthy - but note
    that money is unaffected either way, which is the whole point of the
    outbox.
    """

    def query(session):  # noqa: ANN001, ANN202
        row = session.execute(
            text(
                "SELECT "
                " count(*) FILTER (WHERE published_at IS NULL"
                "   AND dead_lettered_at IS NULL) AS pending,"
                " count(*) FILTER (WHERE published_at IS NOT NULL) AS published,"
                " count(*) FILTER (WHERE dead_lettered_at IS NOT NULL) AS dead_lettered,"
                " EXTRACT(EPOCH FROM (now() - MIN(occurred_at) FILTER "
                "   (WHERE published_at IS NULL AND dead_lettered_at IS NULL))) AS oldest_seconds "
                "FROM outbox_events"
            )
        ).mappings().one()
        return {
            "pending": int(row["pending"]),
            "published": int(row["published"]),
            "deadLettered": int(row["dead_lettered"]),
            "oldestPendingSeconds": float(row["oldest_seconds"] or 0),
        }

    return success_response(container.unit_of_work.run(query), request_id=request_id)


@router.get("/engineering/scheduler", summary="Deferred settlement backlog (protected)")
def scheduler_status(
    container: ContainerDep,
    request_id: RequestId,
    _: None = Depends(require_engineering_key),
):
    """Shows whether undo and escrow timers are being drained."""
    result = container.unit_of_work.run(
        lambda session: container.scheduled_tasks.backlog(session)
    )
    return success_response(result, request_id=request_id)
