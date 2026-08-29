"""PostgreSQL implementations of the financial-core ports.

This is the only layer that knows SQL exists. It contains no business rules:
its job is to load state, lock it correctly, and write back what the domain
decided.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from modules.audit.models import AuditLogRecord
from modules.financial_core.adapters.persistence.models import (
    AccountRecord,
    IdempotencyRecord,
    LedgerEntryRecord,
    LedgerTransactionRecord,
    TransferRecord,
)
from modules.financial_core.application.idempotency import (
    IdempotencyDecision,
    IdempotencyOutcome,
)
from modules.financial_core.domain.account import Account, AccountType
from modules.financial_core.domain.events import DomainEvent
from modules.financial_core.domain.ledger import LedgerPosting
from platform_.kernel.ids import new_ulid
from platform_.kernel.money import SUPPORTED_CURRENCIES, Money
from platform_.messaging.models import OutboxEventRecord


def _to_domain(record: AccountRecord) -> Account:
    currency = SUPPORTED_CURRENCIES[record.currency.strip()]
    return Account(
        id=record.id,
        user_id=record.user_id,
        account_type=record.account_type,
        balance=Money(record.balance_minor, currency),
        status=record.status,
        version=record.version,
    )


class SqlAccountRepository:
    """Reads, locks and writes accounts."""

    def get_by_id(self, session: Session, account_id: str) -> Account | None:
        record = session.get(AccountRecord, account_id)
        return _to_domain(record) if record else None

    def get_by_user_id(self, session: Session, user_id: str) -> Account | None:
        record = session.scalars(
            select(AccountRecord).where(AccountRecord.user_id == user_id)
            .where(AccountRecord.account_type == AccountType.USER)
        ).one_or_none()
        return _to_domain(record) if record else None

    def get_by_phone(self, session: Session, phone: str) -> Account | None:
        from modules.identity.adapters.persistence.models import UserRecord

        record = session.scalars(
            select(AccountRecord)
            .join(UserRecord, UserRecord.id == AccountRecord.user_id)
            .where(UserRecord.phone == phone)
            .where(AccountRecord.account_type == AccountType.USER)
        ).one_or_none()
        return _to_domain(record) if record else None

    def lock_for_update(
        self, session: Session, account_ids: list[str]
    ) -> dict[str, Account]:
        """Lock accounts with ``SELECT ... FOR UPDATE``, ascending id order.

        The ``ORDER BY id`` is the deadlock prevention (spec 11.3). Two
        transfers in opposite directions between the same pair of accounts
        would otherwise grab the two rows in opposite orders and deadlock;
        acquiring them in a single agreed order makes that impossible.

        Sorting in Python as well as SQL is intentional belt-and-braces: it
        documents the contract at the call site and stays correct even if a
        future caller passes ids in a different order.
        """
        ordered = sorted(set(account_ids))
        records = session.scalars(
            select(AccountRecord)
            .where(AccountRecord.id.in_(ordered))
            .order_by(AccountRecord.id)
            .with_for_update()
        ).all()
        return {record.id: _to_domain(record) for record in records}

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
        """Create a user account with a zero balance.

        The balance is hard-coded to zero here rather than accepted as an
        argument. There is deliberately no way to ask this repository to
        create an account that already has money in it - funding must go
        through the ledger (spec 8.3).
        """
        session.execute(
            insert(AccountRecord).values(
                id=account_id,
                user_id=user_id,
                account_type="USER",
                account_number=account_number,
                currency=currency,
                balance_minor=0,
                status="ACTIVE",
                version=0,
                created_at=now,
                updated_at=now,
            )
        )

    def create_subaccount(
        self,
        session: Session,
        *,
        account_id: str,
        user_id: str | None,
        account_type: str,
        account_number: str,
        currency: str,
        now: datetime,
    ) -> None:
        """Create an empty non-primary account; funding still uses the ledger."""
        session.execute(
            insert(AccountRecord).values(
                id=account_id,
                user_id=user_id,
                account_type=account_type,
                account_number=account_number,
                currency=currency,
                balance_minor=0,
                status="ACTIVE",
                version=0,
                created_at=now,
                updated_at=now,
            )
        )

    def set_status(self, session: Session, *, account_id: str, status: str) -> int:
        result = session.execute(
            update(AccountRecord)
            .where(AccountRecord.id == account_id)
            .values(status=status, updated_at=func.now())
        )
        return result.rowcount or 0

    def persist_balance(self, session: Session, account: Account) -> None:
        """Write the decided balance back, bumping the optimistic version."""
        result = session.execute(
            update(AccountRecord)
            .where(AccountRecord.id == account.id)
            .values(balance_minor=account.balance.minor, version=account.version)
        )
        if result.rowcount != 1:
            raise RuntimeError(f"Failed to persist balance for account {account.id}")


class SqlTransferRepository:
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
        status: str = "SUCCEEDED",
        parent_transfer_id: str | None = None,
        intended_receiver_account_id: str | None = None,
    ) -> None:
        session.execute(
            insert(TransferRecord).values(
                id=transfer_id,
                reference=reference,
                sender_account_id=sender_account_id,
                receiver_account_id=receiver_account_id,
                amount_minor=amount.minor,
                currency=amount.currency.code,
                kind=kind,
                status=status,
                note=note,
                initiated_by_user_id=initiated_by_user_id,
                request_id=request_id,
                parent_transfer_id=parent_transfer_id,
                intended_receiver_account_id=intended_receiver_account_id,
                created_at=now,
                # A hold is not complete: it is money in transit. Stamping a
                # completion time here would make an unsettled transfer look
                # finished in every report that reads it.
                completed_at=now if status == "SUCCEEDED" else None,
            )
        )
        # money_request_id is accepted by the port but the link is stored on
        # the money_requests side (its transfer_id column), which is where the
        # UNIQUE constraint enforcing "one transfer per request" lives.
        _ = money_request_id

    def try_transition_status(
        self,
        session: Session,
        *,
        transfer_id: str,
        from_status: str,
        to_status: str,
    ) -> int:
        """Latch a transfer from one status to another. Returns rows changed.

        The ``status = :from_status`` predicate is the whole concurrency
        control for deferred settlement. A user pressing Undo and the timer
        firing are two transactions racing for the same row; exactly one
        changes it, and the other gets 0 back and knows it lost. Without the
        predicate both would proceed and the money would be paid out twice.
        """
        result = session.execute(
            update(TransferRecord)
            .where(
                TransferRecord.id == transfer_id,
                TransferRecord.status == from_status,
            )
            .values(status=to_status, completed_at=func.now())
        )
        return result.rowcount or 0

    def get(self, session: Session, transfer_id: str) -> TransferRecord | None:
        return session.get(TransferRecord, transfer_id)

    def set_intended_receiver(
        self, session: Session, *, transfer_id: str, account_id: str
    ) -> None:
        session.execute(
            update(TransferRecord)
            .where(TransferRecord.id == transfer_id)
            .values(intended_receiver_account_id=account_id)
        )

    def list_pending_undo_for_account(
        self, session: Session, account_id: str
    ) -> list[dict[str, Any]]:
        """Holds still inside their undo window, newest first.

        Scoped to the sender: only the person who sent it may undo it, so
        there is no reason for anyone else's holds to appear.
        """
        records = session.scalars(
            select(TransferRecord)
            .where(
                TransferRecord.sender_account_id == account_id,
                TransferRecord.status == "PENDING_UNDO",
            )
            .order_by(TransferRecord.id.desc())
            .limit(20)
        ).all()
        return [
            {
                "transferId": r.id,
                "reference": r.reference,
                "amountMinor": r.amount_minor,
                "currency": r.currency.strip(),
                "note": r.note,
                "createdAt": r.created_at.isoformat(),
            }
            for r in records
        ]

    def count_sent_since(
        self, session: Session, account_id: str, since: datetime
    ) -> int:
        """How many transfers this account has sent in a window (velocity)."""
        return int(
            session.scalar(
                select(func.count())
                .select_from(TransferRecord)
                .where(
                    TransferRecord.sender_account_id == account_id,
                    TransferRecord.created_at >= since,
                )
            )
            or 0
        )

    def sum_sent_since(
        self, session: Session, account_id: str, since: datetime
    ) -> Money:
        total = session.scalar(
            select(func.coalesce(func.sum(TransferRecord.amount_minor), 0)).where(
                TransferRecord.sender_account_id == account_id,
                TransferRecord.status.in_(("SUCCEEDED", "PENDING_UNDO", "HELD")),
                TransferRecord.created_at >= since,
            )
        )
        return Money(int(total or 0))

    def get_by_reference(
        self, session: Session, reference: str
    ) -> dict[str, Any] | None:
        record = session.scalars(
            select(TransferRecord).where(TransferRecord.reference == reference)
        ).one_or_none()
        if record is None:
            return None
        return {
            "transferId": record.id,
            "reference": record.reference,
            "status": record.status,
            "amountMinor": record.amount_minor,
            "currency": record.currency.strip(),
            "kind": record.kind,
            "note": record.note,
            "senderAccountId": record.sender_account_id,
            "receiverAccountId": record.receiver_account_id,
            "createdAt": record.created_at,
            "completedAt": record.completed_at,
        }


class SqlLedgerRepository:
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
        ledger_transaction_id = new_ulid()
        session.execute(
            insert(LedgerTransactionRecord).values(
                id=ledger_transaction_id,
                transfer_id=transfer_id,
                type=posting_type,
                description=description,
                posted_at=now,
            )
        )
        session.execute(
            insert(LedgerEntryRecord),
            [
                {
                    "id": new_ulid(),
                    "ledger_transaction_id": ledger_transaction_id,
                    "account_id": line.account_id,
                    "amount_minor": line.amount.minor,
                    "currency": line.amount.currency.code,
                    "balance_after_minor": balances_after[line.account_id].minor,
                    "created_at": now,
                }
                for line in posting.lines
            ],
        )
        return ledger_transaction_id


class SqlIdempotencyRepository:
    """Durable idempotency backed by the natural-key primary key (spec 12.2)."""

    def reserve(
        self,
        session: Session,
        *,
        actor_id: str,
        endpoint: str,
        key: str,
        fingerprint: str,
        now: datetime,
    ) -> IdempotencyDecision:
        # INSERT ... ON CONFLICT DO NOTHING is the whole concurrency control.
        # If a competing transaction already inserted this key but has not yet
        # committed, PostgreSQL blocks us here until it resolves - so we never
        # see a half-made decision. If it committed, we insert nothing and fall
        # through to read its row. If it rolled back, we win the insert.
        #
        # The outcome is detected with RETURNING, not rowcount: psycopg3
        # reports rowcount as -1 for this statement, so branching on it would
        # silently treat every first attempt as a duplicate. RETURNING yields
        # exactly one row when the insert happened and none when it conflicted,
        # which is unambiguous.
        inserted = session.execute(
            pg_insert(IdempotencyRecord)
            .values(
                actor_id=actor_id,
                endpoint=endpoint,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                status="PROCESSING",
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["actor_id", "endpoint", "idempotency_key"]
            )
            .returning(IdempotencyRecord.idempotency_key)
        ).first()

        if inserted is not None:
            return IdempotencyDecision(outcome=IdempotencyOutcome.PROCEED)

        # Someone else owns this key. Read their row - expiring the identity
        # map first so we see the committed database state, not a stale copy.
        session.expire_all()

        existing = session.get(IdempotencyRecord, (actor_id, endpoint, key))
        if existing is None:
            # Vanishingly unlikely, but never guess about money.
            return IdempotencyDecision(outcome=IdempotencyOutcome.IN_PROGRESS)

        if existing.request_fingerprint != fingerprint:
            return IdempotencyDecision(outcome=IdempotencyOutcome.PAYLOAD_MISMATCH)

        if existing.status == "COMPLETED":
            return IdempotencyDecision(
                outcome=IdempotencyOutcome.REPLAY,
                stored_status=existing.http_status,
                stored_body=existing.response_body,
                resource_id=existing.resource_id,
            )

        return IdempotencyDecision(outcome=IdempotencyOutcome.IN_PROGRESS)

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
    ) -> None:
        session.execute(
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.actor_id == actor_id,
                IdempotencyRecord.endpoint == endpoint,
                IdempotencyRecord.idempotency_key == key,
            )
            .values(
                status="COMPLETED",
                resource_id=resource_id,
                http_status=http_status,
                response_body=response_body,
                completed_at=now,
            )
        )


class SqlOutboxPublisher:
    """Appends domain events to the outbox inside the caller's transaction."""

    def append(
        self, session: Session, event: DomainEvent, *, trace_id: str | None
    ) -> None:
        session.execute(
            insert(OutboxEventRecord).values(
                id=new_ulid(),
                aggregate_type=type(event).aggregate_type,
                aggregate_id=event.aggregate_id,
                event_type=type(event).event_type,
                payload=event.payload,
                trace_id=trace_id,
                schema_version=type(event).schema_version,
                occurred_at=event.occurred_at,
                attempt_count=0,
                # NULL means "eligible immediately". Seeding this with the
                # application's clock would make a brand-new event ineligible
                # whenever the app server runs even slightly ahead of the
                # database, silently stalling the relay. Only a *failure* sets
                # a concrete retry time, and the database sets it from its own
                # now(), so the comparison is always clock-consistent.
                next_attempt_at=None,
            )
        )


