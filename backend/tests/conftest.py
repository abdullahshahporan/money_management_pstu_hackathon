"""Shared test fixtures.

Spec 24.1: "Do not mock PostgreSQL for concurrency claims; use the real
engine." Every test here runs against a real PostgreSQL instance, because the
behaviour under test - row locks, CHECK constraints, ON CONFLICT, deadlock
detection - simply does not exist in a mock or in SQLite.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

# Pin the whole test session to the dedicated test database *before* any
# application module reads configuration. `get_settings()` is lru_cached, so
# the first call wins - and an app built from the developer .env would write
# to the database a running stack is using.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://mm_app:mm_app_dev_pw@localhost:5432/moneymovement_test",
)
os.environ.setdefault(
    "MIGRATION_DATABASE_URL",
    "postgresql+psycopg://mm_owner:mm_owner_dev_pw@localhost:5432/moneymovement_test",
)
# Throttling would make assertions depend on how fast the suite happens to run.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from modules.financial_core.adapters.persistence.repositories import (
    SqlAccountRepository,
    SqlAuditRepository,
    SqlIdempotencyRepository,
    SqlLedgerRepository,
    SqlOutboxPublisher,
    SqlTransferRepository,
)
from modules.financial_core.application.open_account import OpenAccountUseCase
from modules.financial_core.application.transfer import TransferUseCase
from modules.financial_core.domain.policies import (
    DefaultLimitPolicy,
    NoFeeStrategy,
    TransferLimits,
)
from platform_.database.unit_of_work import UnitOfWork
from platform_.kernel.clock import Clock, FixedClock, SystemClock
from platform_.kernel.ids import new_ulid
from platform_.kernel.money import Money

# A dedicated database, never the one a running stack is using. This suite
# truncates tables between tests; pointed at the deployed database it would
# destroy live data, which it did once during development.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://mm_app:mm_app_dev_pw@localhost:5432/moneymovement_test",
)

# Tables truncated between tests, in FK-safe order. accounts is NOT truncated
# wholesale because the system issuance account is schema, not test data.
_MUTABLE_TABLES = (
    "overdraft_repayments",
    "overdraft_loans",
    "overdraft_grants",
    "overdraft_pools",
    "safepay_escrows",
    "group_withdrawal_approvals",
    "group_withdrawal_requests",
    "group_members",
    "group_wallets",
    "payment_link_payments",
    "payment_links",
    "scheduled_tasks",
    "ledger_entries",
    "ledger_transactions",
    "money_requests",
    "transfers",
    "idempotency_records",
    "outbox_events",
    "consumer_inbox",
    "audit_logs",
    "sessions",
)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(autouse=True)
def clean_database(request: pytest.FixtureRequest) -> Iterator[None]:
    """Reset to a known state before every integration test.

    Skipped for pure unit tests: the domain layer has no database, and making
    every unit test pay for a TRUNCATE would blunt the fast feedback loop that
    makes unit tests worth having.

    Uses the owner role, because the runtime role deliberately cannot DELETE
    from the ledger - which is itself a property under test elsewhere.
    """
    if "integration" not in request.keywords:
        yield
        return

    owner_url = TEST_DATABASE_URL.replace("mm_app:mm_app_dev_pw", "mm_owner:mm_owner_dev_pw")
    owner_engine = create_engine(owner_url)
    with owner_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_MUTABLE_TABLES)} CASCADE"))
        conn.execute(
            text(
                "DELETE FROM accounts WHERE account_type IN "
                "('USER', 'OVERDRAFT_POOL', 'GROUP')"
            )
        )
        conn.execute(text("DELETE FROM users"))
        conn.execute(
            text(
                "UPDATE accounts SET balance_minor = 0, version = 0 "
                "WHERE account_type IN "
                "('SYSTEM_ISSUANCE', 'ESCROW', 'PENDING_SETTLEMENT')"
            ),
        )
    owner_engine.dispose()
    yield


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 8, 29, 9, 0, 0, tzinfo=UTC))


@pytest.fixture
def unit_of_work(session_factory: sessionmaker[Session]) -> UnitOfWork:
    return UnitOfWork(session_factory, max_attempts=3, base_delay_ms=10)


def _build_transfer_use_case(clock: Clock) -> TransferUseCase:
    """The real object graph, wired once for both fixtures and factories."""
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
                daily_total_minor=50_000_000,
            )
        ),
        clock=clock,
    )


@pytest.fixture
def transfer_use_case(clock: FixedClock) -> TransferUseCase:
    return _build_transfer_use_case(clock)


class UserFactory:
    """Creates a user plus a funded account exactly the way registration does.

    Deliberately routed through ``OpenAccountUseCase`` rather than writing a
    balance directly. An earlier version of this factory did set the balance
    with raw SQL, and the reconciliation assertions correctly failed: the
    account had money with no ledger behind it. Test setup that bypasses the
    ledger would quietly invalidate every invariant the suite claims to prove.
    """

    _counter = 0

    @classmethod
    def create(
        cls,
        session: Session,
        *,
        name: str = "Test User",
        balance_minor: int = 10_000_000,
    ) -> tuple[str, str]:
        """Return ``(user_id, account_id)``."""
        cls._counter += 1
        user_id = new_ulid()
        phone = f"017{cls._counter:08d}"

        session.execute(
            text(
                "INSERT INTO users (id, phone, display_name, password_hash, pin_hash) "
                "VALUES (:id, :phone, :name, 'argon2-placeholder', 'argon2-placeholder')"
            ),
            {"id": user_id, "phone": phone, "name": name},
        )
        session.flush()

        opener = OpenAccountUseCase(
            _build_transfer_use_case(SystemClock()), SqlAccountRepository()
        )
        account_id = opener.execute(
            session,
            user_id=user_id,
            opening_balance=Money.from_minor(balance_minor),
            account_number=f"ACC-{cls._counter:08d}",
            now=datetime.now(UTC),
        )
        return user_id, account_id


@pytest.fixture
def user_factory() -> type[UserFactory]:
    return UserFactory


def money(minor: int) -> Money:
    return Money.from_minor(minor)
