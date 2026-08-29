"""Composition root.

Every dependency in the system is constructed here, once, and injected by
constructor. Spec 7.3 asks for Dependency Injection; this is it, done
explicitly rather than through a container framework.

The trade-off is deliberate. A decorator-driven container would save a few
lines, but it would also make the object graph invisible - you would have to
run the app to discover what depends on what. For a system whose central claim
is that exactly one code path moves money, being able to *read* that the
transfer use case is constructed once, with these collaborators, is worth more
than the brevity.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from modules.financial_core.adapters.persistence.repositories import (
    SqlAccountRepository,
    SqlAuditRepository,
    SqlIdempotencyRepository,
    SqlLedgerRepository,
    SqlOutboxPublisher,
    SqlStatementRepository,
    SqlTransferRepository,
)
from modules.financial_core.application.holding import HoldingService
from modules.financial_core.application.open_account import OpenAccountUseCase
from modules.financial_core.application.transfer import TransferUseCase
from modules.financial_core.application.undo import UndoableTransferService
from modules.financial_core.domain.policies import (
    DefaultLimitPolicy,
    NoFeeStrategy,
    TransferLimits,
)
from modules.identity.adapters.persistence.repositories import (
    SqlSessionRepository,
    SqlUserRepository,
)
from modules.identity.application.auth_service import AuthService
from modules.money_request.adapters.persistence.repositories import (
    SqlMoneyRequestRepository,
)
from modules.money_request.application.money_request_service import MoneyRequestService
from modules.overdraft.adapters.persistence.repositories import SqlOverdraftRepository
from modules.overdraft.application.service import OverdraftService
from modules.reconciliation.service import ReconciliationService
from modules.safepay.adapters.persistence.repositories import SqlSafePayRepository
from modules.safepay.application.service import SafePayService
from platform_.config import Settings
from platform_.database.engine import build_engine, build_session_factory
from platform_.database.unit_of_work import UnitOfWork
from platform_.kernel.clock import Clock, SystemClock
from platform_.kernel.money import BDT, Money
from platform_.scheduling.repository import SqlScheduledTaskRepository
from platform_.security.passwords import PasswordHasherService
from platform_.security.tokens import TokenService
from platform_.web.rate_limit import NoopRateLimiter, RateLimiter, RedisRateLimiter


@dataclass
class Container:
    """Holds the wired object graph for one process."""

    settings: Settings
    clock: Clock

    @cached_property
    def engine(self) -> Engine:
        return build_engine(self.settings)

    @cached_property
    def session_factory(self) -> sessionmaker[Session]:
        return build_session_factory(self.engine)

    @cached_property
    def unit_of_work(self) -> UnitOfWork:
        return UnitOfWork(
            self.session_factory,
            max_attempts=self.settings.transaction_max_attempts,
            base_delay_ms=self.settings.transaction_retry_base_delay_ms,
        )

    # -- shared adapters ---------------------------------------------------
    # Repositories are stateless, so one instance per process is correct and
    # avoids per-request allocation.

    @cached_property
    def accounts(self) -> SqlAccountRepository:
        return SqlAccountRepository()

    @cached_property
    def transfers(self) -> SqlTransferRepository:
        return SqlTransferRepository()

    @cached_property
    def ledger(self) -> SqlLedgerRepository:
        return SqlLedgerRepository()

    @cached_property
    def idempotency(self) -> SqlIdempotencyRepository:
        return SqlIdempotencyRepository()

    @cached_property
    def outbox(self) -> SqlOutboxPublisher:
        return SqlOutboxPublisher()

    @cached_property
    def statements(self) -> SqlStatementRepository:
        return SqlStatementRepository()

    @cached_property
    def audit(self) -> SqlAuditRepository:
        return SqlAuditRepository()

    @cached_property
    def users(self) -> SqlUserRepository:
        return SqlUserRepository()

    @cached_property
    def sessions(self) -> SqlSessionRepository:
        return SqlSessionRepository()

    @cached_property
    def money_requests(self) -> SqlMoneyRequestRepository:
        return SqlMoneyRequestRepository()

    @cached_property
    def scheduled_tasks(self) -> SqlScheduledTaskRepository:
        return SqlScheduledTaskRepository()

    @cached_property
    def safepay_escrows(self) -> SqlSafePayRepository:
        return SqlSafePayRepository()

    @cached_property
    def overdrafts(self) -> SqlOverdraftRepository:
        return SqlOverdraftRepository()

    # -- security ----------------------------------------------------------

    @cached_property
    def rate_limiter(self) -> RateLimiter:
        if not self.settings.rate_limit_enabled:
            return NoopRateLimiter()
        return RedisRateLimiter(self.settings.redis_url)

    @cached_property
    def passwords(self) -> PasswordHasherService:
        return PasswordHasherService()

    @cached_property
    def tokens(self) -> TokenService:
        return TokenService(
            secret=self.settings.jwt_secret,
            algorithm=self.settings.jwt_algorithm,
            access_ttl_seconds=self.settings.access_token_ttl_seconds,
            refresh_ttl_seconds=self.settings.refresh_token_ttl_seconds,
        )

    # -- use cases ---------------------------------------------------------

    @cached_property
    def transfer_use_case(self) -> TransferUseCase:
        """The single choke point through which all money moves."""
        return TransferUseCase(
            account_locker=self.accounts,
            account_writer=self.accounts,
            transfer_writer=self.transfers,
            transfer_reader=self.transfers,
            ledger_writer=self.ledger,
            idempotency_store=self.idempotency,
            event_publisher=self.outbox,
            audit_recorder=self.audit,
            fee_strategy=NoFeeStrategy(),
            limit_policy=DefaultLimitPolicy(
                TransferLimits(
                    max_single_transfer_minor=self.settings.max_transfer_amount_minor,
                    daily_total_minor=self.settings.daily_transfer_limit_minor,
                )
            ),
            clock=self.clock,
        )

    @cached_property
    def open_account_use_case(self) -> OpenAccountUseCase:
        return OpenAccountUseCase(self.transfer_use_case, self.accounts)

    @cached_property
    def holding_service(self) -> HoldingService:
        return HoldingService(
            transfer_use_case=self.transfer_use_case,
            transfers=self.transfers,
            clock=self.clock,
        )

    @cached_property
    def undoable_transfers(self) -> UndoableTransferService:
        return UndoableTransferService(
            holding=self.holding_service,
            transfers=self.transfers,
            accounts=self.accounts,
            scheduler=self.scheduled_tasks,
            idempotency=self.idempotency,
            clock=self.clock,
        )

    @cached_property
    def auth_service(self) -> AuthService:
        return AuthService(
            users=self.users,
            sessions=self.sessions,
            open_account=self.open_account_use_case,
            passwords=self.passwords,
            tokens=self.tokens,
            clock=self.clock,
            opening_balance=Money.from_minor(self.settings.opening_balance_minor, BDT),
            pin_max_attempts=self.settings.pin_max_attempts,
            pin_lockout_seconds=self.settings.pin_lockout_seconds,
            access_ttl_seconds=self.settings.access_token_ttl_seconds,
        )

    @cached_property
    def money_request_service(self) -> MoneyRequestService:
        return MoneyRequestService(
            requests=self.money_requests,
            accounts=self.accounts,
            transfer_use_case=self.transfer_use_case,
            event_publisher=self.outbox,
            audit_recorder=self.audit,
            clock=self.clock,
            expiry_hours=self.settings.money_request_expiry_hours,
        )

    @cached_property
    def safepay_service(self) -> SafePayService:
        return SafePayService(
            escrows=self.safepay_escrows,
            accounts=self.accounts,
            users=self.users,
            sessions=self.sessions,
            holding=self.holding_service,
            scheduler=self.scheduled_tasks,
            idempotency=self.idempotency,
            audit=self.audit,
            passwords=self.passwords,
            clock=self.clock,
            # Domain-separated HMAC use means the JWT itself is never used as
            # a delivery code and the code is not recoverable from the DB.
            delivery_code_secret=f"{self.settings.jwt_secret}:safepay",
            auto_release_hours=self.settings.safepay_auto_release_hours,
        )

    @cached_property
    def overdraft_service(self) -> OverdraftService:
        return OverdraftService(
            repository=self.overdrafts,
            accounts=self.accounts,
            users=self.users,
            transfer_use_case=self.transfer_use_case,
            undoable_transfers=self.undoable_transfers,
            idempotency=self.idempotency,
            audit=self.audit,
            clock=self.clock,
            max_draw_minor=self.settings.overdraft_max_draw_minor,
            lien_sweep_basis_points=self.settings.overdraft_lien_sweep_basis_points,
        )

    @cached_property
    def reconciliation_service(self) -> ReconciliationService:
        return ReconciliationService()

    def dispose(self) -> None:
        """Spec 20.3: close pools cleanly on shutdown."""
        if "engine" in self.__dict__:
            self.engine.dispose()
        limiter = self.__dict__.get("rate_limiter")
        if isinstance(limiter, RedisRateLimiter):
            limiter.close()


def build_container(settings: Settings) -> Container:
    container = Container(settings=settings, clock=SystemClock())
    # Wire the only deliberate cycle after both services exist: all incoming
    # transfers notify the overdraft lien, and the lien posts repayment legs
    # through the same TransferUseCase.
    container.transfer_use_case.set_credit_interceptor(container.overdraft_service)
    return container
