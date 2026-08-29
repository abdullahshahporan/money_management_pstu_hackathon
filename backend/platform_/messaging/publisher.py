"""Broker adapters (spec 15.6, 7.4 Adapter pattern).

The relay depends on the ``EventBroker`` port, not on RabbitMQ. Swapping in
Kafka later - spec 15.6 says that choice should follow delivery and replay
needs, not fashion - means writing one adapter, not touching the relay.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import pika
from pika.exceptions import AMQPError

logger = logging.getLogger(__name__)

__all__ = ["EventBroker", "NullBroker", "PublishError", "RabbitMqBroker"]

EXCHANGE_NAME = "money.events"


class PublishError(Exception):
    """The broker did not confirm the message."""


class EventBroker(Protocol):
    def publish(self, *, routing_key: str, envelope: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


class RabbitMqBroker:
    """Publishes with publisher confirms and persistent delivery.

    Confirms matter: without them ``basic_publish`` returns as soon as the
    bytes are handed to the socket, so the relay would mark an event published
    that the broker never actually stored. With confirms, a failure raises and
    the event stays unpublished for the next attempt - at-least-once, honestly.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._connection: pika.BlockingConnection | None = None
        self._channel: pika.adapters.blocking_connection.BlockingChannel | None = None

    def _ensure_channel(self):  # noqa: ANN202
        if self._channel is not None and self._channel.is_open:
            return self._channel

        parameters = pika.URLParameters(self._url)
        parameters.heartbeat = 30
        parameters.blocked_connection_timeout = 15
        parameters.socket_timeout = 10

        self._connection = pika.BlockingConnection(parameters)
        channel = self._connection.channel()
        channel.exchange_declare(
            exchange=EXCHANGE_NAME, exchange_type="topic", durable=True
        )
        channel.confirm_delivery()
        self._channel = channel
        return channel

    def publish(self, *, routing_key: str, envelope: dict[str, Any]) -> None:
        try:
            channel = self._ensure_channel()
            channel.basic_publish(
                exchange=EXCHANGE_NAME,
                routing_key=routing_key,
                body=json.dumps(envelope).encode("utf-8"),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,  # persist to disk
                    message_id=envelope.get("eventId"),
                    correlation_id=envelope.get("traceId"),
                ),
                mandatory=True,
            )
        except (AMQPError, OSError) as exc:
            # Drop the channel so the next attempt reconnects rather than
            # reusing a broken one.
            self._channel = None
            self._connection = None
            raise PublishError(str(exc)) from exc

    def close(self) -> None:
        try:
            if self._connection is not None and self._connection.is_open:
                self._connection.close()
        except (AMQPError, OSError):
            logger.warning("broker_close_failed", exc_info=True)
        finally:
            self._channel = None
            self._connection = None


class NullBroker:
    """Logs instead of publishing. Used in tests and when no broker is configured."""

    def publish(self, *, routing_key: str, envelope: dict[str, Any]) -> None:
        logger.info(
            "event_published_to_null_broker",
            extra={"routing_key": routing_key, "event_id": envelope.get("eventId")},
        )

    def close(self) -> None:
        return
