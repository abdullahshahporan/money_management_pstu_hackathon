"""Concurrency proofs.

Spec 24.1: "Do not mock PostgreSQL for concurrency claims; use the real
engine." These tests run real OS threads against real connections, so the
contention happens inside PostgreSQL where it actually matters. Each thread
owns its own session - sharing one would serialise the test and prove nothing.
"""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from apps.api.container import build_container
from modules.financial_core.adapters.persistence.repositories import (
    SqlAccountRepository,
    SqlAuditRepository,
    SqlIdempotencyRepository,
    SqlLedgerRepository,
    SqlOutboxPublisher,
    SqlTransferRepository,
)
from modules.financial_core.application.idempotency import fingerprint_request
from modules.financial_core.application.transfer import (
    TransferCommand,
    TransferKind,
    TransferUseCase,
)
from modules.financial_core.domain.policies import (
    DefaultLimitPolicy,
    NoFeeStrategy,
    TransferLimits,
)
from platform_.config import get_settings
from platform_.database.unit_of_work import UnitOfWork
from platform_.kernel.clock import SystemClock
from platform_.kernel.errors import AppError, InsufficientFundsError
from platform_.kernel.ids import new_ulid
from platform_.kernel.money import Money
from tests.conftest import UserFactory

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]


def _p2p_counts(session: Session) -> tuple[int, int]:
    """(transfers, ledger entries) for peer-to-peer sends only.

    Opening balances are real transfers too (spec 8.3), so counting every row
    would also count the fixtures' welcome bonuses.
    """
    transfers = session.scalar(
        text("SELECT count(*) FROM transfers WHERE kind = 'P2P_SEND'")
    )
    entries = session.scalar(
        text(
            "SELECT count(*) FROM ledger_entries e "
            "JOIN ledger_transactions lt ON lt.id = e.ledger_transaction_id "
            "JOIN transfers t ON t.id = lt.transfer_id "
            "WHERE t.kind = 'P2P_SEND'"
        )
    )
    return int(transfers), int(entries)


def _build_use_case() -> TransferUseCase:
    """A use case with generous limits, so only the balance can reject."""
    return TransferUseCase(
        account_locker=SqlAccountRepository(),
        account_writer=SqlAccountRepository(),
        transfer_writer=SqlTransferRepository(),
        transfer_reader=SqlTransferRepository(),
        ledger_writer=SqlLedgerRepository(),
        idempotency_store=SqlIdempotencyRepository(),
        event_publisher=SqlOutboxPublisher(),
        audit_recorder=SqlAuditRepository(),
        fee_strategy=NoFeeStrategy(),
        limit_policy=DefaultLimitPolicy(
            TransferLimits(
                max_single_transfer_minor=5_000_000_000,
                daily_total_minor=5_000_000_000,
            )
        ),
        clock=SystemClock(),
    )


def _run_concurrently(
    session_factory: sessionmaker[Session],
    tasks: list[TransferCommand],
    *,
    workers: int = 20,
) -> Counter[str]:
    """Fire every command at once and tally the outcomes by error code."""
    use_case = _build_use_case()
    uow = UnitOfWork(session_factory, max_attempts=5, base_delay_ms=10)
    outcomes: Counter[str] = Counter()
    lock = threading.Lock()
    # Release all threads at the same instant for maximum contention.
    gate = threading.Barrier(workers)

    def attempt(command: TransferCommand) -> None:
        gate.wait()
        try:
            uow.run(lambda s: use_case.execute(s, command))
            code = "SUCCEEDED"
        except AppError as exc:
            code = exc.code
        except Exception as exc:  # noqa: BLE001
            code = f"UNEXPECTED:{type(exc).__name__}:{exc}"
        with lock:
            outcomes[code] += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(attempt, tasks))
    return outcomes


def _assert_books_balance(session_factory: sessionmaker[Session]) -> None:
    """The invariants that must hold after *any* sequence of transfers."""
    with session_factory() as session:
        # Invariant 9: every posted transaction sums to zero.
        unbalanced = session.scalar(
            text(
                "SELECT count(*) FROM (SELECT ledger_transaction_id FROM ledger_entries "
                "GROUP BY ledger_transaction_id HAVING sum(amount_minor) <> 0) x"
            )
        )
        assert unbalanced == 0, "found unbalanced ledger transactions"

        # Invariant 12: materialised balance equals the sum of ledger entries.
        drift = session.execute(
            text(
                "SELECT a.id, a.balance_minor, COALESCE(SUM(e.amount_minor), 0) AS derived "
                "FROM accounts a LEFT JOIN ledger_entries e ON e.account_id = a.id "
                "WHERE a.account_type = 'USER' "
                "GROUP BY a.id, a.balance_minor "
                "HAVING a.balance_minor <> COALESCE(SUM(e.amount_minor), 0)"
            )
        ).all()
        assert drift == [], f"materialised balance drifted from ledger: {drift}"

        # Invariant 6: no user account is ever negative.
        negatives = session.scalar(
            text(
                "SELECT count(*) FROM accounts "
                "WHERE account_type = 'USER' AND balance_minor < 0"
            )
        )
        assert negatives == 0, "a user account went negative"


