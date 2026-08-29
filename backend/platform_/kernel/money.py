"""Money as an exact integer quantity of minor units.

Spec 4.1: *never* use floating point for money. BDT is stored in poisha
(1 BDT = 100 poisha) as a Python ``int``, which is arbitrary-precision, so an
entire class of overflow and rounding bugs cannot occur in the financial core.

This module is deliberately dependency-free: it imports nothing outside the
standard library so the domain layer stays framework-agnostic (spec 7.1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = ["BDT", "MAX_TRANSFER_AMOUNT_MINOR", "Currency", "InvalidMoneyError", "Money"]


class InvalidMoneyError(ValueError):
    """Raised when a value cannot be interpreted as a valid monetary amount."""


@dataclass(frozen=True, slots=True, order=True)
class Currency:
    """An ISO-4217 currency and the number of minor units it subdivides into."""

    code: str
    exponent: int

    def __post_init__(self) -> None:
        if len(self.code) != 3 or not self.code.isalpha() or not self.code.isupper():
            raise InvalidMoneyError(f"Currency code must be 3 uppercase letters, got {self.code!r}")
        if self.exponent < 0:
            raise InvalidMoneyError("Currency exponent cannot be negative")

    @property
    def minor_units_per_major(self) -> int:
        return 10**self.exponent

    def __str__(self) -> str:
        return self.code


BDT: Final = Currency("BDT", 2)
SUPPORTED_CURRENCIES: Final[dict[str, Currency]] = {BDT.code: BDT}

# A deliberately strict grammar. It rejects every input shape that has silently
# corrupted a financial system somewhere: scientific notation, signed values,
# excess precision, whitespace, thousands separators, bare dots, hex, and the
# IEEE specials NaN / Infinity.
#
# The character class is written [0-9] rather than \d on purpose. Python's \d
# is Unicode-aware and matches non-ASCII decimal digits - Arabic-Indic U+0665,
# Devanagari, fullwidth forms - all of which int() then happily converts. That
# would let a caller submit a visually foreign amount that parses to a real
# number. Money accepts ASCII digits only.
_DECIMAL_PATTERN: Final = re.compile(r"^(?P<major>[0-9]{1,12})(?:\.(?P<minor>[0-9]{1,2}))?$")

# Upper bound on a single *transfer* amount. Not a technical limit - Python
# ints are unbounded - but a business one, so an absurd request becomes a
# validation error rather than a successful transfer. 10 billion BDT in poisha.
#
# Deliberately NOT enforced inside Money itself. Money also models balances,
# and the system issuance account legitimately holds a large negative balance:
# at 10 million users funded with BDT 100,000 each it reaches -10^14 poisha.
# Capping the value object would make the closed ecosystem unrepresentable.
# The bound is a transfer policy and is applied where transfers are validated.
MAX_TRANSFER_AMOUNT_MINOR: Final = 1_000_000_000_000


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount of a single currency, held in minor units.

    ``Money`` is immutable and closed under its own arithmetic: operations
    return new instances and refuse to mix currencies. Negative amounts are
    permitted because a ledger entry is a *signed* value; the non-negativity
    rule belongs to accounts, not to money, and is enforced by the
    ``accounts.balance_minor >= 0`` database constraint.
    """

    minor: int
    currency: Currency = BDT

    def __post_init__(self) -> None:
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise InvalidMoneyError(
                f"Amount must be an int of minor units, got {type(self.minor).__name__}"
            )

    @classmethod
    def from_minor(cls, minor: int, currency: Currency = BDT) -> Money:
        """Build from an integer count of minor units - the wire format (spec 22.1)."""
        return cls(minor, currency)

    @classmethod
    def from_decimal_string(cls, raw: str, currency: Currency = BDT) -> Money:
        """Parse a human decimal string such as ``2500.00`` with no float involved.

        Parsing is textual: the fractional part is right-padded to the
        currency exponent and concatenated, so no binary floating-point value
        is ever constructed and no rounding can occur.
        """
        if not isinstance(raw, str):
            raise InvalidMoneyError("Amount must be supplied as a string")
        match = _DECIMAL_PATTERN.match(raw)
        if match is None:
            raise InvalidMoneyError(f"Malformed amount {raw!r}")

        fraction = match.group("minor") or ""
        if len(fraction) > currency.exponent:
            raise InvalidMoneyError(
                f"{currency.code} permits at most {currency.exponent} decimal places"
            )

        major = int(match.group("major"))
        fraction_padded = fraction.ljust(currency.exponent, "0")
        minor = major * currency.minor_units_per_major + int(fraction_padded or "0")
        return cls(minor, currency)

    @classmethod
    def zero(cls, currency: Currency = BDT) -> Money:
        return cls(0, currency)

    @property
    def is_positive(self) -> bool:
        return self.minor > 0

    @property
    def is_zero(self) -> bool:
        return self.minor == 0

    def _assert_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise InvalidMoneyError(
                f"Cannot combine {self.currency.code} with {other.currency.code}"
            )

    def __add__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.minor <= other.minor

    def __gt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.minor > other.minor

    def __ge__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.minor >= other.minor

    def to_decimal_string(self) -> str:
        """Render exactly: ``250050`` -> ``2500.50``. Presentation boundary only."""
        sign = "-" if self.minor < 0 else ""
        magnitude = abs(self.minor)
        unit = self.currency.minor_units_per_major
        if self.currency.exponent == 0:
            return f"{sign}{magnitude}"
        return f"{sign}{magnitude // unit}.{magnitude % unit:0{self.currency.exponent}d}"

    def __str__(self) -> str:
        return f"{self.currency.code} {self.to_decimal_string()}"

    def __repr__(self) -> str:
        return f"Money(minor={self.minor}, currency={self.currency.code!r})"
