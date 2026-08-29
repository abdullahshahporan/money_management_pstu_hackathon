"""Notification consumer (spec 15.3).

Consumes transfer and money-request events and turns them into user-facing
notifications. Two things are worth stating:

*   It deduplicates. Delivery is at-least-once, so the same event can arrive
    twice. The consumer inserts ``(consumer_name, event_id)`` into
    ``consumer_inbox`` *before* acting; the composite primary key makes the
    second delivery fail harmlessly, so the effect happens exactly once even
    though delivery did not.
*   It is entirely off the money path. If this process is down, transfers
    still commit and the backlog simply waits (spec 20.1).
"""

from __future__ import annotations

import json
import logging
import signal
import sys
from types import FrameType
from typing import Any

import pika
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from platform_.config import get_settings
from platform_.database.engine import build_engine, build_session_factory
from platform_.messaging.publisher import EXCHANGE_NAME
from platform_.observability.logging import configure_logging

logger = logging.getLogger("notification_worker")

CONSUMER_NAME = "notification-worker"
QUEUE_NAME = "notifications"
DEAD_LETTER_EXCHANGE = "money.events.dlx"
DEAD_LETTER_QUEUE = "notifications.dlq"

_shutdown = False


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _shutdown  # noqa: PLW0603
    logger.info("shutdown_requested", extra={"signal": signum})
    _shutdown = True


def _claim_event(session: Session, event_id: str) -> bool:
    """Record that we are handling this event. False if already handled.

    This is the deduplication point. It runs in the same transaction as the
    effect below, so either both happen or neither does - a crash between
    them cannot leave the event marked processed but the notification unsent.
    """
    try:
        session.execute(
            text(
                "INSERT INTO consumer_inbox (consumer_name, event_id, processed_at) "
                "VALUES (:consumer, :event_id, now())"
            ),
            {"consumer": CONSUMER_NAME, "event_id": event_id},
        )
        session.flush()
    except IntegrityError:
        session.rollback()
        return False
    return True


def _describe(event_type: str, payload: dict[str, Any]) -> str:
    amount = payload.get("amountMinor", 0) / 100
    if event_type == "financial.transfer.succeeded.v1":
        return f"Transfer {payload.get('reference')} of BDT {amount:,.2f} completed"
    if event_type == "money_request.created.v1":
        return f"New money request {payload.get('reference')} for BDT {amount:,.2f}"
    if event_type == "money_request.accepted.v1":
        return f"Money request {payload.get('requestId')} was paid"
    if event_type == "money_request.rejected.v1":
        return f"Money request {payload.get('requestId')} was declined"
    return f"Event {event_type}"


def handle_message(session_factory: sessionmaker[Session], body: bytes) -> None:
    envelope = json.loads(body)
    event_id = envelope.get("eventId")
    event_type = envelope.get("eventType", "unknown")

    with session_factory() as session:
        if not _claim_event(session, event_id):
            logger.info("duplicate_event_ignored", extra={"event_id": event_id})
            return

        description = _describe(event_type, envelope.get("payload", {}))
        # In a full product this is where an SMS or push provider would be
        # called - behind a circuit breaker, and never inside a database lock
        # (spec 20.2). Here we record the delivery.
        logger.info(
            "notification_delivered",
            extra={
                "event_id": event_id,
                "event_type": event_type,
                "notification": description,
            },
        )
        session.commit()


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

    parameters = pika.URLParameters(settings.rabbitmq_url)
    parameters.heartbeat = 30
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()

    channel.exchange_declare(exchange=EXCHANGE_NAME, exchange_type="topic", durable=True)
    channel.exchange_declare(
        exchange=DEAD_LETTER_EXCHANGE, exchange_type="fanout", durable=True
    )
    channel.queue_declare(queue=DEAD_LETTER_QUEUE, durable=True)
    channel.queue_bind(queue=DEAD_LETTER_QUEUE, exchange=DEAD_LETTER_EXCHANGE)

    # Spec 15.5: a poison message must not loop forever. After the configured
    # retries the broker routes it to the dead-letter queue for an operator.
    channel.queue_declare(
        queue=QUEUE_NAME,
        durable=True,
        arguments={"x-dead-letter-exchange": DEAD_LETTER_EXCHANGE},
    )
    channel.queue_bind(queue=QUEUE_NAME, exchange=EXCHANGE_NAME, routing_key="#")
    # Bound in-flight work so one consumer cannot claim the whole queue.
    channel.basic_qos(prefetch_count=20)

    logger.info("notification_worker_started", extra={"queue": QUEUE_NAME})

    try:
        for method, _properties, body in channel.consume(
            QUEUE_NAME, inactivity_timeout=1
        ):
            if _shutdown:
                break
            if method is None:
                continue
            try:
                handle_message(session_factory, body)
                # Acknowledge only after the effect committed (spec 20.3).
                channel.basic_ack(method.delivery_tag)
            except Exception:  # noqa: BLE001
                logger.exception("notification_handling_failed")
                # requeue=False sends it to the DLQ rather than looping.
                channel.basic_nack(method.delivery_tag, requeue=False)
    finally:
        try:
            channel.cancel()
            connection.close()
        except Exception:  # noqa: BLE001
            logger.warning("broker_shutdown_failed", exc_info=True)
        engine.dispose()
        logger.info("notification_worker_stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
