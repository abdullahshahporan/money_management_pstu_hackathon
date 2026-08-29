"""The outbox relay (spec 15.2).

Reads committed events from ``outbox_events`` and publishes them to the broker,
then marks them published. Runs as its own process so a slow or unavailable
broker cannot add latency to a transfer.

Two properties make this safe to run in more than one replica:

*   ``FOR UPDATE SKIP LOCKED`` means each relay claims a disjoint batch instead
    of competing for the same rows.
*   Delivery is at-least-once and openly so. If the broker confirms a message
    and this process dies before the ``published_at`` write commits, the event
    is re-sent later. Consumers deduplicate through ``consumer_inbox``, which
    is why duplicates are harmless rather than merely unlikely.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from types import FrameType

from sqlalchemy import text
from sqlalchemy.orm import Session

from platform_.config import get_settings
from platform_.database.engine import build_engine, build_session_factory
from platform_.messaging.publisher import (
    EventBroker,
    NullBroker,
    PublishError,
    RabbitMqBroker,
)
from platform_.observability.logging import configure_logging

logger = logging.getLogger("outbox_relay")

_shutdown = False


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    """Spec 20.3: finish the batch in flight, then stop."""
    global _shutdown  # noqa: PLW0603
    logger.info("shutdown_requested", extra={"signal": signum})
    _shutdown = True


CLAIM_BATCH_SQL = """
    SELECT id, aggregate_type, aggregate_id, event_type, payload,
           trace_id, schema_version, occurred_at, attempt_count
    FROM outbox_events
    WHERE published_at IS NULL
      AND dead_lettered_at IS NULL
      AND (next_attempt_at IS NULL OR next_attempt_at <= now())
    ORDER BY occurred_at
    FOR UPDATE SKIP LOCKED
    LIMIT :batch_size
"""


def _backoff_delay(attempt: int) -> timedelta:
    """Exponential backoff with a ceiling (spec 15.5)."""
    return timedelta(seconds=min(2**attempt, 300))


def process_batch(session: Session, broker: EventBroker, *, batch_size: int,
                  max_attempts: int) -> int:
    """Claim and publish one batch. Returns how many were published."""
    rows = session.execute(text(CLAIM_BATCH_SQL), {"batch_size": batch_size}).mappings().all()
    if not rows:
        return 0

    published = 0
    for row in rows:
        envelope = {
            "eventId": row["id"],
            "eventType": row["event_type"],
            "aggregateType": row["aggregate_type"],
            "aggregateId": row["aggregate_id"],
            "occurredAt": row["occurred_at"].isoformat(),
            "traceId": row["trace_id"],
            "schemaVersion": row["schema_version"],
            "payload": row["payload"],
        }
        try:
            broker.publish(routing_key=row["event_type"], envelope=envelope)
        except PublishError as exc:
            attempt = row["attempt_count"] + 1
            dead = attempt >= max_attempts
            session.execute(
                text(
                    "UPDATE outbox_events SET attempt_count = :attempt,"
                    " last_error = :error, next_attempt_at = :next_at,"
                    " dead_lettered_at = :dead_at WHERE id = :id"
                ),
                {
                    "attempt": attempt,
                    "error": str(exc)[:500],
                    "next_at": datetime.now(UTC) + _backoff_delay(attempt),
                    "dead_at": datetime.now(UTC) if dead else None,
                    "id": row["id"],
                },
            )
            logger.warning(
                "outbox_publish_failed",
                extra={"event_id": row["id"], "attempt": attempt, "dead_lettered": dead},
            )
            continue

        session.execute(
            text("UPDATE outbox_events SET published_at = now() WHERE id = :id"),
            {"id": row["id"]},
        )
        published += 1

    return published


def main() -> int:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        environment=settings.environment,
        instance_id=settings.instance_id,
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    broker: EventBroker = (
        RabbitMqBroker(settings.rabbitmq_url) if settings.rabbitmq_url else NullBroker()
    )

    logger.info("outbox_relay_started", extra={"batch_size": settings.outbox_batch_size})

    try:
        while not _shutdown:
            try:
                with session_factory() as session:
                    # One transaction per batch: the row locks are held only
                    # for as long as the batch takes, so other relays keep
                    # making progress on different rows.
                    published = process_batch(
                        session,
                        broker,
                        batch_size=settings.outbox_batch_size,
                        max_attempts=settings.outbox_max_attempts,
                    )
                    session.commit()

                if published:
                    logger.info("outbox_batch_published", extra={"count": published})
                else:
                    time.sleep(settings.outbox_poll_interval_seconds)
            except Exception:  # noqa: BLE001
                # The relay must survive a database blip: money is already
                # committed, and the backlog simply waits.
                logger.exception("outbox_relay_iteration_failed")
                time.sleep(min(settings.outbox_poll_interval_seconds * 5, 10))
    finally:
        broker.close()
        engine.dispose()
        logger.info("outbox_relay_stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
