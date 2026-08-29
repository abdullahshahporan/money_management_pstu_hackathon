"""The double-entry posting.

Invariant 9 (spec 8.2): the signed entries of a posted ledger transaction sum
to zero. Rather than asserting that after the fact, ``LedgerPosting`` refuses
to be constructed unless it already holds. An unbalanced posting is not an
error state the system has to detect - it is a value that cannot exist.

This module is pure domain: no ORM, no framework, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from platform_.kernel.money import Currency, Money

__all__ = [
    "LedgerPosting",
    "PostingLine",
    "UnbalancedPostingError",
]


class UnbalancedPostingError(Exception):
    """Raised when a set of ledger lines does not sum to zero."""


@dataclass(frozen=True, slots=True)
class PostingLine:
    """A signed change to one account.

    Negative debits the account, positive credits it. The sign convention is
    the whole reason the zero-sum check is a single addition.
    """

    account_id: str
    amount: Money

    def __post_init__(self) -> None:
        if self.amount.is_zero:
            raise UnbalancedPostingError("A posting line cannot be zero")

    @property
    def is_debit(self) -> bool:
        return self.amount.minor < 0

    @property
    def is_credit(self) -> bool:
        return self.amount.minor > 0


@dataclass(frozen=True, slots=True)
class LedgerPosting:
    """A balanced accounting event: several signed lines that sum to zero."""

    lines: tuple[PostingLine, ...]
    currency: Currency

    def __post_init__(self) -> None:
        if len(self.lines) < 2:
            raise UnbalancedPostingError(
                "A posting needs at least two lines - money always comes from somewhere"
            )

        total = sum(line.amount.minor for line in self.lines)
        if total != 0:
            raise UnbalancedPostingError(
                f"Posting does not balance: lines sum to {total}, expected 0"
            )

        wrong = [ln for ln in self.lines if ln.amount.currency != self.currency]
        if wrong:
            raise UnbalancedPostingError("All posting lines must share one currency")

        # The same account appearing twice in one posting would net out and
        # hide a mistake behind a balanced total.
        account_ids = [line.account_id for line in self.lines]
        if len(set(account_ids)) != len(account_ids):
            raise UnbalancedPostingError("An account may appear at most once per posting")

    @classmethod
    def transfer(cls, *, from_account_id: str, to_account_id: str, amount: Money) -> LedgerPosting:
        """The canonical two-line posting: debit the sender, credit the receiver."""
        if not amount.is_positive:
            raise UnbalancedPostingError("Transfer amount must be positive")
        if from_account_id == to_account_id:
            raise UnbalancedPostingError("Cannot transfer to the same account")
        return cls(
            lines=(
                PostingLine(account_id=from_account_id, amount=-amount),
                PostingLine(account_id=to_account_id, amount=amount),
            ),
            currency=amount.currency,
        )

    @property
    def debits(self) -> tuple[PostingLine, ...]:
        return tuple(line for line in self.lines if line.is_debit)

    @property
    def credits(self) -> tuple[PostingLine, ...]:
        return tuple(line for line in self.lines if line.is_credit)

    @property
    def total_moved(self) -> Money:
        """The gross value moved - the sum of the credit side."""
        return Money(sum(line.amount.minor for line in self.credits), self.currency)
