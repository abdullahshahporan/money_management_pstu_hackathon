"""The money-request workflow (spec 13).

"My friend owes me BDT 1,200. I want to collect it through the application."

A money request is a *conversation*, not money. It never touches a balance
itself; accepting one calls the financial core's transfer use case, which
remains the only thing in the system that can move value (spec 6.2).

The interesting part is concurrency. A request has exactly one non-terminal
state, PENDING, and every transition out of it is a conditional UPDATE guarded
by ``status = 'PENDING'``. Accept, reject, cancel and the expiry sweep can all
race each other; exactly one wins, and the losers are told the request was
already handled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from modules.financial_core.application.idempotency import fingerprint_request
from modules.financial_core.application.transfer import (
    TransferCommand,
    TransferKind,
    TransferUseCase,
)
from modules.financial_core.domain.events import (
    MoneyRequestAccepted,
    MoneyRequestCreated,
    MoneyRequestRejected,
)
from platform_.kernel.clock import Clock
from platform_.kernel.errors import (
    AuthorizationError,
    NotFoundError,
    SelfTransferError,
    StateConflictError,
    ValidationError,
)
from platform_.kernel.ids import generate_reference, new_ulid
from platform_.kernel.money import Money

logger = logging.getLogger(__name__)

__all__ = ["MoneyRequestService"]


@dataclass(frozen=True, slots=True)
class MoneyRequestResult:
    request_id: str
    reference: str
    status: str
    transfer_reference: str | None = None


class MoneyRequestService:
    def __init__(
        self,
        *,
        requests: Any,
        accounts: Any,
        transfer_use_case: TransferUseCase,
        event_publisher: Any,
        audit_recorder: Any,
        clock: Clock,
        expiry_hours: int = 72,
    ) -> None:
        self._requests = requests
        self._accounts = accounts
        self._transfers = transfer_use_case
        self._events = event_publisher
        self._audit = audit_recorder
        self._clock = clock
        self._expiry = timedelta(hours=expiry_hours)

    # -- create ------------------------------------------------------------

    def create(
        self,
        session: Session,
        *,
        requester_user_id: str,
        payer_phone: str,
        amount: Money,
        note: str | None = None,
        request_id: str | None = None,
    ) -> MoneyRequestResult:
        now = self._clock.now()

        requester = self._accounts.get_by_user_id(session, requester_user_id)
        if requester is None:
            raise NotFoundError("Your account could not be found.")

        payer = self._accounts.get_by_phone(session, payer_phone)
        if payer is None:
            raise NotFoundError("No user found with that phone number.")

        if payer.id == requester.id:
            raise SelfTransferError("You cannot request money from yourself.")

        requester.ensure_active()
        payer.ensure_active()
        if requester.balance.currency != payer.balance.currency:
            raise ValidationError("Both accounts must hold the same currency.")

        money_request_id = new_ulid()
        reference = generate_reference("REQ", now=now)

        self._requests.create(
            session,
            request_id=money_request_id,
            reference=reference,
            requester_account_id=requester.id,
            payer_account_id=payer.id,
            amount=amount,
            note=note,
            expires_at=now + self._expiry,
            now=now,
        )

        self._events.append(
            session,
            MoneyRequestCreated(
                aggregate_id=money_request_id,
                occurred_at=now,
                payload={
                    "requestId": money_request_id,
                    "reference": reference,
                    "requesterUserId": requester_user_id,
                    "payerUserId": payer.user_id,
                    "amountMinor": amount.minor,
                    "currency": amount.currency.code,
                    "note": note,
                },
            ),
            trace_id=request_id,
        )
        self._audit.record(
            session,
            actor_user_id=requester_user_id,
            action="MONEY_REQUEST_CREATED",
            resource_type="money_request",
            resource_id=money_request_id,
            request_id=request_id,
            metadata={"amountMinor": amount.minor, "reference": reference},
            now=now,
        )
        return MoneyRequestResult(
            request_id=money_request_id, reference=reference, status="PENDING"
        )

    # -- accept ------------------------------------------------------------

    def accept(
        self,
        session: Session,
        *,
        request_id: str,
        payer_user_id: str,
        idempotency_key: str,
        api_request_id: str | None = None,
    ) -> MoneyRequestResult:
        """Pay a pending request. Atomic with the transfer it causes."""
        now = self._clock.now()

        # Lock first: this serialises concurrent accept/reject/cancel on the
        # same request so they resolve in a defined order rather than
        # interleaving.
        record = self._requests.lock_for_update(session, request_id)
        if record is None:
            raise NotFoundError("Money request not found.")

        payer_account = self._accounts.get_by_id(session, record.payer_account_id)
        if payer_account is None or payer_account.user_id != payer_user_id:
            # Not the payer. 404 rather than 403 - revealing that a request
            # exists between two other people is itself a leak (spec 21.2).
            raise NotFoundError("Money request not found.")

        if record.status != "PENDING":
            raise StateConflictError(
                f"This request was already {record.status.lower()}.",
                details={"status": record.status},
            )

        if record.expires_at <= now:
            self._requests.mark_terminal(
                session, request_id=request_id, status="EXPIRED", now=now
            )
            raise StateConflictError("This request has expired.")

        amount = Money.from_minor(record.amount_minor)

        # The money moves through the same use case as any other transfer -
        # same locking, same ledger posting, same idempotency guarantees.
        transfer = self._transfers.execute(
            session,
            TransferCommand(
                actor_user_id=payer_user_id,
                sender_account_id=record.payer_account_id,
                receiver_account_id=record.requester_account_id,
                amount=amount,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint_request(
                    {"moneyRequestId": request_id, "amountMinor": amount.minor}
                ),
                kind=TransferKind.REQUEST_SETTLEMENT,
                note=record.note,
                request_id=api_request_id,
                money_request_id=request_id,
            ),
        )

        # Belt and braces: we hold the row lock, so this cannot lose - but the
        # predicate means that even if the lock were ever removed, a second
        # settlement would still be impossible. The UNIQUE constraint on
        # transfer_id is the third layer.
        updated = self._requests.mark_accepted(
            session, request_id=request_id, transfer_id=transfer.transfer_id, now=now
        )
        if updated != 1:
            raise StateConflictError("This request has already been handled.")

        self._events.append(
            session,
            MoneyRequestAccepted(
                aggregate_id=request_id,
                occurred_at=now,
                payload={
                    "requestId": request_id,
                    "transferId": transfer.transfer_id,
                    "transferReference": transfer.reference,
                    "payerUserId": payer_user_id,
                    "amountMinor": amount.minor,
                },
            ),
            trace_id=api_request_id,
        )
        self._audit.record(
            session,
            actor_user_id=payer_user_id,
            action="MONEY_REQUEST_ACCEPTED",
            resource_type="money_request",
            resource_id=request_id,
            request_id=api_request_id,
            metadata={"transferId": transfer.transfer_id},
            now=now,
        )
        return MoneyRequestResult(
            request_id=request_id,
            reference=record.reference,
            status="ACCEPTED",
            transfer_reference=transfer.reference,
        )

    # -- reject / cancel ---------------------------------------------------

    def reject(
        self,
        session: Session,
        *,
        request_id: str,
        payer_user_id: str,
        api_request_id: str | None = None,
    ) -> MoneyRequestResult:
        return self._terminate(
            session,
            request_id=request_id,
            actor_user_id=payer_user_id,
            new_status="REJECTED",
            actor_role="payer",
            api_request_id=api_request_id,
        )

    def cancel(
        self,
        session: Session,
        *,
        request_id: str,
        requester_user_id: str,
        api_request_id: str | None = None,
    ) -> MoneyRequestResult:
        return self._terminate(
            session,
            request_id=request_id,
            actor_user_id=requester_user_id,
            new_status="CANCELLED",
            actor_role="requester",
            api_request_id=api_request_id,
        )

    def _terminate(
        self,
        session: Session,
        *,
        request_id: str,
        actor_user_id: str,
        new_status: str,
        actor_role: str,
        api_request_id: str | None,
    ) -> MoneyRequestResult:
        now = self._clock.now()
        record = self._requests.lock_for_update(session, request_id)
        if record is None:
            raise NotFoundError("Money request not found.")

        expected_account_id = (
            record.payer_account_id
            if actor_role == "payer"
            else record.requester_account_id
        )
        account = self._accounts.get_by_id(session, expected_account_id)
        if account is None or account.user_id != actor_user_id:
            raise AuthorizationError(
                "Only the payer may reject this request."
                if actor_role == "payer"
                else "Only the requester may cancel this request."
            )

        if self._requests.mark_terminal(
            session, request_id=request_id, status=new_status, now=now
        ) != 1:
            raise StateConflictError(
                f"This request was already {record.status.lower()}.",
                details={"status": record.status},
            )

        if new_status == "REJECTED":
            self._events.append(
                session,
                MoneyRequestRejected(
                    aggregate_id=request_id,
                    occurred_at=now,
                    payload={"requestId": request_id, "payerUserId": actor_user_id},
                ),
                trace_id=api_request_id,
            )

        self._audit.record(
            session,
            actor_user_id=actor_user_id,
            action=f"MONEY_REQUEST_{new_status}",
            resource_type="money_request",
            resource_id=request_id,
            request_id=api_request_id,
            metadata=None,
            now=now,
        )
        return MoneyRequestResult(
            request_id=request_id, reference=record.reference, status=new_status
        )

    # -- queries -----------------------------------------------------------

    def list_for_user(
        self,
        session: Session,
        *,
        user_id: str,
        direction: str,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        account = self._accounts.get_by_user_id(session, user_id)
        if account is None:
            raise NotFoundError("Your account could not be found.")
        return self._requests.list_for_account(
            session,
            account_id=account.id,
            direction=direction,
            status=status,
            limit=limit,
            cursor=cursor,
        )

    def expire_due(self, session: Session) -> int:
        return self._requests.expire_due(session, now=self._clock.now())
