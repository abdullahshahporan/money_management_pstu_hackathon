"""Community Spot-Me pool, automatic shortfall draw, and repayment lien."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from modules.financial_core.application.idempotency import (
    fingerprint_request,
    replay_body_or_raise,
)
from modules.financial_core.application.transfer import (
    TransferCommand,
    TransferKind,
    TransferUseCase,
)
from modules.financial_core.application.undo import UndoableTransferService
from modules.financial_core.domain.account import (
    PENDING_SETTLEMENT_ACCOUNT_ID,
    Account,
    AccountType,
)
from modules.overdraft.adapters.persistence.models import (
    DEFAULT_MAX_DRAW_MINOR,
    LIEN_SWEEP_BASIS_POINTS,
)
from platform_.kernel.errors import (
    ConflictError,
    InsufficientFundsError,
    NotFoundError,
    SelfTransferError,
)
from platform_.kernel.ids import new_ulid
from platform_.kernel.money import Money

__all__ = ["OverdraftService"]

SEND_ENDPOINT = "POST:/api/v1/transfers"


class OverdraftService:
    def __init__(
        self,
        *,
        repository: object,
        accounts: object,
        users: object,
        transfer_use_case: TransferUseCase,
        undoable_transfers: UndoableTransferService,
        idempotency: object,
        audit: object,
        clock: object,
        max_draw_minor: int = DEFAULT_MAX_DRAW_MINOR,
        lien_sweep_basis_points: int = LIEN_SWEEP_BASIS_POINTS,
    ) -> None:
        self._repo = repository
        self._accounts = accounts
        self._users = users
        self._transfers = transfer_use_case
        self._undoable = undoable_transfers
        self._idempotency = idempotency
        self._audit = audit
        self._clock = clock
        self._max_draw_minor = max_draw_minor
        self._lien_sweep_basis_points = lien_sweep_basis_points

    # -- sponsor commands ------------------------------------------------

    def create_pool(
        self,
        session: Session,
        *,
        sponsor_user_id: str,
        amount: Money,
        idempotency_key: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        endpoint = "POST:/api/v1/overdraft/pools"
        now = self._clock.now()
        replay = self._reserve(
            session,
            actor_id=sponsor_user_id,
            endpoint=endpoint,
            key=idempotency_key,
            fingerprint=fingerprint_request(
                {"amountMinor": amount.minor, "currency": amount.currency.code}
            ),
            now=now,
        )
        if replay is not None:
            replay["replayed"] = True
            return replay
        if self._repo.get_pool_for_sponsor(session, sponsor_user_id) is not None:
            raise ConflictError("You already have a Spot-Me pool.")
        sponsor = self._accounts.get_by_user_id(session, sponsor_user_id)
        if sponsor is None:
            raise NotFoundError("Your account could not be found.")

        pool_id = new_ulid()
        pool_account_id = new_ulid()
        self._accounts.create_subaccount(
            session,
            account_id=pool_account_id,
            user_id=sponsor_user_id,
            account_type=AccountType.OVERDRAFT_POOL,
            account_number=f"POOL-{pool_id[-12:]}",
            currency=amount.currency.code,
            now=now,
        )
        self._repo.create_pool(
            session,
            pool_id=pool_id,
            sponsor_user_id=sponsor_user_id,
            pool_account_id=pool_account_id,
            currency=amount.currency.code,
            now=now,
        )
        funding = self._transfers.execute(
            session,
            TransferCommand(
                actor_user_id=sponsor_user_id,
                sender_account_id=sponsor.id,
                receiver_account_id=pool_account_id,
                amount=amount,
                idempotency_key=f"pool-fund:{pool_id}",
                request_fingerprint=fingerprint_request(
                    {"poolId": pool_id, "amountMinor": amount.minor}
                ),
                kind=TransferKind.OVERDRAFT_FUND,
                note="Fund Spot-Me pool",
                request_id=request_id,
                idempotency_endpoint="INTERNAL:/overdraft/fund",
            ),
        )
        body = {
            "poolId": pool_id,
            "poolAccountId": pool_account_id,
            "status": "ACTIVE",
            "balanceMinor": amount.minor,
            "currency": amount.currency.code,
            "fundingTransferId": funding.transfer_id,
        }
        self._complete(
            session,
            actor_id=sponsor_user_id,
            endpoint=endpoint,
            key=idempotency_key,
            resource_id=pool_id,
            body=body,
            now=now,
            status=201,
        )
        self._audit_event(
            session,
            actor=sponsor_user_id,
            action="OVERDRAFT_POOL_CREATED",
            resource_id=pool_id,
            request_id=request_id,
            metadata={"amountMinor": amount.minor},
        )
        return body

    def fund_pool(
        self,
        session: Session,
        *,
        sponsor_user_id: str,
        amount: Money,
        idempotency_key: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        pool = self._repo.get_pool_for_sponsor(session, sponsor_user_id)
        if pool is None or pool.status != "ACTIVE":
            raise NotFoundError("You do not have an active Spot-Me pool.")
        sponsor = self._accounts.get_by_user_id(session, sponsor_user_id)
        if sponsor is None:
            raise NotFoundError("Your account could not be found.")
        transfer = self._transfers.execute(
            session,
            TransferCommand(
                actor_user_id=sponsor_user_id,
                sender_account_id=sponsor.id,
                receiver_account_id=pool.pool_account_id,
                amount=amount,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint_request(
                    {"poolId": pool.id, "amountMinor": amount.minor}
                ),
                kind=TransferKind.OVERDRAFT_FUND,
                note="Top up Spot-Me pool",
                request_id=request_id,
                idempotency_endpoint=f"POST:/api/v1/overdraft/pools/{pool.id}/fund",
            ),
        )
        account = self._accounts.get_by_id(session, pool.pool_account_id)
        return {
            "poolId": pool.id,
            "status": pool.status,
            "balanceMinor": account.balance.minor if account else 0,
            "currency": amount.currency.code,
            "fundingTransferId": transfer.transfer_id,
            "replayed": transfer.replayed,
        }

    def grant_access(
        self,
        session: Session,
        *,
        sponsor_user_id: str,
        beneficiary_phone: str,
        max_draw_minor: int,
        idempotency_key: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        endpoint = "POST:/api/v1/overdraft/grants"
        now = self._clock.now()
        replay = self._reserve(
            session,
            actor_id=sponsor_user_id,
            endpoint=endpoint,
            key=idempotency_key,
            fingerprint=fingerprint_request(
                {"beneficiaryPhone": beneficiary_phone, "maxDrawMinor": max_draw_minor}
            ),
            now=now,
        )
        if replay is not None:
            replay["replayed"] = True
            return replay
        pool = self._repo.get_pool_for_sponsor(session, sponsor_user_id)
        beneficiary = self._users.get_by_phone(session, beneficiary_phone)
        if pool is None or pool.status != "ACTIVE":
            raise NotFoundError("Create an active Spot-Me pool first.")
        if beneficiary is None or beneficiary.status != "ACTIVE":
            raise NotFoundError("Beneficiary not found.")
        if beneficiary.id == sponsor_user_id:
            raise SelfTransferError("You cannot grant your pool to yourself.")
        if max_draw_minor > self._max_draw_minor:
            raise ConflictError(
                f"A Spot-Me draw cannot exceed {self._max_draw_minor} minor units."
            )
        if self._repo.get_grant(
            session, pool_id=pool.id, beneficiary_user_id=beneficiary.id
        ) is not None:
            raise ConflictError("This person already has access to your pool.")

        grant_id = new_ulid()
        self._repo.create_grant(
            session,
            grant_id=grant_id,
            pool_id=pool.id,
            beneficiary_user_id=beneficiary.id,
            max_draw_minor=max_draw_minor,
            now=now,
        )
        body = {
            "grantId": grant_id,
            "poolId": pool.id,
            "beneficiaryName": beneficiary.display_name,
            "beneficiaryPhone": beneficiary.phone,
            "maxDrawMinor": max_draw_minor,
            "status": "ACTIVE",
        }
        self._complete(
            session,
            actor_id=sponsor_user_id,
            endpoint=endpoint,
            key=idempotency_key,
            resource_id=grant_id,
            body=body,
            now=now,
            status=201,
        )
        self._audit_event(
            session,
            actor=sponsor_user_id,
            action="OVERDRAFT_ACCESS_GRANTED",
            resource_id=grant_id,
            request_id=request_id,
            metadata={
                "beneficiaryUserId": beneficiary.id,
                "maxDrawMinor": max_draw_minor,
            },
        )
        return body

    def summary(self, session: Session, *, user_id: str) -> dict[str, Any]:
        return self._repo.summary(session, user_id)

    # -- send with automatic shortfall ----------------------------------

    def send(
        self,
        session: Session,
        *,
        actor_user_id: str,
        sender_account_id: str,
        receiver_account_id: str,
        amount: Money,
        idempotency_key: str,
        note: str | None,
        request_id: str | None,
    ) -> dict[str, Any]:
        now = self._clock.now()
        fingerprint = fingerprint_request(
            {
                "senderAccountId": sender_account_id,
                "receiverAccountId": receiver_account_id,
                "amountMinor": amount.minor,
                "currency": amount.currency.code,
                "note": note,
            }
        )
        replay = self._reserve(
            session,
            actor_id=actor_user_id,
            endpoint=SEND_ENDPOINT,
            key=idempotency_key,
            fingerprint=fingerprint,
            now=now,
        )
        if replay is not None:
            replay["replayed"] = True
            return replay

        grants = self._repo.active_grants_for_borrower(session, actor_user_id)
        pool_ids = [str(grant["pool_account_id"]) for grant in grants]
        locked = self._accounts.lock_for_update(
            session, [sender_account_id, PENDING_SETTLEMENT_ACCOUNT_ID, *pool_ids]
        )
        sender = locked.get(sender_account_id)
        if sender is None:
            raise NotFoundError("Your account could not be found.")
        sender.ensure_owned_by(actor_user_id)
        shortfall = max(0, amount.minor - sender.balance.minor)

        draw_body: dict[str, Any] | None = None
        if shortfall:
            derived_key = fingerprint_request({"clientKey": idempotency_key})
            selected = next(
                (
                    grant
                    for grant in grants
                    if shortfall <= int(grant["max_draw_minor"])
                    and shortfall <= locked[str(grant["pool_account_id"])].balance.minor
                ),
                None,
            )
            if selected is None or shortfall > self._max_draw_minor:
                raise InsufficientFundsError(
                    details={
                        "availableMinor": sender.balance.minor,
                        "requestedMinor": amount.minor,
                        "shortfallMinor": shortfall,
                        "spotMeAvailable": False,
                    }
                )
            draw_amount = Money.from_minor(shortfall, amount.currency)
            draw = self._transfers.execute(
                session,
                TransferCommand(
                    actor_user_id=str(selected["sponsor_user_id"]),
                    sender_account_id=str(selected["pool_account_id"]),
                    receiver_account_id=sender_account_id,
                    amount=draw_amount,
                    idempotency_key=f"spot-draw:{derived_key}",
                    request_fingerprint=fingerprint_request(
                        {
                            "poolId": selected["pool_id"],
                            "borrowerUserId": actor_user_id,
                            "amountMinor": shortfall,
                        }
                    ),
                    kind=TransferKind.OVERDRAFT_DRAW,
                    note="Automatic Spot-Me shortfall",
                    request_id=request_id,
                    enforce_limits=False,
                    idempotency_endpoint="INTERNAL:/overdraft/draw",
                ),
            )
            loan_id = self._repo.create_loan(
                session,
                pool_id=str(selected["pool_id"]),
                borrower_user_id=actor_user_id,
                draw_transfer_id=draw.transfer_id,
                amount_minor=shortfall,
                currency=amount.currency.code,
                now=now,
            )
            draw_body = {
                "loanId": loan_id,
                "poolId": selected["pool_id"],
                "drawTransferId": draw.transfer_id,
                "amountMinor": shortfall,
            }

        hold = self._undoable.send(
            session,
            actor_user_id=actor_user_id,
            sender_account_id=sender_account_id,
            receiver_account_id=receiver_account_id,
            amount=amount,
            idempotency_key=(
                f"undo-hold:{fingerprint_request({'clientKey': idempotency_key})}"
            ),
            note=note,
            request_id=request_id,
            idempotency_endpoint="INTERNAL:/transfers/undo-hold",
        )
        body = {
            **hold.to_response(),
            "undoExpiresAt": self._undoable.undo_deadline(hold),
            "overdraftUsed": draw_body is not None,
            "overdraft": draw_body,
            "replayed": False,
        }
        self._complete(
            session,
            actor_id=actor_user_id,
            endpoint=SEND_ENDPOINT,
            key=idempotency_key,
            resource_id=hold.transfer_id,
            body=body,
            now=now,
            status=201,
        )
        return body

    # -- TransferUseCase credit interceptor -----------------------------

    def additional_lock_ids(
        self,
        session: Session,
        *,
        receiver_account_id: str,
        transfer_kind: str,
    ) -> list[str]:
        if transfer_kind in (TransferKind.OVERDRAFT_DRAW, TransferKind.OVERDRAFT_REPAY):
            return []
        receiver = self._accounts.get_by_id(session, receiver_account_id)
        if receiver is None or receiver.account_type != AccountType.USER or not receiver.user_id:
            return []
        return [
            str(loan["pool_account_id"])
            for loan in self._repo.outstanding_loans(session, receiver.user_id)
        ]

    def after_credit(
        self,
        session: Session,
        *,
        incoming_transfer_id: str,
        receiver_account: Account,
        amount: Money,
        transfer_kind: str,
        request_id: str | None,
    ) -> None:
        if transfer_kind in (TransferKind.OVERDRAFT_DRAW, TransferKind.OVERDRAFT_REPAY):
            return
        if receiver_account.account_type != AccountType.USER or not receiver_account.user_id:
            return
        budget = amount.minor * self._lien_sweep_basis_points // 10_000
        if budget <= 0:
            return
        loans = self._repo.outstanding_loans(
            session, receiver_account.user_id, lock=True
        )
        for loan in loans:
            if budget <= 0:
                break
            repay_minor = min(budget, int(loan["outstanding_minor"]))
            repayment = self._transfers.execute(
                session,
                TransferCommand(
                    actor_user_id=receiver_account.user_id,
                    sender_account_id=receiver_account.id,
                    receiver_account_id=str(loan["pool_account_id"]),
                    amount=Money.from_minor(repay_minor, amount.currency),
                    idempotency_key=f"lien:{incoming_transfer_id}:{loan['id']}",
                    request_fingerprint=fingerprint_request(
                        {
                            "incomingTransferId": incoming_transfer_id,
                            "loanId": loan["id"],
                            "amountMinor": repay_minor,
                        }
                    ),
                    kind=TransferKind.OVERDRAFT_REPAY,
                    note="Automatic Spot-Me repayment",
                    request_id=request_id,
                    enforce_limits=False,
                    idempotency_endpoint="INTERNAL:/overdraft/lien-repayment",
                ),
            )
            self._repo.apply_repayment(
                session,
                loan_id=str(loan["id"]),
                transfer_id=repayment.transfer_id,
                amount_minor=repay_minor,
                triggered_by_transfer_id=incoming_transfer_id,
                now=self._clock.now(),
            )
            budget -= repay_minor

    # -- shared idempotency/audit helpers -------------------------------

    def _reserve(self, session: Session, **values: Any) -> dict[str, Any] | None:
        return replay_body_or_raise(self._idempotency.reserve(session, **values))

    def _complete(
        self,
        session: Session,
        *,
        actor_id: str,
        endpoint: str,
        key: str,
        resource_id: str,
        body: dict[str, Any],
        now: Any,
        status: int,
    ) -> None:
        stored = {k: v for k, v in body.items() if k != "replayed"}
        self._idempotency.complete(
            session,
            actor_id=actor_id,
            endpoint=endpoint,
            key=key,
            resource_id=resource_id,
            http_status=status,
            response_body=stored,
            now=now,
        )

    def _audit_event(
        self,
        session: Session,
        *,
        actor: str,
        action: str,
        resource_id: str,
        request_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        self._audit.record(
            session,
            actor_user_id=actor,
            action=action,
            resource_type="overdraft",
            resource_id=resource_id,
            request_id=request_id,
            metadata=metadata,
            now=self._clock.now(),
        )
