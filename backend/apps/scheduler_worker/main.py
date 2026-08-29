"""The deferred-settlement worker.

Polls ``scheduled_tasks`` for due work and executes it: settling transfers
whose undo window has elapsed, auto-releasing escrow the buyer never disputed,
and expiring payment links and money requests.

Structurally identical to the outbox relay, and for the same reasons - claim a
batch with ``FOR UPDATE SKIP LOCKED``, do the work, mark it done, all inside
one transaction. Several replicas can run without duplicating or blocking each
other, and a crash loses nothing because the queue is durable rows rather than
in-memory timers.

Every handler is idempotent. Delivery is effectively at-least-once: a worker
can die between doing the work and marking the row done, and the row will be
claimed again. That is safe because each handler latches on a status
transition, so a repeat finds nothing left to do.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from types import FrameType

from sqlalchemy.orm import Session

from apps.api.container import build_container
from platform_.config import get_settings
from platform_.observability.logging import configure_logging

logger = logging.getLogger("scheduler_worker")

POLL_INTERVAL_SECONDS = 1.0
BATCH_SIZE = 50
MAX_ATTEMPTS = 8

_shutdown = False


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _shutdown  # noqa: PLW0603
    logger.info("shutdown_requested", extra={"signal": signum})
    _shutdown = True


def run_due_tasks(session: Session, container) -> int:  # noqa: ANN001
    """Claim and execute one batch. Returns how many tasks completed."""
    tasks = container.scheduled_tasks.claim_due(session, batch_size=BATCH_SIZE)
    if not tasks:
        return 0

    completed = 0
    for task in tasks:
        task_type = task["task_type"]
        resource_id = task["resource_id"]
        try:
            # One savepoint per task: a broken escrow must not roll back an
            # earlier successful undo settlement in the same claimed batch.
            with session.begin_nested():
                if task_type == "UNDO_SETTLE":
                    container.undoable_transfers.settle(session, transfer_id=resource_id)
                elif task_type == "ESCROW_AUTO_RELEASE":
                    container.safepay_service.auto_release(session, escrow_id=resource_id)
                elif task_type == "MONEY_REQUEST_EXPIRY":
                    container.money_request_service.expire_due(session)
                else:
                    logger.warning("unknown_task_type", extra={"task_type": task_type})

                container.scheduled_tasks.complete(session, task_id=task["id"])
            completed += 1
        except Exception as exc:  # noqa: BLE001
            # One bad task must not stall the batch, and must not lose the
            # money it was going to move. Back off and let it be retried; after
            # MAX_ATTEMPTS it is parked as FAILED for an operator, still
            # holding its funds safely in the holding account.
            attempt = int(task["attempt_count"]) + 1
            logger.exception(
                "scheduled_task_failed",
                extra={
                    "task_type": task_type,
                    "resource_id": resource_id,
                    "attempt": attempt,
                },
            )
            container.scheduled_tasks.record_failure(
                session,
                task_id=task["id"],
                attempt=attempt,
                error=str(exc),
                max_attempts=MAX_ATTEMPTS,
            )

    return completed


def main() -> int:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        environment=settings.environment,
        instance_id=settings.instance_id,
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    container = build_container(settings)
    logger.info("scheduler_worker_started", extra={"batch_size": BATCH_SIZE})

    try:
        while not _shutdown:
            try:
                with container.session_factory() as session:
                    done = run_due_tasks(session, container)
                    session.commit()
                if done:
                    logger.info("scheduled_tasks_completed", extra={"count": done})
                else:
                    time.sleep(POLL_INTERVAL_SECONDS)
            except Exception:  # noqa: BLE001
                # A database blip must not kill the worker: the queue is
                # durable, so the work is still there when it recovers.
                logger.exception("scheduler_iteration_failed")
                time.sleep(min(POLL_INTERVAL_SECONDS * 5, 10))
    finally:
        container.dispose()
        logger.info("scheduler_worker_stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
