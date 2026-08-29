"""Idempotency decision types and request fingerprinting (spec 12).

The contract: one ``Idempotency-Key`` per *user intent*. Retrying that intent -
after a timeout, a dropped connection, or an impatient second tap - must return
the original outcome rather than moving money twice.

The key alone is not enough. A client that reuses a key with a different body
has a bug, and silently replaying the first response would hide it while the
second transfer never happens. So the key is bound to a fingerprint of the
canonical request, and a mismatch is rejected loudly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from platform_.kernel.errors import (
    IdempotencyKeyReuseError,
    RequestInProgressError,
)

__all__ = [
    "IdempotencyDecision",
    "IdempotencyOutcome",
    "fingerprint_request",
    "replay_body_or_raise",
]


class IdempotencyOutcome(StrEnum):
    """What the store decided about this (actor, endpoint, key)."""

    PROCEED = "PROCEED"
    """First use of this key. The caller owns the reservation and must complete it."""

    REPLAY = "REPLAY"
    """Already completed with an identical body. Return the stored response."""

    IN_PROGRESS = "IN_PROGRESS"
    """A concurrent duplicate is still executing. Tell the client to retry."""

    PAYLOAD_MISMATCH = "PAYLOAD_MISMATCH"
    """Key reused with a different body. A client bug - reject it."""


@dataclass(frozen=True, slots=True)
class IdempotencyDecision:
    outcome: IdempotencyOutcome
    stored_status: int | None = None
    stored_body: dict[str, Any] | None = None
    resource_id: str | None = None

    @property
    def should_proceed(self) -> bool:
        return self.outcome is IdempotencyOutcome.PROCEED


def fingerprint_request(payload: dict[str, Any]) -> str:
    """Return a stable SHA-256 over the semantic content of a request.

    Canonicalisation matters: ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` are the
    same intent and must fingerprint identically, or a client that serialises
    its JSON in a different key order would be told its own retry is a
    different request. Sorting keys and using a separator-normalised dump
    makes the hash depend on meaning rather than formatting.
    """
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def replay_body_or_raise(decision: IdempotencyDecision) -> dict[str, Any] | None:
    """Translate a store decision for non-transfer commands.

    TransferUseCase reconstructs a rich ``TransferResult`` from a replay.  The
    undo, SafePay and overdraft commands also need the same guarantees, but
    their responses have different shapes.  They use this small shared helper
    and keep their own response serialisation at the feature boundary.
    """
    if decision.outcome is IdempotencyOutcome.PROCEED:
        return None
    if decision.outcome is IdempotencyOutcome.PAYLOAD_MISMATCH:
        raise IdempotencyKeyReuseError
    if decision.outcome is IdempotencyOutcome.IN_PROGRESS:
        raise RequestInProgressError
    return dict(decision.stored_body or {})
