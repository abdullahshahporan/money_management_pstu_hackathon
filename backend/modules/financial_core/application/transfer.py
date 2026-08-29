"""The transfer use case - the only code path that moves money.

Spec 6.2: balance, transfer, ledger, idempotency and outbox stay inside one
bounded context sharing one ACID transaction. Splitting them across services
would turn a safe local commit into a distributed transaction requiring sagas
and compensation, and compensation is not equivalent to atomicity because
other operations can observe the intermediate state.

Every money movement in this system - peer-to-peer send, money-request
settlement, signup grant, reversal - funnels through ``TransferUseCase``. That
is deliberate: it means the invariants are enforced in exactly one place, and
a new feature cannot accidentally invent a second way to change a balance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from modules.financial_core.application.idempotency import (
    IdempotencyDecision,
    IdempotencyOutcome,
)
from modules.financial_core.application.ports import (
    AccountLocker,
    AccountWriter,
    AuditRecorder,
    EventPublisher,
    IdempotencyStore,
    LedgerWriter,
    TransferReader,
    TransferWriter,
)
from modules.financial_core.domain.account import Account
from modules.financial_core.domain.events import TransferSucceeded
from modules.financial_core.domain.ledger import LedgerPosting
from modules.financial_core.domain.policies import FeeStrategy, LimitPolicy
from platform_.kernel.clock import Clock
from platform_.kernel.errors import (
    IdempotencyKeyReuseError,
    NotFoundError,
    RequestInProgressError,
    SelfTransferError,
)
from platform_.kernel.ids import generate_reference, new_ulid
from platform_.kernel.money import Money

logger = logging.getLogger(__name__)

TRANSFER_ENDPOINT = "POST:/api/v1/transfers"


class TransferKind:
    P2P_SEND = "P2P_SEND"
    REQUEST_SETTLEMENT = "REQUEST_SETTLEMENT"
    SIGNUP_GRANT = "SIGNUP_GRANT"
    REVERSAL = "REVERSAL"
    UNDO_HOLD = "UNDO_HOLD"
    UNDO_SETTLE = "UNDO_SETTLE"
    UNDO_REFUND = "UNDO_REFUND"
    ESCROW_HOLD = "ESCROW_HOLD"
    ESCROW_RELEASE = "ESCROW_RELEASE"
    ESCROW_REFUND = "ESCROW_REFUND"
    GROUP_DEPOSIT = "GROUP_DEPOSIT"
    GROUP_WITHDRAWAL = "GROUP_WITHDRAWAL"
    LINK_PAYMENT = "LINK_PAYMENT"
    OVERDRAFT_DRAW = "OVERDRAFT_DRAW"
    OVERDRAFT_REPAY = "OVERDRAFT_REPAY"
    OVERDRAFT_FUND = "OVERDRAFT_FUND"


# Movements the system performs on its own initiative, where the paying
# account has no owning user to authorise it: releasing escrow, settling an
# undo window, paying out a quorum-approved group withdrawal, issuing the
# signup grant. Each is only reachable after its own rule was satisfied
# elsewhere - a quorum, a delivery code, an elapsed timer - so the payer
# authorisation that guards ordinary transfers does not apply.
SYSTEM_INITIATED_KINDS = frozenset(
    {
        TransferKind.SIGNUP_GRANT,
        TransferKind.UNDO_SETTLE,
        TransferKind.UNDO_REFUND,
        TransferKind.ESCROW_RELEASE,
        TransferKind.ESCROW_REFUND,
        TransferKind.GROUP_WITHDRAWAL,
    }
)


@dataclass(frozen=True, slots=True)
class TransferCommand:
    """One intent to move money."""

    actor_user_id: str
    sender_account_id: str
    receiver_account_id: str
    amount: Money
    idempotency_key: str
    request_fingerprint: str
    kind: str = TransferKind.P2P_SEND
    note: str | None = None
    request_id: str | None = None
    money_request_id: str | None = None
    enforce_limits: bool = True
    # A hold lands as PENDING_UNDO or HELD rather than SUCCEEDED: the money has
    # really moved into a holding account, but the payee has not been paid yet.
    initial_status: str = "SUCCEEDED"
    # Set on a settlement leg to link it back to the hold it completes.
    parent_transfer_id: str | None = None
    # Set on a hold: where the money is ultimately meant to go.
    intended_receiver_account_id: str | None = None
    # Internal feature legs must not share the public transfer endpoint's
    # idempotency namespace.  A mobile client can legitimately use the same
    # UUID for a SafePay command and an ordinary send.
    idempotency_endpoint: str = TRANSFER_ENDPOINT


@dataclass(frozen=True, slots=True)
class TransferResult:
    transfer_id: str
    reference: str
    status: str
    amount: Money
    fee: Money
    sender_balance_after: Money
    receiver_balance_after: Money
    completed_at: datetime
    replayed: bool = False

    def to_response(self) -> dict[str, Any]:
        return {
            "transferId": self.transfer_id,
            "reference": self.reference,
            "status": self.status,
            "amountMinor": self.amount.minor,
            "feeMinor": self.fee.minor,
            "currency": self.amount.currency.code,
            "senderBalanceMinor": self.sender_balance_after.minor,
            "completedAt": self.completed_at.isoformat(),
        }


class TransferUseCase:
    """Executes a transfer atomically, exactly once."""

    def __init__(
        self,
        *,
        account_locker: AccountLocker,
        account_writer: AccountWriter,
        transfer_writer: TransferWriter,
        transfer_reader: TransferReader,
        ledger_writer: LedgerWriter,
        idempotency_store: IdempotencyStore,
        event_publisher: EventPublisher,
        audit_recorder: AuditRecorder,
        fee_strategy: FeeStrategy,
        limit_policy: LimitPolicy,
        clock: Clock,
        credit_interceptor: object | None = None,
    ) -> None:
        self._accounts = account_locker
        self._account_writer = account_writer
        self._transfers = transfer_writer
        self._transfer_reader = transfer_reader
        self._ledger = ledger_writer
        self._idempotency = idempotency_store
        self._events = event_publisher
        self._audit = audit_recorder
        self._fees = fee_strategy
        self._limits = limit_policy
        self._clock = clock
        self._credit_interceptor = credit_interceptor

    def set_credit_interceptor(self, interceptor: object) -> None:
        """Install the overdraft lien hook at the composition root.

        It is a setter only to break the construction cycle: the interceptor
        itself needs this use case to post repayment legs.  Runtime requests
        never mutate it.
        """
        self._credit_interceptor = interceptor

    def execute(self, session: Session, command: TransferCommand) -> TransferResult:
        """Run one transfer inside the caller's transaction.

        The caller supplies the session and owns the commit, so this whole
        method is one atomic unit: either every row below exists afterwards,
        or none of them do.
        """
        now = self._clock.now()

        # -- 1. Idempotency, before anything else --------------------------
        # Reserving first means a duplicate never even reaches the locking
        # stage, so retries cost almost nothing and cannot contend for rows.
        decision = self._idempotency.reserve(
            session,
            actor_id=command.actor_user_id,
            endpoint=command.idempotency_endpoint,
            key=command.idempotency_key,
            fingerprint=command.request_fingerprint,
            now=now,
        )
        replay = self._handle_idempotency_decision(decision)
        if replay is not None:
            return replay

        # -- 2. Lock both accounts, in a deterministic order ---------------
        # Ascending account id, always - see AccountLocker.lock_for_update.
        # Without this, A->B and B->A running concurrently deadlock.
        if command.sender_account_id == command.receiver_account_id:
            raise SelfTransferError

        extra_lock_ids: list[str] = []
        if self._credit_interceptor is not None:
            extra_lock_ids = self._credit_interceptor.additional_lock_ids(
                session,
                receiver_account_id=command.receiver_account_id,
                transfer_kind=command.kind,
            )
        locked = self._accounts.lock_for_update(
            session,
            [command.sender_account_id, command.receiver_account_id, *extra_lock_ids],
        )
        sender = locked.get(command.sender_account_id)
        receiver = locked.get(command.receiver_account_id)
        if sender is None or receiver is None:
            raise NotFoundError("One of the accounts in this transfer does not exist.")

        # -- 3. Revalidate against freshly locked state --------------------
        # Everything is re-checked here rather than before the lock, because
        # anything read before locking is already stale by definition.
        self._authorize(sender, command)
        sender.ensure_active()
        receiver.ensure_active()
        sender.ensure_same_currency_as(receiver)

        fee = self._fees.fee_for(command.amount)
        total_debit = command.amount + fee

        if command.enforce_limits:
            already_today = self._transfer_reader.sum_sent_since(
                session, sender.id, self._start_of_day(now)
            )
            self._limits.ensure_within_limits(
                amount=total_debit, already_sent_today=already_today
            )

        # Raises InsufficientFundsError. The database CHECK constraint repeats
        # this rule as the final net; both must agree.
        sender.ensure_can_afford(total_debit)

        # -- 4. Apply the movement ----------------------------------------
        updated_sender = sender.debit(total_debit)
        updated_receiver = receiver.credit(command.amount)

        self._account_writer.persist_balance(session, updated_sender)
        self._account_writer.persist_balance(session, updated_receiver)

        # -- 5. Record the transfer ---------------------------------------
        transfer_id = new_ulid()
        reference = generate_reference("TRX", now=now)

        self._transfers.create(
            session,
            transfer_id=transfer_id,
            reference=reference,
            sender_account_id=sender.id,
            receiver_account_id=receiver.id,
            amount=command.amount,
            kind=command.kind,
            status=command.initial_status,
            note=command.note,
            initiated_by_user_id=command.actor_user_id,
            request_id=command.request_id,
            money_request_id=command.money_request_id,
            parent_transfer_id=command.parent_transfer_id,
            intended_receiver_account_id=command.intended_receiver_account_id,
            now=now,
        )

        # -- 6. Post the balanced ledger transaction -----------------------
        # LedgerPosting cannot be constructed unbalanced, so invariant 9 holds
        # by construction rather than by a check we might forget to run.
        posting = LedgerPosting.transfer(
            from_account_id=sender.id,
            to_account_id=receiver.id,
            amount=command.amount,
        )
        self._ledger.post(
            session,
            posting=posting,
            transfer_id=transfer_id,
            posting_type=command.kind,
            description=command.note,
            balances_after={
                updated_sender.id: updated_sender.balance,
                updated_receiver.id: updated_receiver.balance,
            },
            now=now,
        )

        # -- 7. Outbox, in the same transaction ----------------------------
        # If this commits, the event exists. If it rolls back, so does the
        # event. There is no window where money moved but nobody was told.
        self._events.append(
            session,
            TransferSucceeded(
                aggregate_id=transfer_id,
                occurred_at=now,
                payload={
                    "transferId": transfer_id,
                    "reference": reference,
                    "senderAccountId": sender.id,
                    "receiverAccountId": receiver.id,
                    "senderUserId": sender.user_id,
                    "receiverUserId": receiver.user_id,
                    "amountMinor": command.amount.minor,
                    "feeMinor": fee.minor,
                    "currency": command.amount.currency.code,
                    "kind": command.kind,
                },
            ),
            trace_id=command.request_id,
        )

        self._audit.record(
            session,
            actor_user_id=command.actor_user_id,
            action="TRANSFER_SUCCEEDED",
            resource_type="transfer",
            resource_id=transfer_id,
            request_id=command.request_id,
            metadata={
                "reference": reference,
                "amountMinor": command.amount.minor,
                "kind": command.kind,
            },
            now=now,
        )

        result = TransferResult(
            transfer_id=transfer_id,
            reference=reference,
            status=command.initial_status,
            amount=command.amount,
            fee=fee,
            sender_balance_after=updated_sender.balance,
            receiver_balance_after=updated_receiver.balance,
            completed_at=now,
        )

        # A community-overdraft lien is applied in this same database
        # transaction.  The incoming credit is therefore never observable to
        # the borrower before its repayment slice has been moved back to the
        # sponsor's pool.
        if self._credit_interceptor is not None:
            self._credit_interceptor.after_credit(
                session,
                incoming_transfer_id=transfer_id,
                receiver_account=updated_receiver,
                amount=command.amount,
                transfer_kind=command.kind,
                request_id=command.request_id,
            )

        # -- 8. Store the response for future replays ----------------------
        self._idempotency.complete(
            session,
            actor_id=command.actor_user_id,
            endpoint=command.idempotency_endpoint,
            key=command.idempotency_key,
            resource_id=transfer_id,
            http_status=201,
            response_body=result.to_response(),
            now=now,
        )

        logger.info(
            "transfer_succeeded",
            extra={
                "transfer_id": transfer_id,
                "reference": reference,
                "amount_minor": command.amount.minor,
                "kind": command.kind,
            },
        )
        return result

    # -- helpers ----------------------------------------------------------

    def _handle_idempotency_decision(
        self, decision: IdempotencyDecision
    ) -> TransferResult | None:
        if decision.outcome is IdempotencyOutcome.PROCEED:
            return None

        if decision.outcome is IdempotencyOutcome.PAYLOAD_MISMATCH:
            raise IdempotencyKeyReuseError

        if decision.outcome is IdempotencyOutcome.IN_PROGRESS:
            raise RequestInProgressError

        # REPLAY: return exactly what the original attempt returned.
        body = decision.stored_body or {}
        currency_code = body.get("currency", "BDT")
        from platform_.kernel.money import SUPPORTED_CURRENCIES

        currency = SUPPORTED_CURRENCIES.get(currency_code)
        if currency is None:
            raise NotFoundError("Stored transfer used an unsupported currency.")

        return TransferResult(
            transfer_id=body.get("transferId", decision.resource_id or ""),
            reference=body.get("reference", ""),
            status=body.get("status", "SUCCEEDED"),
            amount=Money(int(body.get("amountMinor", 0)), currency),
            fee=Money(int(body.get("feeMinor", 0)), currency),
            sender_balance_after=Money(int(body.get("senderBalanceMinor", 0)), currency),
            receiver_balance_after=Money(0, currency),
            completed_at=datetime.fromisoformat(body["completedAt"])
            if body.get("completedAt")
            else self._clock.now(),
            replayed=True,
        )

    @staticmethod
    def _authorize(sender: Account, command: TransferCommand) -> None:
        """Who is allowed to move money out of the sending account.

        Ordinarily: only its owner. The exception is a movement out of an
        account nobody owns - a holding account or the issuance account - which
        the system performs itself once some other rule has been satisfied.
        Both halves of the condition matter: the kind must be system-initiated
        AND the payer must genuinely be unowned, so this can never be used to
        move money out of a real person's account.
        """
        if command.kind in SYSTEM_INITIATED_KINDS and sender.user_id is None:
            return
        sender.ensure_owned_by(command.actor_user_id)

    @staticmethod
    def _start_of_day(now: datetime) -> datetime:
        """Daily limit window. UTC midnight - documented, not guessed."""
        return (now - timedelta(days=0)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
