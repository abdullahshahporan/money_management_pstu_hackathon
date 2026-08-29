"""Ports for the financial core.

Spec 7.3 (Dependency Inversion): the use case depends on these Protocols;
PostgreSQL is one implementation of them. Spec 7.3 (Interface Segregation):
they are deliberately several small ports rather than one repository
god-object, so a caller that only appends ledger entries cannot accidentally
reach for a method that moves a balance.

Every method takes the ``session`` explicitly. That is not incidental: it is
how the use case guarantees that every write in a transfer shares one
transaction (spec 7.5). A repository that opened its own connection could not
participate in the atomic boundary, and the bug would be invisible until a
partial failure in production.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

from modules.financial_core.application.idempotency import IdempotencyDecision
from modules.financial_core.domain.account import Account
from modules.financial_core.domain.events import DomainEvent
from modules.financial_core.domain.ledger import LedgerPosting
from platform_.kernel.money import Money


class AccountReader(Protocol):
    def get_by_id(self, session: Session, account_id: str) -> Account | None: ...

    def get_by_user_id(self, session: Session, user_id: str) -> Account | None: ...

    def get_by_phone(self, session: Session, phone: str) -> Account | None: ...


class AccountLocker(Protocol):
    def lock_for_update(
        self, session: Session, account_ids: list[str]
    ) -> dict[str, Account]:
        """Lock the given accounts with ``SELECT ... FOR UPDATE``.

        Implementations MUST lock in ascending account-id order regardless of
        the order requested. That single rule is what prevents the classic
        A-to-B / B-to-A deadlock (spec 11.3).
        """
        ...


class AccountWriter(Protocol):
    def persist_balance(self, session: Session, account: Account) -> None: ...


class AccountCreator(Protocol):
    def create(
        self,
        session: Session,
        *,
        account_id: str,
        user_id: str,
        account_number: str,
        currency: str,
        now: datetime,
    ) -> None:
        """Create an ACTIVE user account with a zero balance.

        Zero, always. Funding is a ledger posting, never an opening value
        written straight into the balance column (spec 8.3).
        """
        ...


class LedgerWriter(Protocol):
    def post(
        self,
        session: Session,
        *,
        posting: LedgerPosting,
        transfer_id: str,
        posting_type: str,
        description: str | None,
        balances_after: dict[str, Money],
        now: datetime,
    ) -> str:
        """Append one balanced ledger transaction. Returns its id."""
        ...


class TransferWriter(Protocol):
    def create(
        self,
        session: Session,
        *,
        transfer_id: str,
        reference: str,
        sender_account_id: str,
        receiver_account_id: str,
        amount: Money,
        kind: str,
        note: str | None,
        initiated_by_user_id: str | None,
        request_id: str | None,
        money_request_id: str | None,
        now: datetime,
    ) -> None: ...


class TransferReader(Protocol):
    def sum_sent_since(
        self, session: Session, account_id: str, since: datetime
    ) -> Money: ...

    def get_by_reference(self, session: Session, reference: str) -> dict[str, Any] | None: ...


class IdempotencyStore(Protocol):
    def reserve(
        self,
        session: Session,
        *,
        actor_id: str,
        endpoint: str,
        key: str,
        fingerprint: str,
        now: datetime,
    ) -> IdempotencyDecision: ...

    def complete(
        self,
        session: Session,
        *,
        actor_id: str,
        endpoint: str,
        key: str,
        resource_id: str,
        http_status: int,
        response_body: dict[str, Any],
        now: datetime,
    ) -> None: ...


class EventPublisher(Protocol):
    """Appends to the transactional outbox - never talks to a broker directly.

    Publishing to a broker inside the money transaction would reintroduce the
    dual-write problem the outbox exists to solve (spec 15.1), and would hold
    database locks open across a network call (spec 20.2).
    """

    def append(
        self, session: Session, event: DomainEvent, *, trace_id: str | None
    ) -> None: ...


class AuditRecorder(Protocol):
    def record(
        self,
        session: Session,
        *,
        actor_user_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        request_id: str | None,
        metadata: dict[str, Any] | None,
        now: datetime,
    ) -> None: ...

__all__ = [
    "AccountCreator",
    "AccountLocker",
    "AccountReader",
    "AccountWriter",
    "AuditRecorder",
    "EventPublisher",
    "IdempotencyDecision",
    "IdempotencyStore",
    "LedgerWriter",
    "TransferReader",
    "TransferWriter",
]
