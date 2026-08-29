"""Transfer use case against real PostgreSQL.

Covers the mandatory cases from spec 24.2 that concern a single transfer.
Concurrency lives in tests/concurrency/.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from modules.financial_core.application.idempotency import fingerprint_request
from modules.financial_core.application.transfer import (
    TransferCommand,
    TransferKind,
    TransferUseCase,
)
from platform_.database.unit_of_work import UnitOfWork
from platform_.kernel.errors import (
    IdempotencyKeyReuseError,
    InsufficientFundsError,
    SelfTransferError,
)
from platform_.kernel.ids import new_ulid
from platform_.kernel.money import Money
from tests.conftest import UserFactory

pytestmark = pytest.mark.integration


def _p2p_transfer_count(session: Session) -> int:
    """Count only peer-to-peer sends.

    Opening balances are themselves transfers (spec 8.3 - the signup grant is
    a real balanced ledger transaction, not a balance poke), so a bare
    ``count(*)`` would also count the fixtures' welcome bonuses.
    """
    return session.scalar(
        text("SELECT count(*) FROM transfers WHERE kind = 'P2P_SEND'")
    )


def _p2p_ledger_entry_count(session: Session) -> int:
    return session.scalar(
        text(
            "SELECT count(*) FROM ledger_entries e "
            "JOIN ledger_transactions lt ON lt.id = e.ledger_transaction_id "
            "JOIN transfers t ON t.id = lt.transfer_id "
            "WHERE t.kind = 'P2P_SEND'"
        )
    )


def _command(
    *, actor: str, sender: str, receiver: str, minor: int, key: str | None = None
) -> TransferCommand:
    payload = {"receiverAccountId": receiver, "amountMinor": minor}
    return TransferCommand(
        actor_user_id=actor,
        sender_account_id=sender,
        receiver_account_id=receiver,
        amount=Money.from_minor(minor),
        idempotency_key=key or new_ulid(),
        request_fingerprint=fingerprint_request(payload),
        kind=TransferKind.P2P_SEND,
        note="test transfer",
        request_id=new_ulid(),
    )


def _setup_two_users(
    session_factory: sessionmaker[Session], *, alice_balance: int = 10_000_000
) -> tuple[str, str, str, str]:
    with session_factory() as session:
        alice_user, alice_acct = UserFactory.create(
            session, name="Alice", balance_minor=alice_balance
        )
        bob_user, bob_acct = UserFactory.create(session, name="Bob", balance_minor=10_000_000)
        session.commit()
    return alice_user, alice_acct, bob_user, bob_acct


class TestSuccessfulTransfer:
    def test_moves_money_and_posts_balanced_ledger(
        self,
        session_factory: sessionmaker[Session],
        unit_of_work: UnitOfWork,
        transfer_use_case: TransferUseCase,
    ) -> None:
        alice_user, alice_acct, _, bob_acct = _setup_two_users(session_factory)

        result = unit_of_work.run(
            lambda s: transfer_use_case.execute(
                s,
                _command(
                    actor=alice_user, sender=alice_acct, receiver=bob_acct, minor=250_000
                ),
            )
        )

        assert result.status == "SUCCEEDED"
        assert result.reference.startswith("TRX-")
        assert result.amount.minor == 250_000

        with session_factory() as session:
            balances = dict(
                session.execute(
                    text("SELECT id, balance_minor FROM accounts WHERE id IN (:a, :b)"),
                    {"a": alice_acct, "b": bob_acct},
                ).all()
            )
            assert balances[alice_acct] == 10_000_000 - 250_000
            assert balances[bob_acct] == 10_000_000 + 250_000

            # Invariant 8: exactly two entries. Invariant 9: they sum to zero.
            entries = session.execute(
                text(
                    "SELECT e.account_id, e.amount_minor, e.balance_after_minor "
                    "FROM ledger_entries e "
                    "JOIN ledger_transactions t ON t.id = e.ledger_transaction_id "
                    "WHERE t.transfer_id = :tid ORDER BY e.amount_minor"
                ),
                {"tid": result.transfer_id},
            ).all()
            assert len(entries) == 2
            assert sum(row[1] for row in entries) == 0
            assert {row[0] for row in entries} == {alice_acct, bob_acct}

            # balance_after snapshots must match the real balances.
            snapshots = {row[0]: row[2] for row in entries}
            assert snapshots[alice_acct] == balances[alice_acct]
            assert snapshots[bob_acct] == balances[bob_acct]

    def test_writes_outbox_event_in_the_same_transaction(
        self,
        session_factory: sessionmaker[Session],
        unit_of_work: UnitOfWork,
        transfer_use_case: TransferUseCase,
    ) -> None:
        alice_user, alice_acct, _, bob_acct = _setup_two_users(session_factory)
        result = unit_of_work.run(
            lambda s: transfer_use_case.execute(
                s,
                _command(
                    actor=alice_user, sender=alice_acct, receiver=bob_acct, minor=100_000
                ),
            )
        )
        with session_factory() as session:
            row = session.execute(
                text(
                    "SELECT event_type, aggregate_id, published_at FROM outbox_events "
                    "WHERE aggregate_id = :tid"
                ),
                {"tid": result.transfer_id},
            ).one()
            assert row[0] == "financial.transfer.succeeded.v1"
            assert row[2] is None, "event must start unpublished"


class TestRejections:
    def test_insufficient_funds_changes_nothing(
        self,
        session_factory: sessionmaker[Session],
        unit_of_work: UnitOfWork,
        transfer_use_case: TransferUseCase,
    ) -> None:
        alice_user, alice_acct, _, bob_acct = _setup_two_users(
            session_factory, alice_balance=1_000
        )

        with pytest.raises(InsufficientFundsError):
            unit_of_work.run(
                lambda s: transfer_use_case.execute(
                    s,
                    _command(
                        actor=alice_user,
                        sender=alice_acct,
                        receiver=bob_acct,
                        minor=999_999,
                    ),
                )
            )

        with session_factory() as session:
            # Invariant 10: a failed transfer changes no balance and posts
            # no ledger entries. Not "rolls back most of it" - none of it.
            assert (
                session.scalar(
                    text("SELECT balance_minor FROM accounts WHERE id = :a"),
                    {"a": alice_acct},
                )
                == 1_000
            )
            assert _p2p_transfer_count(session) == 0
            assert _p2p_ledger_entry_count(session) == 0
            assert (
                session.scalar(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE payload->>'kind' = 'P2P_SEND'"
                    )
                )
                == 0
            )
            # The idempotency reservation must not survive either, or the
            # client could never retry the same intent after topping up.
            assert (
                session.scalar(
                    text(
                        "SELECT count(*) FROM idempotency_records "
                        "WHERE idempotency_key NOT LIKE 'signup-grant:%'"
                    )
                )
                == 0
            )

    def test_self_transfer_rejected(
        self,
        session_factory: sessionmaker[Session],
        unit_of_work: UnitOfWork,
        transfer_use_case: TransferUseCase,
    ) -> None:
        alice_user, alice_acct, _, _ = _setup_two_users(session_factory)
        with pytest.raises(SelfTransferError):
            unit_of_work.run(
                lambda s: transfer_use_case.execute(
                    s,
                    _command(
                        actor=alice_user,
                        sender=alice_acct,
                        receiver=alice_acct,
                        minor=100,
                    ),
                )
            )


class TestIdempotency:
    def test_same_key_repeated_creates_exactly_one_transfer(
        self,
        session_factory: sessionmaker[Session],
        unit_of_work: UnitOfWork,
        transfer_use_case: TransferUseCase,
    ) -> None:
        alice_user, alice_acct, _, bob_acct = _setup_two_users(session_factory)
        key = new_ulid()

        results = [
            unit_of_work.run(
                lambda s: transfer_use_case.execute(
                    s,
                    _command(
                        actor=alice_user,
                        sender=alice_acct,
                        receiver=bob_acct,
                        minor=250_000,
                        key=key,
                    ),
                )
            )
            for _ in range(10)
        ]

        assert len({r.transfer_id for r in results}) == 1
        assert results[0].replayed is False
        assert all(r.replayed for r in results[1:])

        with session_factory() as session:
            assert _p2p_transfer_count(session) == 1
            assert _p2p_ledger_entry_count(session) == 2
            # Money moved once, not ten times.
            assert (
                session.scalar(
                    text("SELECT balance_minor FROM accounts WHERE id = :a"),
                    {"a": alice_acct},
                )
                == 10_000_000 - 250_000
            )

    def test_same_key_different_payload_is_rejected(
        self,
        session_factory: sessionmaker[Session],
        unit_of_work: UnitOfWork,
        transfer_use_case: TransferUseCase,
    ) -> None:
        alice_user, alice_acct, _, bob_acct = _setup_two_users(session_factory)
        key = new_ulid()

        unit_of_work.run(
            lambda s: transfer_use_case.execute(
                s,
                _command(
                    actor=alice_user,
                    sender=alice_acct,
                    receiver=bob_acct,
                    minor=250_000,
                    key=key,
                ),
            )
        )

        with pytest.raises(IdempotencyKeyReuseError):
            unit_of_work.run(
                lambda s: transfer_use_case.execute(
                    s,
                    _command(
                        actor=alice_user,
                        sender=alice_acct,
                        receiver=bob_acct,
                        minor=999_999,  # different amount, same key
                        key=key,
                    ),
                )
            )

        with session_factory() as session:
            assert _p2p_transfer_count(session) == 1
