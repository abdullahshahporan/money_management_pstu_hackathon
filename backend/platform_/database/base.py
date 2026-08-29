"""Declarative base and shared metadata for every module's tables.

A single ``MetaData`` is intentional: this is a modular monolith over one
PostgreSQL database (spec 6.3). Modules own their *tables*, not their own
database, so foreign keys across module boundaries remain enforceable and the
transfer stays one local ACID transaction. When a module is later extracted
into its own service (spec 6.4) its tables move with it.

The naming convention makes constraint names deterministic, so Alembic
autogenerate produces stable diffs and a failed CHECK reports a name we can
map straight back to the invariant it protects.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata
