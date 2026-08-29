"""Send with a short undo window.

A mistyped recipient or a wrong amount is the most common way people lose
money in a payment app, and it is not a systems failure - it is a human one.
This gives the sender a few seconds to take it back.

Two things make it honest rather than cosmetic:

*   **The money is genuinely gone from the sender.** It sits in the
    PENDING_SETTLEMENT account, not in the sender's balance behind a flag, so
    the sender cannot spend it twice while the window is open.
*   **The receiver is not paid yet, and is not told they were.** Showing an
    incoming payment that can still evaporate would be worse than showing
    nothing. The credit appears when it settles.

The cost is real and worth stating: every transfer is non-final for the length
of the window. That is the trade this feature makes - a few seconds of
uncertainty for the receiver, in exchange for recoverability for the sender.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from modules.financial_core.application.holding import HoldingService
from modules.financial_core.application.idempotency import (
    fingerprint_request,
    replay_body_or_raise,
)
from modules.financial_core.application.transfer import TransferKind, TransferResult
from modules.financial_core.domain.account import PENDING_SETTLEMENT_ACCOUNT_ID
from platform_.kernel.clock import Clock
from platform_.kernel.errors import NotFoundError, StateConflictError
from platform_.kernel.money import Money

logger = logging.getLogger(__name__)

__all__ = ["UNDO_WINDOW_SECONDS", "UndoableTransferService"]

UNDO_WINDOW_SECONDS = 10
UNDO_SETTLE_TASK = "UNDO_SETTLE"

PENDING = "PENDING_UNDO"
SETTLED = "SUCCEEDED"
REFUNDED = "REFUNDED"


class UndoableTransferService:
    def __init__(
        self,
        *,
        holding: HoldingService,
        transfers: object,
        accounts: object,
        scheduler: object,
        idempotency: object,
        clock: Clock,
        window_seconds: int = UNDO_WINDOW_SECONDS,
    ) -> None:
        self._holding = holding
        self._transfers = transfers
        self._accounts = accounts
        self._scheduler = scheduler
        self._idempotency = idempotency
        self._clock = clock
        self._window_seconds = window_seconds

    def send(
        self,
        session: Session,
        *,
        actor_user_id: str,
        sender_account_id: str,
        receiver_account_id: str,
        amount: Money,
        idempotency_key: str,
        note: str | None = None,
        request_id: str | None = None,
        idempotency_endpoint: str = "POST:/api/v1/transfers",
    ) -> TransferResult:
        """Debit the sender into the holding account and start the clock."""
        hold = self._holding.hold(
            session,
            actor_user_id=actor_user_id,
            payer_account_id=sender_account_id,
            holding_account_id=PENDING_SETTLEMENT_ACCOUNT_ID,
            amount=amount,
            kind=TransferKind.UNDO_HOLD,
            pending_status=PENDING,
            idempotency_key=idempotency_key,
            note=note,
            request_id=request_id,
            intended_receiver_account_id=receiver_account_id,
            idempotency_endpoint=idempotency_endpoint,
        )

        # A replayed request must not queue a second settlement. The scheduler
        # is idempotent on (task_type, resource_id) as well, so this is belt
        # and braces rather than the only guard.
        if not hold.replayed:
            self._scheduler.schedule(
                session,
                task_type=UNDO_SETTLE_TASK,
                resource_id=hold.transfer_id,
                delay_seconds=self._window_seconds,
            )
            logger.info(
                "transfer_held_for_undo",
                extra={
                    "transfer_id": hold.transfer_id,
                    "window_seconds": self._window_seconds,
                },
            )
        return hold

    def undo(
        self,
        session: Session,
        *,
        transfer_id: str,
        actor_user_id: str,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> dict:
        """Take it back, if the timer has not already settled it."""
        now = self._clock.now()
        endpoint = f"POST:/api/v1/transfers/{transfer_id}/undo"
        decision = self._idempotency.reserve(
            session,
            actor_id=actor_user_id,
            endpoint=endpoint,
            key=idempotency_key,
            fingerprint=fingerprint_request({"transferId": transfer_id}),
            now=now,
        )
        replay = replay_body_or_raise(decision)
        if replay is not None:
            replay["replayed"] = True
            return replay

        hold = self._require_hold(session, transfer_id)

        sender = self._accounts.get_by_id(session, hold.sender_account_id)
        if sender is None or sender.user_id != actor_user_id:
            # Not the sender's transfer. 404 over 403: confirming that a
            # transfer id exists is itself a disclosure.
            raise NotFoundError("Transfer not found.")

        settlement = self._holding.settle(
            session,
            hold_transfer_id=hold.id,
            hold_reference=hold.reference,
            from_status=PENDING,
            to_status=REFUNDED,
            beneficiary_account_id=hold.sender_account_id,
            holding_account_id=PENDING_SETTLEMENT_ACCOUNT_ID,
            amount=Money.from_minor(hold.amount_minor),
            kind=TransferKind.UNDO_REFUND,
            actor_user_id=actor_user_id,
            note="Undone by sender",
            request_id=request_id,
        )
        # Best-effort. If the worker already claimed the row this changes
        # nothing, and that is fine - the status latch above already decided
        # the outcome, so the claimed task will simply find nothing to do.
        self._scheduler.cancel(
            session, task_type=UNDO_SETTLE_TASK, resource_id=hold.id
        )
        logger.info("transfer_undone", extra={"transfer_id": hold.id})
        response = {
            "transferId": hold.id,
            "reference": hold.reference,
            "status": REFUNDED,
            "refundTransferId": settlement.transfer_id,
            "refundReference": settlement.reference,
            "amountMinor": settlement.amount.minor,
            "currency": settlement.amount.currency.code,
            "senderBalanceMinor": settlement.receiver_balance_after.minor,
            "replayed": False,
        }
        self._idempotency.complete(
            session,
            actor_id=actor_user_id,
            endpoint=endpoint,
            key=idempotency_key,
            resource_id=hold.id,
            http_status=200,
            response_body=response,
            now=now,
        )
        return response

    def undo_deadline(self, hold: TransferResult) -> str:
        return (hold.completed_at + timedelta(seconds=self._window_seconds)).isoformat()

    def settle(
        self, session: Session, *, transfer_id: str
    ) -> TransferResult | None:
        """Finalise a hold whose window has elapsed. Called by the scheduler.

        Returns None if the sender already undid it - losing that race is a
        normal outcome, not an error, so the task completes successfully.
        """
        hold = self._transfers.get(session, transfer_id)
        if hold is None:
            return None
        if hold.status != PENDING:
            logger.info(
                "undo_settlement_skipped",
                extra={"transfer_id": transfer_id, "status": hold.status},
            )
            return None
        if hold.intended_receiver_account_id is None:
            raise StateConflictError("Held transfer has no intended receiver.")

        settlement = self._holding.settle(
            session,
            hold_transfer_id=hold.id,
            hold_reference=hold.reference,
            from_status=PENDING,
            to_status=SETTLED,
            beneficiary_account_id=hold.intended_receiver_account_id,
            holding_account_id=PENDING_SETTLEMENT_ACCOUNT_ID,
            amount=Money.from_minor(hold.amount_minor),
            kind=TransferKind.UNDO_SETTLE,
            # The holding account has no owner; the system settles it once the
            # window has elapsed.
            actor_user_id=hold.initiated_by_user_id or "",
            note=hold.note,
        )
        logger.info("transfer_settled", extra={"transfer_id": hold.id})
        return settlement

    def _require_hold(self, session: Session, transfer_id: str):  # noqa: ANN202
        hold = self._transfers.get(session, transfer_id)
        if hold is None or hold.kind != TransferKind.UNDO_HOLD:
            raise NotFoundError("Transfer not found.")
        if hold.status != PENDING:
            raise StateConflictError(
                "This transfer can no longer be undone - it has already been "
                f"{hold.status.lower()}.",
                details={"status": hold.status},
            )
        return hold
