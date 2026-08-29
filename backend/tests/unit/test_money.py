"""Money value object.

The single most important unit in the system: if this is wrong, every balance
is wrong. No database, no framework - pure domain.
"""

from __future__ import annotations

import pytest

from platform_.kernel.money import (
    BDT,
    MAX_TRANSFER_AMOUNT_MINOR,
    Currency,
    InvalidMoneyError,
    Money,
)


class TestParsing:
    @pytest.mark.parametrize(
        ("raw", "expected_minor"),
        [
            ("2500.00", 250_000),
            ("2500.5", 250_050),
            ("2500", 250_000),
            ("0.01", 1),
            ("0.1", 10),
            ("0", 0),
            ("0.00", 0),
            ("999999999999", 99_999_999_999_900),
        ],
    )
    def test_valid_decimal_strings(self, raw: str, expected_minor: int) -> None:
        assert Money.from_decimal_string(raw).minor == expected_minor

    @pytest.mark.parametrize(
        "raw",
        [
            "1e5", "1E5", "-5", "+5", "1.234", "abc", "", " ", " 5", "5 ",
            "1,000", ".5", "5.", "0x10", "1_000", "2500.00.00",
            "NaN", "nan", "Infinity", "inf", "-inf", "None", "null",
        ],
    )
    def test_malformed_strings_are_rejected(self, raw: str) -> None:
        with pytest.raises(InvalidMoneyError):
            Money.from_decimal_string(raw)

    @pytest.mark.parametrize(
        ("raw", "description"),
        [
            ("٥", "Arabic-Indic five"),
            ("٥٠", "Arabic-Indic fifty"),
            ("５", "fullwidth five"),
            ("१", "Devanagari one"),
            ("2500.٥", "ASCII major, Arabic-Indic minor"),
        ],
    )
    def test_non_ascii_digits_are_rejected(self, raw: str, description: str) -> None:
        """Python's ``\\d`` matches Unicode digits and ``int()`` converts them.

        Without an explicit ASCII-only character class, these all parse as
        real numbers, so a caller could submit an amount that looks foreign
        but spends real money. This test exists because that bug was present
        in the first version of the parser.
        """
        with pytest.raises(InvalidMoneyError):
            Money.from_decimal_string(raw)

    @pytest.mark.parametrize("bad", [2500.0, True, False, None, "2500", [], {}])
    def test_non_int_minor_units_are_rejected(self, bad: object) -> None:
        with pytest.raises(InvalidMoneyError):
            Money(bad)  # type: ignore[arg-type]


class TestRendering:
    @pytest.mark.parametrize("minor", [0, 1, 10, 100, 250_000, 250_050, 99_999_999])
    def test_round_trip(self, minor: int) -> None:
        rendered = Money.from_minor(minor).to_decimal_string()
        assert Money.from_decimal_string(rendered).minor == minor

    def test_formatting(self) -> None:
        assert Money.from_minor(250_050).to_decimal_string() == "2500.50"
        assert Money.from_minor(1).to_decimal_string() == "0.01"
        assert str(Money.from_minor(250_050)) == "BDT 2500.50"

    def test_negative_amounts_render_with_sign(self) -> None:
        assert Money.from_minor(-250_050).to_decimal_string() == "-2500.50"


class TestArithmetic:
    def test_addition_and_subtraction(self) -> None:
        assert (Money.from_minor(100) + Money.from_minor(50)).minor == 150
        assert (Money.from_minor(100) - Money.from_minor(150)).minor == -50

    def test_immutability(self) -> None:
        original = Money.from_minor(100)
        _ = original + Money.from_minor(50)
        assert original.minor == 100
        with pytest.raises(AttributeError):
            original.minor = 999  # type: ignore[misc]

    def test_currencies_cannot_be_mixed(self) -> None:
        usd = Currency("USD", 2)
        with pytest.raises(InvalidMoneyError):
            Money.from_minor(100, BDT) + Money.from_minor(100, usd)
        with pytest.raises(InvalidMoneyError):
            _ = Money.from_minor(100, BDT) < Money.from_minor(100, usd)

    def test_comparison(self) -> None:
        assert Money.from_minor(100) > Money.from_minor(50)
        assert Money.from_minor(100) == Money.from_minor(100)
        assert Money.from_minor(50) <= Money.from_minor(50)


class TestPrecisionAtScale:
    """Python's unbounded int is why these hold. In float64 they would not."""

    def test_beyond_the_float64_safe_integer_range(self) -> None:
        # 2**53 is the largest integer a double can represent exactly - the
        # limit a JavaScript or float-based implementation would hit.
        unsafe = 2**53 + 1
        assert Money.from_minor(unsafe).minor == unsafe
        assert Money.from_minor(unsafe) != Money.from_minor(2**53)

    def test_issuance_account_at_ten_million_users(self) -> None:
        """The issuance balance at full scale must be exactly representable.

        10 million users funded with BDT 100,000 each puts the issuance
        account at -10^14 poisha. This is why the transfer cap is a policy
        rather than a constraint inside the value object.
        """
        issuance = -(10_000_000 * 100_000 * 100)
        money = Money.from_minor(issuance)
        assert money.minor == issuance
        assert money.to_decimal_string() == "-1000000000000.00"
        assert abs(issuance) > MAX_TRANSFER_AMOUNT_MINOR

    def test_no_rounding_drift_over_many_operations(self) -> None:
        total = Money.zero()
        for _ in range(10_000):
            total = total + Money.from_decimal_string("0.01")
        assert total.minor == 10_000
        assert total.to_decimal_string() == "100.00"
