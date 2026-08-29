"""Conditional Safe-Pay state machine and escrow settlement rules."""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from modules.financial_core.application.holding import HoldingService
from modules.financial_core.application.idempotency import (
    fingerprint_request,
    replay_body_or_raise,
)
from modules.financial_core.application.transfer import TransferKind
from modules.financial_core.domain.account import ESCROW_ACCOUNT_ID
from modules.safepay.adapters.persistence.models import (
    DELIVERY_CODE_MAX_ATTEMPTS,
    SafePayEscrowRecord,
)
from platform_.kernel.errors import NotFoundError, StateConflictError
from platform_.kernel.ids import generate_reference, new_ulid
from platform_.kernel.money import Money

__all__ = ["SafePayService"]

CREATE_ENDPOINT = "POST:/api/v1/safepay"
AUTO_RELEASE_TASK = "ESCROW_AUTO_RELEASE"


class SafePayService:
    def __init__(
        self,
        *,
        escrows: object,
        accounts: object,
        users: object,
        sessions: object,
        holding: HoldingService,
        scheduler: object,
        idempotency: object,
        audit: object,
        passwords: object,
        clock: object,
        delivery_code_secret: str,
        auto_release_hours: int = 72,
        code_lock_seconds: int = 900,
    ) -> None:
        self._escrows = escrows
        self._accounts = accounts
        self._users = users
        self._sessions = sessions
        self._holding = holding
        self._scheduler = scheduler
        self._idempotency = idempotency
        self._audit = audit
        self._passwords = passwords
        self._clock = clock
        self._delivery_code_secret = delivery_code_secret.encode("utf-8")
        self._auto_release_hours = auto_release_hours
        self._code_lock_seconds = code_lock_seconds

    def create(
        self,
        session: Session,
        *,
        buyer_user_id: str,
        buyer_account_id: str,
        seller_user_id: str,
        seller_account_id: str,
        amount: Money,
        description: str | None,
        idempotency_key: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        now = self._clock.now()
        fingerprint = fingerprint_request(
            {
                "sellerUserId": seller_user_id,
                "amountMinor": amount.minor,
                "currency": amount.currency.code,
                "description": description,
            }
        )
        replay = self._reserve(
            session,
            actor_id=buyer_user_id,
            endpoint=CREATE_ENDPOINT,
            key=idempotency_key,
            fingerprint=fingerprint,
            now=now,
        )
        if replay is not None:
            # The OTP itself is not stored in the idempotency table. It is a
            # server-secret PRF of the escrow id, so only this authenticated
            # buyer response can reproduce it after a lost network response.
            replay["deliveryCode"] = self._delivery_code(replay["escrowId"])
            replay["replayed"] = True
            return replay

        escrow_id = new_ulid()
        code = self._delivery_code(escrow_id)
        hold = self._holding.hold(
            session,
            actor_user_id=buyer_user_id,
            payer_account_id=buyer_account_id,
            holding_account_id=ESCROW_ACCOUNT_ID,
            amount=amount,
            kind=TransferKind.ESCROW_HOLD,
            pending_status="HELD",
            idempotency_key=f"safepay-hold:{escrow_id}",
            note=description,
            request_id=request_id,
            intended_receiver_account_id=seller_account_id,
            idempotency_endpoint="INTERNAL:/safepay/hold",
        )
        reference = generate_reference("SAFE", now=now)
        self._escrows.create(
            session,
            escrow_id=escrow_id,
            reference=reference,
            hold_transfer_id=hold.transfer_id,
            buyer_user_id=buyer_user_id,
            seller_user_id=seller_user_id,
            amount_minor=amount.minor,
            currency=amount.currency.code,
            description=description,
            delivery_code_hash=self._passwords.hash(code),
            now=now,
        )
        body = {
            "escrowId": escrow_id,
            "reference": reference,
            "holdTransferId": hold.transfer_id,
            "status": "AWAITING_SHIPMENT",
            "amountMinor": amount.minor,
            "currency": amount.currency.code,
        }
        self._complete(
            session,
            actor_id=buyer_user_id,
            endpoint=CREATE_ENDPOINT,
            key=idempotency_key,
            resource_id=escrow_id,
            body=body,
            now=now,
            status=201,
        )
        self._audit_event(
            session,
            actor=buyer_user_id,
            action="SAFEPAY_CREATED",
            escrow_id=escrow_id,
            request_id=request_id,
            metadata={"amountMinor": amount.minor, "sellerUserId": seller_user_id},
        )
        return {**body, "deliveryCode": code, "replayed": False}

    def list_for_user(self, session: Session, *, user_id: str) -> list[dict[str, Any]]:
        return self._escrows.list_for_user(session, user_id=user_id)

    def detail(
        self, session: Session, *, escrow_id: str, user_id: str
    ) -> dict[str, Any]:
        record = self._require_visible(session, escrow_id, user_id)
        body = self._escrows.as_dict(record)
        body["role"] = "BUYER" if record.buyer_user_id == user_id else "SELLER"
        if record.buyer_user_id == user_id and record.status not in ("RELEASED", "REFUNDED"):
            body["deliveryCode"] = self._delivery_code(record.id)
        return body

    def mark_shipped(
        self,
        session: Session,
        *,
        escrow_id: str,
        seller_user_id: str,
        courier: str,
        tracking_number: str,
        idempotency_key: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        return self._state_command(
            session,
            actor_id=seller_user_id,
            endpoint=f"POST:/api/v1/safepay/{escrow_id}/ship",
            key=idempotency_key,
            payload={"courier": courier, "trackingNumber": tracking_number},
            execute=lambda now: self._mark_shipped_once(
                session,
                escrow_id=escrow_id,
                seller_user_id=seller_user_id,
                courier=courier,
                tracking_number=tracking_number,
                now=now,
                request_id=request_id,
            ),
        )

    def release_with_code(
        self,
        session: Session,
        *,
        escrow_id: str,
        seller_user_id: str,
        delivery_code: str,
        idempotency_key: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        endpoint = f"POST:/api/v1/safepay/{escrow_id}/release-code"
        now = self._clock.now()
        replay = self._reserve(
            session,
            actor_id=seller_user_id,
            endpoint=endpoint,
            key=idempotency_key,
            fingerprint=fingerprint_request({"deliveryCode": delivery_code}),
            now=now,
        )
        if replay is not None:
            replay["replayed"] = True
            return replay

        escrow = self._escrows.lock(session, escrow_id)
        if escrow is None or escrow.seller_user_id != seller_user_id:
            raise NotFoundError("SafePay order not found.")
        if escrow.status not in ("AWAITING_SHIPMENT", "SHIPPED", "DELIVERED"):
            raise StateConflictError(f"SafePay is already {escrow.status.lower()}.")

        if escrow.delivery_code_locked_until and now < escrow.delivery_code_locked_until:
            body = {
                "escrowId": escrow.id,
                "status": escrow.status,
                "errorCode": "DELIVERY_CODE_LOCKED",
                "message": "Too many incorrect delivery-code attempts. Try later.",
            }
            self._complete(
                session,
                actor_id=seller_user_id,
                endpoint=endpoint,
                key=idempotency_key,
                resource_id=escrow.id,
                body=body,
                now=now,
                status=423,
            )
            return body

        if not self._passwords.verify(escrow.delivery_code_hash, delivery_code):
            attempts = escrow.delivery_code_attempts + 1
            locked_until = (
                now + timedelta(seconds=self._code_lock_seconds)
                if attempts >= DELIVERY_CODE_MAX_ATTEMPTS
                else None
            )
            self._escrows.record_code_failure(
                session,
                escrow_id=escrow.id,
                attempts=attempts,
                locked_until=locked_until,
                now=now,
            )
            body = {
                "escrowId": escrow.id,
                "status": escrow.status,
                "errorCode": (
                    "DELIVERY_CODE_LOCKED" if locked_until else "INVALID_DELIVERY_CODE"
                ),
                "message": (
                    "Delivery code locked after too many attempts."
                    if locked_until
                    else "The delivery code is incorrect."
                ),
                "attemptsRemaining": max(0, DELIVERY_CODE_MAX_ATTEMPTS - attempts),
            }
            self._complete(
                session,
                actor_id=seller_user_id,
                endpoint=endpoint,
                key=idempotency_key,
                resource_id=escrow.id,
                body=body,
                now=now,
                status=423 if locked_until else 400,
            )
            return body

        body = self._release_locked(
            session,
            escrow=escrow,
            actor_user_id=seller_user_id,
            reason="Buyer delivery code verified",
            request_id=request_id,
        )
        self._complete(
            session,
            actor_id=seller_user_id,
            endpoint=endpoint,
            key=idempotency_key,
            resource_id=escrow.id,
            body=body,
            now=now,
        )
        return body

    def confirm_received(
        self,
        session: Session,
        *,
        escrow_id: str,
        buyer_user_id: str,
        idempotency_key: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        return self._state_command(
            session,
            actor_id=buyer_user_id,
            endpoint=f"POST:/api/v1/safepay/{escrow_id}/confirm-received",
            key=idempotency_key,
            payload={"escrowId": escrow_id},
            execute=lambda _now: self._buyer_release_once(
                session,
                escrow_id=escrow_id,
                buyer_user_id=buyer_user_id,
                request_id=request_id,
            ),
        )

    def dispute(
        self,
        session: Session,
        *,
        escrow_id: str,
        buyer_user_id: str,
        reason: str,
        idempotency_key: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        return self._state_command(
            session,
            actor_id=buyer_user_id,
            endpoint=f"POST:/api/v1/safepay/{escrow_id}/dispute",
            key=idempotency_key,
            payload={"reason": reason},
            execute=lambda now: self._dispute_once(
                session,
                escrow_id=escrow_id,
                buyer_user_id=buyer_user_id,
                reason=reason,
                now=now,
                request_id=request_id,
            ),
        )

    def courier_delivered(
        self,
        session: Session,
        *,
        courier: str,
        tracking_number: str,
        event_id: str,
        release_immediately: bool,
    ) -> dict[str, Any]:
        escrow = self._escrows.find_by_tracking(
            session, courier=courier, tracking_number=tracking_number
        )
        if escrow is None:
            raise NotFoundError("No SafePay order matches this courier parcel.")
        endpoint = f"POST:/courier/{courier}/delivered"
        now = self._clock.now()
        replay = self._reserve(
            session,
            actor_id=escrow.seller_user_id,
            endpoint=endpoint,
            key=event_id,
            fingerprint=fingerprint_request(
                {
                    "trackingNumber": tracking_number,
                    "releaseImmediately": release_immediately,
                }
            ),
            now=now,
        )
        if replay is not None:
            replay["replayed"] = True
            return replay
        if escrow.status == "SHIPPED":
            release_at = now + timedelta(hours=self._auto_release_hours)
            if self._escrows.mark_delivered(
                session,
                escrow_id=escrow.id,
                delivered_at=now,
                auto_release_at=release_at,
            ) != 1:
                raise StateConflictError("Delivery status changed concurrently.")
            session.flush()
            session.refresh(escrow)
            if release_immediately:
                body = self._release_locked(
                    session,
                    escrow=escrow,
                    actor_user_id=escrow.seller_user_id,
                    reason=f"Verified {courier} delivery event",
                    request_id=None,
                )
            else:
                self._scheduler.schedule(
                    session,
                    task_type=AUTO_RELEASE_TASK,
                    resource_id=escrow.id,
                    delay_seconds=self._auto_release_hours * 3600,
                )
                body = {
                    "escrowId": escrow.id,
                    "status": "DELIVERED",
                    "autoReleaseAt": release_at.isoformat(),
                }
        elif escrow.status in ("RELEASED", "REFUNDED", "DISPUTED"):
            body = {"escrowId": escrow.id, "status": escrow.status}
        else:
            raise StateConflictError(
                f"Courier delivery cannot be applied while {escrow.status.lower()}."
            )
        self._complete(
            session,
            actor_id=escrow.seller_user_id,
            endpoint=endpoint,
            key=event_id,
            resource_id=escrow.id,
            body=body,
            now=now,
        )
        return body

    def auto_release(
        self, session: Session, *, escrow_id: str
    ) -> dict[str, Any] | None:
        escrow = self._escrows.lock(session, escrow_id)
        if escrow is None or escrow.status != "DELIVERED":
            return None
        return self._release_locked(
            session,
            escrow=escrow,
            actor_user_id=escrow.seller_user_id,
            reason="SafePay dispute window elapsed",
            request_id=None,
        )

    def resolve_dispute(
        self,
        session: Session,
        *,
        escrow_id: str,
        decision: str,
        note: str,
        ban_buyer: bool,
        idempotency_key: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        # Engineering/admin commands have no user JWT. The escrow id is a
        # stable 26-char idempotency actor, scoped by its admin endpoint.
        return self._state_command(
            session,
            actor_id=escrow_id,
            endpoint=f"POST:/api/v1/admin/safepay/{escrow_id}/resolve",
            key=idempotency_key,
            payload={"decision": decision, "note": note, "banBuyer": ban_buyer},
            execute=lambda _now: self._resolve_once(
                session,
                escrow_id=escrow_id,
                decision=decision,
                note=note,
                ban_buyer=ban_buyer,
                request_id=request_id,
            ),
        )

    def list_disputes(self, session: Session) -> list[dict[str, Any]]:
        """Admin read model for the dispute-resolution panel."""
        return self._escrows.list_disputes(session)

    # -- one-shot state transitions -------------------------------------

    def _mark_shipped_once(self, session: Session, **values: Any) -> dict[str, Any]:
        request_id = values.pop("request_id")
        if self._escrows.mark_shipped(session, **values) != 1:
            raise StateConflictError("Only an awaiting-shipment order can be shipped.")
        self._audit_event(
            session,
            actor=values["seller_user_id"],
            action="SAFEPAY_SHIPPED",
            escrow_id=values["escrow_id"],
            request_id=request_id,
            metadata={
                "courier": values["courier"],
                "trackingNumber": values["tracking_number"],
            },
        )
        return {
            "escrowId": values["escrow_id"],
            "status": "SHIPPED",
            "courier": values["courier"],
            "trackingNumber": values["tracking_number"],
        }

    def _buyer_release_once(
        self,
        session: Session,
        *,
        escrow_id: str,
        buyer_user_id: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        escrow = self._escrows.lock(session, escrow_id)
        if escrow is None or escrow.buyer_user_id != buyer_user_id:
            raise NotFoundError("SafePay order not found.")
        if escrow.status not in ("SHIPPED", "DELIVERED"):
            raise StateConflictError("This order is not awaiting receipt confirmation.")
        return self._release_locked(
            session,
            escrow=escrow,
            actor_user_id=buyer_user_id,
            reason="Buyer confirmed receipt",
            request_id=request_id,
        )

    def _dispute_once(
        self,
        session: Session,
        *,
        escrow_id: str,
        buyer_user_id: str,
        reason: str,
        now: Any,
        request_id: str | None,
    ) -> dict[str, Any]:
        if self._escrows.mark_disputed(
            session,
            escrow_id=escrow_id,
            buyer_user_id=buyer_user_id,
            reason=reason,
            now=now,
        ) != 1:
            raise StateConflictError("This SafePay order cannot enter dispute now.")
        self._scheduler.cancel(
            session, task_type=AUTO_RELEASE_TASK, resource_id=escrow_id
        )
        self._audit_event(
            session,
            actor=buyer_user_id,
            action="SAFEPAY_DISPUTED",
            escrow_id=escrow_id,
            request_id=request_id,
            metadata={"reason": reason},
        )
        return {"escrowId": escrow_id, "status": "DISPUTED"}

    def _resolve_once(
        self,
        session: Session,
        *,
        escrow_id: str,
        decision: str,
        note: str,
        ban_buyer: bool,
        request_id: str | None,
    ) -> dict[str, Any]:
        escrow = self._escrows.lock(session, escrow_id)
        if escrow is None:
            raise NotFoundError("SafePay order not found.")
        if escrow.status != "DISPUTED":
            raise StateConflictError("Only a disputed SafePay order can be resolved.")
        if decision == "RELEASE":
            body = self._release_locked(
                session,
                escrow=escrow,
                actor_user_id=escrow.seller_user_id,
                reason=note,
                request_id=request_id,
                resolution_note=note,
            )
            if ban_buyer:
                self._users.set_status(session, user_id=escrow.buyer_user_id, status="CLOSED")
                account = self._accounts.get_by_user_id(session, escrow.buyer_user_id)
                if account:
                    self._accounts.set_status(session, account_id=account.id, status="CLOSED")
                self._sessions.revoke_all_for_user(
                    session, user_id=escrow.buyer_user_id, now=self._clock.now()
                )
                body["buyerBanned"] = True
            return body
        return self._refund_locked(
            session,
            escrow=escrow,
            reason=note,
            request_id=request_id,
            resolution_note=note,
        )

    def _release_locked(
        self,
        session: Session,
        *,
        escrow: SafePayEscrowRecord,
        actor_user_id: str,
        reason: str,
        request_id: str | None,
        resolution_note: str | None = None,
    ) -> dict[str, Any]:
        hold = self._holding.get_hold(session, escrow.hold_transfer_id)
        if hold is None or hold.intended_receiver_account_id is None:
            raise StateConflictError("SafePay hold is incomplete.")
        settlement = self._holding.settle(
            session,
            hold_transfer_id=hold.id,
            hold_reference=hold.reference,
            from_status="HELD",
            to_status="SUCCEEDED",
            beneficiary_account_id=hold.intended_receiver_account_id,
            holding_account_id=ESCROW_ACCOUNT_ID,
            amount=Money.from_minor(escrow.amount_minor),
            kind=TransferKind.ESCROW_RELEASE,
            actor_user_id=actor_user_id,
            note=reason,
            request_id=request_id,
        )
        if self._escrows.mark_settled(
            session,
            escrow_id=escrow.id,
            from_statuses=("AWAITING_SHIPMENT", "SHIPPED", "DELIVERED", "DISPUTED"),
            status="RELEASED",
            settlement_transfer_id=settlement.transfer_id,
            now=self._clock.now(),
            resolution_note=resolution_note,
        ) != 1:
            raise StateConflictError("SafePay was resolved concurrently.")
        self._scheduler.cancel(
            session, task_type=AUTO_RELEASE_TASK, resource_id=escrow.id
        )
        self._audit_event(
            session,
            actor=actor_user_id,
            action="SAFEPAY_RELEASED",
            escrow_id=escrow.id,
            request_id=request_id,
            metadata={"settlementTransferId": settlement.transfer_id, "reason": reason},
        )
        return {
            "escrowId": escrow.id,
            "reference": escrow.reference,
            "status": "RELEASED",
            "settlementTransferId": settlement.transfer_id,
            "settlementReference": settlement.reference,
        }

    def _refund_locked(
        self,
        session: Session,
        *,
        escrow: SafePayEscrowRecord,
        reason: str,
        request_id: str | None,
        resolution_note: str,
    ) -> dict[str, Any]:
        hold = self._holding.get_hold(session, escrow.hold_transfer_id)
        if hold is None:
            raise StateConflictError("SafePay hold is incomplete.")
        settlement = self._holding.settle(
            session,
            hold_transfer_id=hold.id,
            hold_reference=hold.reference,
            from_status="HELD",
            to_status="REFUNDED",
            beneficiary_account_id=hold.sender_account_id,
            holding_account_id=ESCROW_ACCOUNT_ID,
            amount=Money.from_minor(escrow.amount_minor),
            kind=TransferKind.ESCROW_REFUND,
            actor_user_id=escrow.buyer_user_id,
            note=reason,
            request_id=request_id,
        )
        if self._escrows.mark_settled(
            session,
            escrow_id=escrow.id,
            from_statuses=("DISPUTED",),
            status="REFUNDED",
            settlement_transfer_id=settlement.transfer_id,
            now=self._clock.now(),
            resolution_note=resolution_note,
        ) != 1:
            raise StateConflictError("SafePay was resolved concurrently.")
        return {
            "escrowId": escrow.id,
            "reference": escrow.reference,
            "status": "REFUNDED",
            "settlementTransferId": settlement.transfer_id,
            "settlementReference": settlement.reference,
        }

    # -- shared helpers --------------------------------------------------

    def _state_command(
        self,
        session: Session,
        *,
        actor_id: str,
        endpoint: str,
        key: str,
        payload: dict[str, Any],
        execute: Any,
    ) -> dict[str, Any]:
        now = self._clock.now()
        replay = self._reserve(
            session,
            actor_id=actor_id,
            endpoint=endpoint,
            key=key,
            fingerprint=fingerprint_request(payload),
            now=now,
        )
        if replay is not None:
            replay["replayed"] = True
            return replay
        body = execute(now)
        self._complete(
            session,
            actor_id=actor_id,
            endpoint=endpoint,
            key=key,
            resource_id=body["escrowId"],
            body=body,
            now=now,
        )
        return body

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
        status: int = 200,
    ) -> None:
        stored = {k: v for k, v in body.items() if k not in ("deliveryCode", "replayed")}
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

    def _delivery_code(self, escrow_id: str) -> str:
        digest = hmac.new(
            self._delivery_code_secret,
            f"safepay-delivery:{escrow_id}".encode(),
            hashlib.sha256,
        ).digest()
        return f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"

    def _require_visible(
        self, session: Session, escrow_id: str, user_id: str
    ) -> SafePayEscrowRecord:
        escrow = self._escrows.get(session, escrow_id)
        if escrow is None or user_id not in (escrow.buyer_user_id, escrow.seller_user_id):
            raise NotFoundError("SafePay order not found.")
        return escrow

    def _audit_event(
        self,
        session: Session,
        *,
        actor: str | None,
        action: str,
        escrow_id: str,
        request_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        self._audit.record(
            session,
            actor_user_id=actor,
            action=action,
            resource_type="safepay_escrow",
            resource_id=escrow_id,
            request_id=request_id,
            metadata=metadata,
            now=self._clock.now(),
        )
