"""Shared column definitions.

Centralising these keeps every table honest about the three things that matter
most in a financial schema: identifiers are fixed-width sortable ULIDs, money is
always ``BIGINT`` minor units (spec 4.1), and every timestamp is
``TIMESTAMPTZ`` (spec 8.2 invariant 14).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from sqlalchemy import CHAR, BigInteger, DateTime, String, func
from sqlalchemy.orm import mapped_column

ULID_LENGTH = 26

# Primary/foreign key identifiers. CHAR(26) rather than UUID: a ULID is
# lexicographically sortable by creation time, so it doubles as the pagination
# cursor and keeps B-tree inserts clustered at the right edge.
UlidPk = Annotated[str, mapped_column(CHAR(ULID_LENGTH), primary_key=True)]
UlidFk = Annotated[str, mapped_column(CHAR(ULID_LENGTH))]

# Money. Never NUMERIC, never FLOAT - integer minor units only.
MoneyMinor = Annotated[int, mapped_column(BigInteger)]

# ISO-4217 currency code.
CurrencyCode = Annotated[str, mapped_column(CHAR(3))]

ShortText = Annotated[str, mapped_column(String(120))]
StatusText = Annotated[str, mapped_column(String(30))]


def created_at_column() -> object:
    """A creation timestamp. Deliberately NOT indexed by default.

    Spec 9.3: every index is paid for on each write. No table here is queried
    by ``created_at`` alone - history and audit lookups are always scoped to an
    account or actor first - so the composite indexes declared on each table
    already cover those paths. Adding a standalone index per table would have
    cost eight extra B-trees to serve no query.
    """
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def updated_at_column() -> object:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


TimestampTz = Annotated[datetime, mapped_column(DateTime(timezone=True))]
