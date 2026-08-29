"""Transfer policies: fees and limits.

Spec 18 asks for Strategy here, and this is a case where it earns its place:
fee and limit rules are exactly the part of a payments system that changes for
business reasons, on a different schedule from the transfer mechanics. Adding
``CorporateFeeStrategy`` must not require reopening the code that moves money.

What is deliberately *not* a strategy: the balance check, the zero-sum rule,
and the lock ordering. Those are invariants, not policy, and making them
pluggable would only create a way to plug in a wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from platform_.kernel.errors import DailyLimitExceededError, TransferLimitExceededError
from platform_.kernel.money import Money

__all__ = [
    "DefaultLimitPolicy",
    "FeeStrategy",
    "FlatFeeStrategy",
    "LimitPolicy",
    "NoFeeStrategy",
    "PercentageFeeStrategy",
    "TransferLimits",
]


class FeeStrategy(Protocol):
    """Computes the fee charged to the sender for a transfer."""

    def fee_for(self, amount: Money) -> Money: ...


class NoFeeStrategy:
    """The closed ecosystem charges nothing. The seam exists for later."""

    def fee_for(self, amount: Money) -> Money:
        return Money.zero(amount.currency)


@dataclass(frozen=True, slots=True)
class FlatFeeStrategy:
    flat_minor: int

    def fee_for(self, amount: Money) -> Money:
        return Money(self.flat_minor, amount.currency)


@dataclass(frozen=True, slots=True)
class PercentageFeeStrategy:
    """A fee of ``basis_points`` per ten-thousand, rounded up to the minor unit.

    Rounding is integer-only and deliberately in the operator's favour, which
    is the conventional choice and, more importantly, is *stated* rather than
    left to whatever the float happened to do.
    """

    basis_points: int
    minimum_minor: int = 0

    def fee_for(self, amount: Money) -> Money:
        # Ceiling division without touching floating point.
        raw = -(-amount.minor * self.basis_points // 10_000)
        return Money(max(raw, self.minimum_minor), amount.currency)


@dataclass(frozen=True, slots=True)
class TransferLimits:
    max_single_transfer_minor: int
    daily_total_minor: int


class LimitPolicy(Protocol):
    def ensure_within_limits(
        self, *, amount: Money, already_sent_today: Money
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DefaultLimitPolicy:
    """Per-transaction and rolling-daily caps (spec 3.3).

    ``already_sent_today`` is computed inside the transfer transaction while
    the sender's row is locked, so two concurrent transfers cannot each see a
    stale total and both slip under the cap.
    """

    limits: TransferLimits

    def ensure_within_limits(self, *, amount: Money, already_sent_today: Money) -> None:
        if amount.minor > self.limits.max_single_transfer_minor:
            raise TransferLimitExceededError(
                details={
                    "requestedMinor": amount.minor,
                    "maxSingleTransferMinor": self.limits.max_single_transfer_minor,
                }
            )

        projected = already_sent_today.minor + amount.minor
        if projected > self.limits.daily_total_minor:
            raise DailyLimitExceededError(
                details={
                    "alreadySentTodayMinor": already_sent_today.minor,
                    "requestedMinor": amount.minor,
                    "dailyLimitMinor": self.limits.daily_total_minor,
                    "remainingMinor": max(
                        0, self.limits.daily_total_minor - already_sent_today.minor
                    ),
                }
            )
