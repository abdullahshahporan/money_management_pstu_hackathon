"""Engine and session factory.

Spec 17.5: an unbounded pool multiplied by autoscaled replicas is how an API
tier takes down its own database. The pool here is deliberately small and
explicit; total connections are ``replicas x (pool_size + max_overflow)`` and
must stay well inside the server's ``max_connections``. In production PgBouncer
sits in front of this.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from platform_.config import Settings


def build_engine(settings: Settings) -> Engine:
    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        # Recycle below any proxy/server idle timeout so we never hand out a
        # connection the server has already closed.
        pool_recycle=1800,
        pool_pre_ping=True,
        echo=False,
        # psycopg3 server-side parameters applied to every new connection.
        connect_args={
            "application_name": f"money-movement/{settings.instance_id}",
            "options": (
                f"-c statement_timeout={settings.statement_timeout_ms} "
                f"-c idle_in_transaction_session_timeout="
                f"{settings.idle_in_transaction_timeout_ms} "
                f"-c lock_timeout={settings.lock_timeout_ms}"
            ),
        },
    )

    @event.listens_for(engine, "connect")
    def _set_session_defaults(dbapi_connection, connection_record) -> None:  # noqa: ANN001, ARG001
        # Spec 9.5: READ COMMITTED plus explicit FOR UPDATE row locks is the
        # chosen isolation strategy. Set it explicitly rather than relying on
        # the server default, so the guarantee is visible in the code.
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET SESSION default_transaction_isolation = 'read committed'")

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a session, committing on success and rolling back on any error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
