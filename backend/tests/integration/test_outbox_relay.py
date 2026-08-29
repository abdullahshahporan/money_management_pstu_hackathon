"""Outbox relay behaviour (spec 15, 20.1).

The claim being tested: a broker outage degrades notifications, never money.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from apps.outbox_relay.main import process_batch
from modules.financial_core.application.idempotency import fingerprint_request
from modules.financial_core.application.transfer import (
    TransferCommand,
    TransferKind,
    TransferUseCase,
)
from platform_.database.unit_of_work import UnitOfWork
from platform_.kernel.ids import new_ulid
from platform_.kernel.money import Money
from platform_.messaging.publisher import PublishError
from tests.conftest import UserFactory

pytestmark = pytest.mark.integration


class RecordingBroker:
    """Captures published envelopes."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, *, routing_key: str, envelope: dict[str, Any]) -> None:
        self.published.append({"routingKey": routing_key, **envelope})

    def close(self) -> None:
        return


class BrokenBroker:
    """Simulates a broker that is down."""

    def __init__(self) -> None:
        self.attempts = 0

    def publish(self, *, routing_key: str, envelope: dict[str, Any]) -> None:  # noqa: ARG002
        self.attempts += 1
        raise PublishError("connection refused")

    def close(self) -> None:
        return


def _make_transfer(
    session_factory: sessionmaker[Session],
    unit_of_work: UnitOfWork,
    transfer_use_case: TransferUseCase,
) -> str:
    with session_factory() as session:
        alice_user, alice_acct = UserFactory.create(session, balance_minor=10_000_000)
        _, bob_acct = UserFactory.create(session, balance_minor=0)
        session.commit()

    result = unit_of_work.run(
        lambda s: transfer_use_case.execute(
            s,
            TransferCommand(
                actor_user_id=alice_user,
                sender_account_id=alice_acct,
                receiver_account_id=bob_acct,
                amount=Money.from_minor(250_000),
                idempotency_key=new_ulid(),
                request_fingerprint=fingerprint_request({"n": 1}),
                kind=TransferKind.P2P_SEND,
            ),
        )
    )
    return result.transfer_id


class TestOutboxRelay:
    def test_publishes_committed_events_and_marks_them(
        self,
        session_factory: sessionmaker[Session],
        unit_of_work: UnitOfWork,
        transfer_use_case: TransferUseCase,
    ) -> None:
        transfer_id = _make_transfer(session_factory, unit_of_work, transfer_use_case)
        broker = RecordingBroker()

        with session_factory() as session:
            published = process_batch(session, broker, batch_size=100, max_attempts=8)
            session.commit()

        assert published >= 1
        transfer_events = [
            e for e in broker.published
            if e["eventType"] == "financial.transfer.succeeded.v1"
            and e["aggregateId"] == transfer_id
        ]
        assert len(transfer_events) == 1
        assert transfer_events[0]["payload"]["amountMinor"] == 250_000

        with session_factory() as session:
            unpublished = session.scalar(
                text("SELECT count(*) FROM outbox_events WHERE published_at IS NULL")
            )
            assert unpublished == 0

    def test_broker_outage_does_not_roll_back_money(
        self,
        session_factory: sessionmaker[Session],
        unit_of_work: UnitOfWork,
        transfer_use_case: TransferUseCase,
    ) -> None:
        """The headline failure-tolerance property (spec 20.1)."""
        transfer_id = _make_transfer(session_factory, unit_of_work, transfer_use_case)
        broken = BrokenBroker()

        with session_factory() as session:
            published = process_batch(session, broken, batch_size=100, max_attempts=8)
            session.commit()

        assert published == 0
        assert broken.attempts >= 1

        with session_factory() as session:
            # The money is untouched: the transfer committed long before the
            # relay ever ran, which is the entire point of the outbox.
            assert (
                session.scalar(
                    text("SELECT count(*) FROM transfers WHERE id = :t"),
                    {"t": transfer_id},
                )
                == 1
            )
            # The event is still queued with a scheduled retry, not lost.
            row = session.execute(
                text(
                    "SELECT attempt_count, next_attempt_at, last_error, dead_lettered_at "
                    "FROM outbox_events WHERE aggregate_id = :t"
                ),
                {"t": transfer_id},
            ).mappings().one()
            assert row["attempt_count"] == 1
            assert row["next_attempt_at"] is not None
            assert "connection refused" in row["last_error"]
            assert row["dead_lettered_at"] is None

    def test_backlog_drains_once_the_broker_returns(
        self,
        session_factory: sessionmaker[Session],
        unit_of_work: UnitOfWork,
        transfer_use_case: TransferUseCase,
    ) -> None:
        transfer_id = _make_transfer(session_factory, unit_of_work, transfer_use_case)

        with session_factory() as session:
            process_batch(session, BrokenBroker(), batch_size=100, max_attempts=8)
            session.commit()

        # Clear the backoff so the retry is eligible immediately.
        with session_factory() as session:
            session.execute(text("UPDATE outbox_events SET next_attempt_at = now()"))
            session.commit()

        healthy = RecordingBroker()
        with session_factory() as session:
            published = process_batch(session, healthy, batch_size=100, max_attempts=8)
            session.commit()

        assert published >= 1
        assert any(e["aggregateId"] == transfer_id for e in healthy.published)

        with session_factory() as session:
            assert (
                session.scalar(
                    text("SELECT count(*) FROM outbox_events WHERE published_at IS NULL")
                )
                == 0
            )

    def test_poison_event_is_dead_lettered_not_retried_forever(
        self,
        session_factory: sessionmaker[Session],
        unit_of_work: UnitOfWork,
        transfer_use_case: TransferUseCase,
    ) -> None:
        _make_transfer(session_factory, unit_of_work, transfer_use_case)

        # max_attempts=1 makes the first failure terminal.
        with session_factory() as session:
            process_batch(session, BrokenBroker(), batch_size=100, max_attempts=1)
            session.commit()

        with session_factory() as session:
            dead = session.scalar(
                text("SELECT count(*) FROM outbox_events WHERE dead_lettered_at IS NOT NULL")
            )
            assert dead >= 1
            # A dead-lettered event is excluded from the relay's claim query,
            # so it stops consuming capacity and waits for an operator.
            claimable = session.scalar(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE published_at IS NULL AND dead_lettered_at IS NULL"
                )
            )
            assert claimable == 0
