"""Claiming and completing scheduled work."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, text, update
from sqlalchemy.orm import Session

from platform_.kernel.ids import new_ulid
from platform_.scheduling.models import ScheduledTaskRecord

__all__ = ["SqlScheduledTaskRepository"]

# Claim only rows that are due, and skip rows another worker already holds.
# SKIP LOCKED is what lets several scheduler replicas run without either
# duplicating work or queueing behind each other.
CLAIM_SQL = """
    SELECT id, task_type, resource_id, attempt_count
    FROM scheduled_tasks
    WHERE status = 'PENDING'
      AND due_at <= now()
    ORDER BY due_at
    FOR UPDATE SKIP LOCKED
    LIMIT :batch_size
"""


class SqlScheduledTaskRepository:
    def schedule(
        self,
        session: Session,
        *,
        task_type: str,
        resource_id: str,
        delay_seconds: float,
    ) -> str:
        """Queue work for `delay_seconds` from now, as measured by the database.

        The due time is computed with ``now() + interval`` inside PostgreSQL,
        never from the application clock. An earlier bug in the outbox relay
        seeded a due time from the app server, and whenever that server ran
        even slightly ahead of the database the work became permanently
        ineligible and silently stalled. Both the write and the read of a due
        time therefore happen on the same clock.

        ``ON CONFLICT DO NOTHING`` on (task_type, resource_id) makes scheduling
        idempotent: a retried request cannot queue the same settlement twice.
        """
        task_id = new_ulid()
        session.execute(
            text(
                "INSERT INTO scheduled_tasks "
                "(id, task_type, resource_id, due_at, status, attempt_count) "
                "VALUES (:id, :task_type, :resource_id, "
                "now() + make_interval(secs => :delay), 'PENDING', 0) "
                "ON CONFLICT (task_type, resource_id) DO NOTHING"
            ),
            {
                "id": task_id,
                "task_type": task_type,
                "resource_id": resource_id,
                "delay": float(delay_seconds),
            },
        )
        return task_id

    def claim_due(
        self, session: Session, *, batch_size: int = 50
    ) -> list[dict[str, Any]]:
        rows = session.execute(text(CLAIM_SQL), {"batch_size": batch_size}).mappings().all()
        return [dict(row) for row in rows]

    def complete(self, session: Session, *, task_id: str) -> None:
        session.execute(
            update(ScheduledTaskRecord)
            .where(ScheduledTaskRecord.id == task_id)
            .values(status="DONE", completed_at=func.now())
        )

    def cancel(self, session: Session, *, task_type: str, resource_id: str) -> int:
        """Cancel pending work for a resource. Returns rows affected.

        Used when a user acts before the timer - undoing a transfer, or
        confirming delivery early. Cancelling is best-effort by design: if the
        worker already claimed the row, this changes nothing and the settlement
        proceeds, which is correct because the settlement itself is latched.
        """
        result = session.execute(
            update(ScheduledTaskRecord)
            .where(
                ScheduledTaskRecord.task_type == task_type,
                ScheduledTaskRecord.resource_id == resource_id,
                ScheduledTaskRecord.status == "PENDING",
            )
            .values(status="CANCELLED", completed_at=func.now())
        )
        return result.rowcount or 0

    def record_failure(
        self,
        session: Session,
        *,
        task_id: str,
        attempt: int,
        error: str,
        max_attempts: int,
    ) -> None:
        """Back off, or give up and leave the row for an operator."""
        terminal = attempt >= max_attempts
        delay = min(2**attempt, 300)
        session.execute(
            text(
                "UPDATE scheduled_tasks SET attempt_count = :attempt,"
                " last_error = :error,"
                " status = CASE WHEN :terminal THEN 'FAILED' ELSE 'PENDING' END,"
                " due_at = CASE WHEN :terminal THEN due_at"
                "               ELSE now() + make_interval(secs => :delay) END"
                " WHERE id = :id"
            ),
            {
                "attempt": attempt,
                "error": error[:500],
                "terminal": terminal,
                "delay": float(delay),
                "id": task_id,
            },
        )

    def backlog(self, session: Session) -> dict[str, Any]:
        """Depth and age of pending work, for the health dashboard.

        A scheduler that has silently stopped looks exactly like one with
        nothing to do, unless you measure how old the oldest due item is.
        """
        row = session.execute(
            text(
                "SELECT"
                " count(*) FILTER (WHERE status = 'PENDING') AS pending,"
                " count(*) FILTER (WHERE status = 'PENDING' AND due_at <= now()) AS overdue,"
                " count(*) FILTER (WHERE status = 'FAILED') AS failed,"
                " count(*) FILTER (WHERE status = 'DONE') AS done,"
                " COALESCE(EXTRACT(EPOCH FROM (now() - MIN(due_at) FILTER"
                "   (WHERE status = 'PENDING' AND due_at <= now()))), 0) AS oldest_overdue_seconds"
                " FROM scheduled_tasks"
            )
        ).mappings().one()
        return {
            "pending": int(row["pending"]),
            "overdue": int(row["overdue"]),
            "failed": int(row["failed"]),
            "done": int(row["done"]),
            "oldestOverdueSeconds": float(row["oldest_overdue_seconds"] or 0),
        }
