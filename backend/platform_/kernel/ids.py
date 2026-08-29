"""Identifier generation: ULIDs for entities, human references for receipts.

Spec 22.1 asks for UUID/ULID identifiers. ULID is chosen over UUIDv4 because it
is *lexicographically sortable by creation time*: a ULID primary key clusters
new rows at the right-hand edge of the B-tree rather than scattering random
inserts across it, and cursor pagination over history (spec 22.1) can order by
id alone without a secondary timestamp comparison.

Stdlib only - no external ulid package - so the kernel stays dependency-free.
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import UTC, datetime

__all__ = ["generate_reference", "new_ulid"]

# Crockford base32: excludes I, L, O and U to avoid transcription ambiguity and
# accidental profanity in customer-visible references.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_RANDOM_BITS = 80
_ULID_TIME_BITS = 48


def _encode(value: int, length: int) -> str:
    """Encode ``value`` as ``length`` Crockford base32 characters, zero-padded."""
    chars = []
    for _ in range(length):
        value, remainder = divmod(value, 32)
        chars.append(_CROCKFORD[remainder])
    return "".join(reversed(chars))


def new_ulid(*, timestamp_ms: int | None = None) -> str:
    """Return a 26-character ULID.

    Layout: 48 bits of millisecond timestamp followed by 80 bits of
    cryptographically secure randomness. Two ULIDs generated in the same
    millisecond retain 80 bits of entropy, so collision across concurrent
    workers is not a practical concern.
    """
    ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    if not 0 <= ms < (1 << _ULID_TIME_BITS):
        raise ValueError("timestamp out of ULID range")
    randomness = int.from_bytes(os.urandom(_ULID_RANDOM_BITS // 8), "big")
    return _encode(ms, 10) + _encode(randomness, 16)


def generate_reference(prefix: str, *, now: datetime | None = None) -> str:
    """Return a customer-facing reference such as ``TRX-20260829-K3M9QP4T``.

    The suffix is random rather than a sequence on purpose. A sequential
    public reference leaks total system volume and invites enumeration of
    other users' receipts (spec 21.2, IDOR). Uniqueness is guaranteed by a
    UNIQUE constraint in the database, not by hope: the caller retries on
    conflict.
    """
    moment = now or datetime.now(UTC)
    suffix = "".join(secrets.choice(_CROCKFORD) for _ in range(8))
    return f"{prefix}-{moment:%Y%m%d}-{suffix}"
