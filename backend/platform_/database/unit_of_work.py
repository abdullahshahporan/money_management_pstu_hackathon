"""Transaction boundary with bounded retry on transient serialization failures.

Spec 7.5: the *application use case* owns the transaction boundary, not the
HTTP handler and not the repository. Every repository call participating in a
transfer shares the one session opened here.

Spec 11.3 / 20.1: PostgreSQL aborts one participant of a deadlock and raises a
serialization failure. Those are transient by definition, so the whole
transaction is retried with exponential backoff plus full jitter. Retrying is
safe precisely because the transaction was atomic: the aborted attempt left
nothing behind. Business rejections such as INSUFFICIENT_FUNDS are never
retried, because retrying cannot create money.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from platform_.kernel.errors import DependencyUnavailableError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# PostgreSQL SQLSTATEs indicating a transient conflict that is worth retrying.
RETRYABLE_SQLSTATES = frozenset(
    {
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
    }
)

# Connection-level failures: the database is unreachable, not merely contended.
UNAVAILABLE_SQLSTATES = frozenset(
    {
        "08000",  # connection_exception
        "08001",  # sqlclient_unable_to_establish_sqlconnection
        "08003",  # connection_does_not_exist
        "08004",  # sqlserver_rejected_establishment_of_sqlconnection
        "08006",  # connection_failure
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
    }
)


def sqlstate_of(error: BaseException) -> str | None:
    """Extract the PostgreSQL SQLSTATE from a SQLAlchemy-wrapped driver error."""
    orig = getattr(error, "orig", None)
    if orig is None:
        return None
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


class UnitOfWork:
    """Opens transactions and retries only the transient ones."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        max_attempts: int = 3,
        base_delay_ms: int = 25,
    ) -> None:
        self._session_factory = session_factory
        self._max_attempts = max_attempts
        self._base_delay_ms = base_delay_ms

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """One transaction, committed on success and rolled back on any error."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def run(self, operation: Callable[[Session], T]) -> T:
        """Execute ``operation`` in one transaction, retrying transient conflicts.

        ``operation`` may be invoked more than once, each time with a fresh
        session and a fresh transaction, so it must not depend on mutations it
        made to its own in-memory arguments during a previous attempt.
        """
        last_error: BaseException | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                with self.transaction() as session:
                    return operation(session)
            except (OperationalError, DBAPIError) as exc:
                state = sqlstate_of(exc)

                if state in UNAVAILABLE_SQLSTATES:
                    # Spec 18.5: do not guess a balance, do not fabricate success.
                    logger.error(
                        "database_unavailable",
                        extra={"sqlstate": state, "attempt": attempt},
                    )
                    raise DependencyUnavailableError from exc

                if state not in RETRYABLE_SQLSTATES:
                    raise

                last_error = exc
                if attempt == self._max_attempts:
                    break

                # Exponential backoff with full jitter, so retrying participants
                # do not collide again in lockstep.
                delay_ms = self._base_delay_ms * (2 ** (attempt - 1))
                time.sleep(random.uniform(0, delay_ms) / 1000)  # noqa: S311
                logger.warning(
                    "transaction_retry",
                    extra={
                        "sqlstate": state,
                        "attempt": attempt,
                        "max_attempts": self._max_attempts,
                    },
                )

        logger.error("transaction_retries_exhausted", extra={"attempts": self._max_attempts})
        raise DependencyUnavailableError(
            "The operation could not be completed due to contention. Please retry."
        ) from last_error