class SqlAuditRepository:
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
        ip_address: str | None = None,
    ) -> None:
        session.execute(
            insert(AuditLogRecord).values(
                id=new_ulid(),
                actor_user_id=actor_user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=request_id,
                ip_address=ip_address,
                # The ORM attribute is event_metadata; the DB column is
                # "metadata". Passing metadata= here would resolve to
                # SQLAlchemy's own MetaData on the declarative class.
                event_metadata=metadata,
                created_at=now,
            )
        )


class SqlStatementRepository:
    """Account statement queries.

    Reads from ``ledger_entries`` rather than ``transfers`` because the ledger
    is the account's actual history: it holds one row per account per movement,
    already carries the signed amount and the running balance, and is indexed
    on ``(account_id, id)`` for exactly this access pattern.
    """

    def list_entries(
        self,
        session: Session,
        *,
        account_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Cursor-paginated statement, newest first.

        Keyset pagination on the ULID, never OFFSET. OFFSET makes PostgreSQL
        walk and discard every skipped row, so deep pages degrade linearly -
        at 10 million users that is the difference between a fast query and a
        timeout (spec 22.1).
        """
        sql = """
            SELECT e.id, e.amount_minor, e.balance_after_minor, e.currency,
                   e.created_at, t.reference, t.kind, t.note, t.status,
                   t.sender_account_id, t.receiver_account_id,
                   sender_user.display_name AS sender_name,
                   receiver_user.display_name AS receiver_name,
                   intended_user.display_name AS intended_receiver_name,
                   parent_sender_user.display_name AS parent_sender_name
            FROM ledger_entries e
            JOIN ledger_transactions lt ON lt.id = e.ledger_transaction_id
            JOIN transfers t ON t.id = lt.transfer_id
            LEFT JOIN accounts sa ON sa.id = t.sender_account_id
            LEFT JOIN users sender_user ON sender_user.id = sa.user_id
            LEFT JOIN accounts ra ON ra.id = t.receiver_account_id
            LEFT JOIN users receiver_user ON receiver_user.id = ra.user_id
            LEFT JOIN accounts ia ON ia.id = t.intended_receiver_account_id
            LEFT JOIN users intended_user ON intended_user.id = ia.user_id
            LEFT JOIN transfers parent_t ON parent_t.id = t.parent_transfer_id
            LEFT JOIN accounts parent_sa ON parent_sa.id = parent_t.sender_account_id
            LEFT JOIN users parent_sender_user ON parent_sender_user.id = parent_sa.user_id
            WHERE e.account_id = :account_id
            {cursor_clause}
            ORDER BY e.id DESC
            LIMIT :limit
        """
        # The cursor predicate is composed rather than passed as a nullable
        # bind. PostgreSQL cannot infer a type for a parameter that appears
        # only as `:cursor IS NULL`, and an OR-ed nullable comparison also
        # discourages the planner from using the (account_id, id) index.
        params: dict[str, Any] = {"account_id": account_id, "limit": limit}
        if cursor:
            cursor_clause = "AND e.id < :cursor"
            params["cursor"] = cursor
        else:
            cursor_clause = ""

        rows = (
            session.execute(text(sql.format(cursor_clause=cursor_clause)), params)
            .mappings()
            .all()
        )

        entries = []
        for row in rows:
            outgoing = row["amount_minor"] < 0
            counterparty = (
                (row["intended_receiver_name"] or row["receiver_name"])
                if outgoing
                else (row["parent_sender_name"] or row["sender_name"])
            )
            entries.append(
                {
                    "entryId": row["id"],
                    "reference": row["reference"],
                    "direction": "DEBIT" if outgoing else "CREDIT",
                    "amountMinor": abs(row["amount_minor"]),
                    "signedAmountMinor": row["amount_minor"],
                    "balanceAfterMinor": row["balance_after_minor"],
                    "currency": row["currency"].strip(),
                    "kind": row["kind"],
                    "note": row["note"],
                    "counterpartyName": counterparty or "System",
                    "createdAt": row["created_at"].isoformat(),
                }
            )
        return entries
