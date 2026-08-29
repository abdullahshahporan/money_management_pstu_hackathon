"""Two-stage money movement: hold now, settle or refund later.

Three features defer settlement - the 10-second undo window, safe-pay escrow,
and any future review hold. They all need the same thing, and they all get it
here rather than each inventing their own.

**The money really moves at hold time.** It leaves the payer's account and
lands in a holding account. It would be far simpler to leave it where it is
and set a ``reserved`` flag, and that would be wrong: a flag does not stop the
next concurrent transfer from spending the same taka, because the balance
check reads the balance, not the flag. Moving the money makes the ordinary
balance check do the enforcing, with no new rule to remember.

**Exactly one outcome wins.** A user tapping Undo at the same instant the
timer fires is a genuine race between two transactions. It is settled by a
conditional UPDATE on the hold's status - the same latch the money-request
workflow already uses. Whoever changes the row from its pending status owns
the outcome; the loser sees zero rows updated and is told the transfer was
already resolved. Because the latch and the money movement share one
transaction, there is no window where the status says one thing and the ledger
another.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from modules.financial_core.application.idempotency import fingerprint_request
from modules.financial_core.application.transfer import (
    TransferCommand,
    TransferResult,
    TransferUseCase,
)
from platform_.kernel.clock import Clock
from platform_.kernel.errors import StateConflictError
from platform_.kernel.money import Money

logger = logging.getLogger(__name__)

__all__ = ["HoldOutcome", "HoldingService"]


@dataclass(frozen=True, slots=True)
class HoldOutcome:
    hold: TransferResult
    settlement: TransferResult | None = None


class HoldingService:
    def __init__(
        self,
        *,
        transfer_use_case: TransferUseCase,
        transfers: object,
        clock: Clock,
    ) -> None:
        self._transfers_use_case = transfer_use_case
        self._transfers = transfers
        self._clock = clock

    def get_hold(self, session: Session, transfer_id: str):  # noqa: ANN201
        return self._transfers.get(session, transfer_id)

    def hold(
        self,
        session: Session,
        *,
        actor_user_id: str,
        payer_account_id: str,
        holding_account_id: str,
        amount: Money,
        kind: str,
        pending_status: str,
        idempotency_key: str,
        note: str | None = None,
        request_id: str | None = None,
        enforce_limits: bool = True,
        intended_receiver_account_id: str | None = None,
        idempotency_endpoint: str = "INTERNAL:/holds",
    ) -> TransferResult:
        """Move money out of the payer and into a holding account."""
        return self._transfers_use_case.execute(
            session,
            TransferCommand(
                actor_user_id=actor_user_id,
                sender_account_id=payer_account_id,
                receiver_account_id=holding_account_id,
                amount=amount,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint_request(
                    {
                        "payer": payer_account_id,
                        "holding": holding_account_id,
                        "amountMinor": amount.minor,
                        "kind": kind,
                    }
                ),
                kind=kind,
                note=note,
                request_id=request_id,
                enforce_limits=enforce_limits,
                initial_status=pending_status,
                intended_receiver_account_id=intended_receiver_account_id,
                idempotency_endpoint=idempotency_endpoint,
            ),
        )

    def settle(
        self,
        session: Session,
        *,
        hold_transfer_id: str,
        hold_reference: str,
        from_status: str,
        to_status: str,
        beneficiary_account_id: str,
        holding_account_id: str,
        amount: Money,
        kind: str,
        actor_user_id: str,
        note: str | None = None,
        request_id: str | None = None,
    ) -> TransferResult:
        """Complete a hold by paying the beneficiary, or refunding the payer.

        ``beneficiary_account_id`` is the payee for a release and the original
        payer for a refund - the mechanics are identical, only the destination
        differs, which is why refund is not a separate method.
        """
        # Latch first. This is the race winner: whoever flips the status owns
        # the outcome. Doing the money movement first would let both a timer
        # and a user Undo each post a settlement leg before either noticed the
        # other, paying the money out twice.
        if self._transfers.try_transition_status(
            session,
            transfer_id=hold_transfer_id,
            from_status=from_status,
            to_status=to_status,
        ) != 1:
            raise StateConflictError(
                "This transfer has already been settled or cancelled.",
                details={"reference": hold_reference},
            )

        # Deterministic key derived from the hold and the outcome. A retried
        # settlement tick can never pay twice, and a settle and a refund of the
        # same hold are distinct intents.
        settlement_key = f"settle:{hold_transfer_id}:{to_status}"

        return self._transfers_use_case.execute(
            session,
            TransferCommand(
                actor_user_id=actor_user_id,
                sender_account_id=holding_account_id,
                receiver_account_id=beneficiary_account_id,
                amount=amount,
                idempotency_key=settlement_key,
                request_fingerprint=fingerprint_request(
                    {"hold": hold_transfer_id, "outcome": to_status}
                ),
                kind=kind,
                note=note,
                request_id=request_id,
                # A holding account pays out only what was paid in, and the
                # payer's limits were already applied at hold time. Applying
                # them again here could strand money in escrow.
                enforce_limits=False,
                parent_transfer_id=hold_transfer_id,
                idempotency_endpoint=f"INTERNAL:/holds/{kind.lower()}",
            ),
        )
