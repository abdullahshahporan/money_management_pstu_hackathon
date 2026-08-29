"""The Account aggregate.

A domain object, deliberately separate from the ORM row. The use case loads a
locked row, turns it into this, asks it to decide, and writes the result back.
Keeping the decision here means the rules can be unit-tested with no database
at all, and read without SQLAlchemy in the way.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from platform_.kernel.errors import (
    AccountInactiveError,
    AuthorizationError,
    CurrencyMismatchError,
    InsufficientFundsError,
)
from platform_.kernel.money import Money

__all__ = [
    "ESCROW_ACCOUNT_ID",
    "PENDING_SETTLEMENT_ACCOUNT_ID",
    "SYSTEM_ISSUANCE_ACCOUNT_ID",
    "UNOWNED_ACCOUNT_TYPES",
    "USER_OWNED_ACCOUNT_TYPES",
    "Account",
    "AccountStatus",
    "AccountType",
]

# The single issuance account. Spec 8.3: the opening BDT 100,000 is not an
# UPDATE to a balance, it is a balanced ledger transaction sourced from here.
# Its balance is the exact negative of all money in circulation, which is what
# turns "no money was created" into a single SUM over the ledger.
#
# This lives in the domain, not in persistence: which account issues money is
# an accounting rule, not a storage detail.
SYSTEM_ISSUANCE_ACCOUNT_ID = "00000000000000000000000000"


class AccountType:
    """Every kind of account that can hold value.

    Money parked mid-flight is never left in the payer's account with a flag
    saying "reserved" - a flag does not stop a concurrent transfer from
    spending it. It is moved into a real holding account, so the ordinary
    balance check does the enforcing.
    """

    USER = "USER"
    SYSTEM_ISSUANCE = "SYSTEM_ISSUANCE"
    # Holds money during the 10-second undo window.
    PENDING_SETTLEMENT = "PENDING_SETTLEMENT"
    # Holds money for conditional (safe-pay) transfers until delivery is proven.
    ESCROW = "ESCROW"
    # A shared wallet. Owned by a group, so user_id is NULL.
    GROUP = "GROUP"
    # A sponsor's "spot me" liquidity pool. Owned by the sponsoring user.
    OVERDRAFT_POOL = "OVERDRAFT_POOL"


# Accounts belonging to exactly one user.
USER_OWNED_ACCOUNT_TYPES = (AccountType.USER, AccountType.OVERDRAFT_POOL)
# Accounts belonging to no single user.
UNOWNED_ACCOUNT_TYPES = (
    AccountType.SYSTEM_ISSUANCE,
    AccountType.ESCROW,
    AccountType.PENDING_SETTLEMENT,
    AccountType.GROUP,
)

# Fixed ids for the singleton system holding accounts, mirroring the issuance
# account. Traceability does not suffer from pooling: every entry carries its
# transfer_id, so "how much of escrow belongs to transfer X" is always a query.
ESCROW_ACCOUNT_ID = "00000000000000000000000001"
PENDING_SETTLEMENT_ACCOUNT_ID = "00000000000000000000000002"


class AccountStatus:
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class Account:
    """An account and its authoritative balance at the moment it was locked."""

    id: str
    user_id: str | None
    account_type: str
    balance: Money
    status: str
    version: int

    @property
    def is_system(self) -> bool:
        return self.account_type == AccountType.SYSTEM_ISSUANCE

    @property
    def can_transact(self) -> bool:
        return self.status == AccountStatus.ACTIVE

    @property
    def may_go_negative(self) -> bool:
        """Only the issuance account may. Issuing money is what makes it negative.

        Holding accounts (escrow, pending settlement, group, pool) are hard
        floored at zero: they can only ever pay out money that was paid in, so
        a negative balance there would mean value appeared from nowhere.
        """
        return self.account_type == AccountType.SYSTEM_ISSUANCE

    @property
    def is_holding_account(self) -> bool:
        """True if this account parks money mid-flight on someone's behalf."""
        return self.account_type in (
            AccountType.ESCROW,
            AccountType.PENDING_SETTLEMENT,
        )

    # -- guards -------------------------------------------------------------

    def ensure_active(self) -> None:
        if not self.can_transact:
            raise AccountInactiveError(
                f"Account is {self.status.lower()} and cannot transact.",
                details={"accountId": self.id, "status": self.status},
            )

    def ensure_owned_by(self, user_id: str) -> None:
        """Spec 21.2: object-level authorization, checked server-side every time."""
        if self.user_id != user_id:
            raise AuthorizationError("You do not own this account.")

    def ensure_same_currency_as(self, other: Account) -> None:
        if self.balance.currency != other.balance.currency:
            raise CurrencyMismatchError(
                details={
                    "from": self.balance.currency.code,
                    "to": other.balance.currency.code,
                }
            )

    def ensure_can_afford(self, amount: Money) -> None:
        """Invariant 6. The database repeats this check; both must agree."""
        if self.may_go_negative:
            return
        if self.balance < amount:
            raise InsufficientFundsError(
                details={
                    "availableMinor": self.balance.minor,
                    "requestedMinor": amount.minor,
                    "shortfallMinor": amount.minor - self.balance.minor,
                }
            )

    # -- transitions --------------------------------------------------------

    def debit(self, amount: Money) -> Account:
        """Return a new Account with ``amount`` removed. Never mutates."""
        if not amount.is_positive:
            raise ValueError("Debit amount must be positive")
        self.ensure_active()
        self.ensure_can_afford(amount)
        return replace(self, balance=self.balance - amount, version=self.version + 1)

    def credit(self, amount: Money) -> Account:
        """Return a new Account with ``amount`` added. Never mutates."""
        if not amount.is_positive:
            raise ValueError("Credit amount must be positive")
        self.ensure_active()
        return replace(self, balance=self.balance + amount, version=self.version + 1)