class TestOverdraftRace:
    """Spec 11.6, stated exactly."""

    def test_hundred_simultaneous_withdrawals_cannot_overspend(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        # BDT 10,000.00 = 1,000,000 poisha. Each transfer is BDT 200.00.
        # Demand is exactly twice the balance, so exactly half must fail.
        starting_balance = 1_000_000
        transfer_amount = 20_000
        attempts = 100
        expected_successes = starting_balance // transfer_amount  # 50

        with session_factory() as session:
            alice_user, alice_acct = UserFactory.create(
                session, name="Alice", balance_minor=starting_balance
            )
            _, bob_acct = UserFactory.create(session, name="Bob", balance_minor=0)
            session.commit()

        commands = [
            TransferCommand(
                actor_user_id=alice_user,
                sender_account_id=alice_acct,
                receiver_account_id=bob_acct,
                amount=Money.from_minor(transfer_amount),
                idempotency_key=new_ulid(),  # distinct intents, not retries
                request_fingerprint=fingerprint_request({"n": i}),
                kind=TransferKind.P2P_SEND,
            )
            for i in range(attempts)
        ]

        outcomes = _run_concurrently(session_factory, commands, workers=20)

        unexpected = {k: v for k, v in outcomes.items() if k.startswith("UNEXPECTED")}
        assert not unexpected, f"unexpected failures: {unexpected}"

        assert outcomes["SUCCEEDED"] == expected_successes
        assert outcomes[InsufficientFundsError.code] == attempts - expected_successes

        with session_factory() as session:
            alice_balance = session.scalar(
                text("SELECT balance_minor FROM accounts WHERE id = :a"), {"a": alice_acct}
            )
            bob_balance = session.scalar(
                text("SELECT balance_minor FROM accounts WHERE id = :b"), {"b": bob_acct}
            )
            assert alice_balance == 0, "sender must be drained to exactly zero"
            assert bob_balance == starting_balance, "receiver must gain exactly the balance"

            transfers, entries = _p2p_counts(session)
            assert transfers == expected_successes
            assert entries == expected_successes * 2
            # Across the WHOLE ledger, including the opening grants, the
            # signed entries must still sum to exactly zero. This is the
            # "no money was created or destroyed" proof.
            assert (
                session.scalar(
                    text("SELECT COALESCE(SUM(amount_minor),0) FROM ledger_entries")
                )
                == 0
            )

        _assert_books_balance(session_factory)


class TestIdempotencyStorm:
    """A double-tap is not 2 requests; it can be 50."""

    def test_same_key_fired_concurrently_creates_exactly_one_transfer(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as session:
            alice_user, alice_acct = UserFactory.create(session, balance_minor=10_000_000)
            _, bob_acct = UserFactory.create(session, balance_minor=0)
            session.commit()

        shared_key = new_ulid()
        payload = {"receiverAccountId": bob_acct, "amountMinor": 250_000}
        commands = [
            TransferCommand(
                actor_user_id=alice_user,
                sender_account_id=alice_acct,
                receiver_account_id=bob_acct,
                amount=Money.from_minor(250_000),
                idempotency_key=shared_key,
                request_fingerprint=fingerprint_request(payload),
                kind=TransferKind.P2P_SEND,
            )
            for _ in range(50)
        ]

        outcomes = _run_concurrently(session_factory, commands, workers=25)
        unexpected = {k: v for k, v in outcomes.items() if k.startswith("UNEXPECTED")}
        assert not unexpected, f"unexpected failures: {unexpected}"

        with session_factory() as session:
            # The only assertion that really matters: money moved once.
            transfers, entries = _p2p_counts(session)
            assert transfers == 1
            assert entries == 2
            assert (
                session.scalar(
                    text("SELECT balance_minor FROM accounts WHERE id = :a"),
                    {"a": alice_acct},
                )
                == 10_000_000 - 250_000
            )

        # Every caller either got the result or was told to retry - nobody
        # got a wrong answer.
        assert outcomes["SUCCEEDED"] + outcomes.get("REQUEST_IN_PROGRESS", 0) == 50
        _assert_books_balance(session_factory)


class TestSharedSpotMePoolRace:
    """Many beneficiaries may draw one pool, but its balance never goes below zero."""

    def test_shared_pool_is_drained_sequentially_without_double_spend(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        container = build_container(get_settings())
        borrowers: list[tuple[str, str]] = []
        with session_factory() as session:
            sponsor_user, _ = UserFactory.create(
                session, name="Sponsor", balance_minor=100_000
            )
            _, recipient_account = UserFactory.create(
                session, name="Merchant", balance_minor=0
            )
            for index in range(10):
                borrowers.append(
                    UserFactory.create(
                        session, name=f"Borrower {index}", balance_minor=0
                    )
                )
            session.commit()

        container.unit_of_work.run(
            lambda session: container.overdraft_service.create_pool(
                session,
                sponsor_user_id=sponsor_user,
                amount=Money.from_minor(50_000),
                idempotency_key=new_ulid(),
                request_id=new_ulid(),
            )
        )
        with session_factory() as session:
            phones = dict(
                session.execute(
                    text("SELECT id, phone FROM users WHERE id = ANY(:ids)"),
                    {"ids": [user_id for user_id, _ in borrowers]},
                ).all()
            )
        for borrower_user, _ in borrowers:
            container.unit_of_work.run(
                lambda session, user_id=borrower_user: (
                    container.overdraft_service.grant_access(
                        session,
                        sponsor_user_id=sponsor_user,
                        beneficiary_phone=phones[user_id],
                        max_draw_minor=10_000,
                        idempotency_key=new_ulid(),
                        request_id=new_ulid(),
                    )
                )
            )

        gate = threading.Barrier(10)
        outcomes: Counter[str] = Counter()
        outcome_lock = threading.Lock()

        def attempt(pair: tuple[str, str]) -> None:
            borrower_user, borrower_account = pair
            gate.wait()
            try:
                container.unit_of_work.run(
                    lambda session: container.overdraft_service.send(
                        session,
                        actor_user_id=borrower_user,
                        sender_account_id=borrower_account,
                        receiver_account_id=recipient_account,
                        amount=Money.from_minor(10_000),
                        idempotency_key=new_ulid(),
                        note="shared pool race",
                        request_id=new_ulid(),
                    )
                )
                result = "SUCCEEDED"
            except AppError as exc:
                result = exc.code
            with outcome_lock:
                outcomes[result] += 1

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(attempt, borrowers))

        assert outcomes["SUCCEEDED"] == 5
        assert outcomes[InsufficientFundsError.code] == 5
        with session_factory() as session:
            assert session.scalar(
                text(
                    "SELECT a.balance_minor FROM accounts a "
                    "JOIN overdraft_pools p ON p.pool_account_id = a.id "
                    "WHERE p.sponsor_user_id = :sponsor"
                ),
                {"sponsor": sponsor_user},
            ) == 0
            assert session.scalar(
                text("SELECT count(*) FROM overdraft_loans WHERE status = 'OUTSTANDING'")
            ) == 5
            assert session.scalar(
                text("SELECT count(*) FROM transfers WHERE kind = 'UNDO_HOLD'")
            ) == 5
        _assert_books_balance(session_factory)
        container.dispose()


class TestBidirectionalDeadlock:
    """A->B and B->A at the same time is the classic deadlock (spec 11.3)."""

    def test_opposite_direction_transfers_do_not_deadlock_or_corrupt(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as session:
            alice_user, alice_acct = UserFactory.create(session, balance_minor=10_000_000)
            bob_user, bob_acct = UserFactory.create(session, balance_minor=10_000_000)
            session.commit()

        commands: list[TransferCommand] = []
        for i in range(60):
            a_to_b = i % 2 == 0
            commands.append(
                TransferCommand(
                    actor_user_id=alice_user if a_to_b else bob_user,
                    sender_account_id=alice_acct if a_to_b else bob_acct,
                    receiver_account_id=bob_acct if a_to_b else alice_acct,
                    amount=Money.from_minor(10_000),
                    idempotency_key=new_ulid(),
                    request_fingerprint=fingerprint_request({"n": i}),
                    kind=TransferKind.P2P_SEND,
                )
            )

        outcomes = _run_concurrently(session_factory, commands, workers=20)
        unexpected = {k: v for k, v in outcomes.items() if k.startswith("UNEXPECTED")}
        assert not unexpected, f"unexpected failures (deadlock?): {unexpected}"
        assert outcomes["SUCCEEDED"] == 60

        with session_factory() as session:
            total = session.scalar(
                text(
                    "SELECT SUM(balance_minor) FROM accounts WHERE id IN (:a, :b)"
                ),
                {"a": alice_acct, "b": bob_acct},
            )
            # 30 each way at the same amount nets to zero movement overall.
            assert total == 20_000_000, "money was created or destroyed"

        _assert_books_balance(session_factory)
