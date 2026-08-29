"""Request and response models.

Spec 21.3 asks for "schema validation and allowlists". Pydantic gives both:
``extra="forbid"`` rejects any field we did not declare, so a client cannot
smuggle an unexpected key past validation and hope something downstream reads
it. Spec 22.1 fixes the wire format for money as integer minor units, so no
decimal string ever reaches the domain from the network.

These models are also the source of the OpenAPI document (spec 22.1), which
means the published contract cannot drift from what the server actually
accepts - they are generated from the same declarations.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Bangladeshi mobile numbers. The same pattern is a CHECK constraint on the
# users table, so the rule is enforced at both ends.
PHONE_PATTERN = r"^01[3-9][0-9]{8}$"

Phone = Annotated[str, Field(pattern=PHONE_PATTERN, examples=["01712345678"])]
Pin = Annotated[str, Field(pattern=r"^[0-9]{4,6}$", examples=["1234"])]
# Amounts are integer minor units (poisha). Bounded so an absurd value is a
# 400 at the edge rather than a business rejection deeper in.
AmountMinor = Annotated[int, Field(gt=0, le=1_000_000_000_000, examples=[250000])]
Note = Annotated[str, Field(max_length=200)]


class StrictModel(BaseModel):
    """Base for every inbound model.

    ``extra="forbid"`` makes unknown fields an error rather than silently
    ignored input. ``strict=True`` disables Pydantic's lax coercion, so the
    string ``"2500"`` is not quietly accepted where an integer is required -
    for a money field, a client sending the wrong type is a bug worth
    surfacing, not one worth guessing around.
    """

    model_config = ConfigDict(
        extra="forbid", strict=True, str_strip_whitespace=True
    )


# -- auth --------------------------------------------------------------------


class RegisterRequest(StrictModel):
    phone: Phone
    displayName: Annotated[str, Field(min_length=2, max_length=120)]
    password: Annotated[str, Field(min_length=8, max_length=128)]
    pin: Pin


class LoginRequest(StrictModel):
    phone: Phone
    password: Annotated[str, Field(min_length=1, max_length=128)]


class RefreshRequest(StrictModel):
    refreshToken: Annotated[str, Field(min_length=10, max_length=512)]


class LogoutRequest(StrictModel):
    refreshToken: Annotated[str, Field(min_length=10, max_length=512)]


# -- transfers ---------------------------------------------------------------


class TransferRequest(StrictModel):
    recipientPhone: Phone
    amountMinor: AmountMinor
    pin: Pin
    note: Note | None = None
    currency: Literal["BDT"] = "BDT"


# -- SafePay ---------------------------------------------------------------


class CreateSafePayRequest(StrictModel):
    sellerPhone: Phone
    amountMinor: AmountMinor
    pin: Pin
    description: Note | None = None
    currency: Literal["BDT"] = "BDT"


class ShipSafePayRequest(StrictModel):
    courier: Annotated[str, Field(pattern=r"^[a-z0-9-]{2,32}$")]
    trackingNumber: Annotated[str, Field(min_length=4, max_length=64)]


class ReleaseSafePayRequest(StrictModel):
    deliveryCode: Annotated[str, Field(pattern=r"^[0-9]{6}$")]


class DisputeSafePayRequest(StrictModel):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class CourierDeliveryWebhook(StrictModel):
    eventId: Annotated[str, Field(min_length=8, max_length=128)]
    trackingNumber: Annotated[str, Field(min_length=4, max_length=64)]
    status: Literal["DELIVERED"]
    releaseImmediately: bool = True


class ResolveSafePayDispute(StrictModel):
    decision: Literal["RELEASE", "REFUND"]
    note: Annotated[str, Field(min_length=10, max_length=500)]
    banBuyer: bool = False


# -- community overdraft ---------------------------------------------------


class CreateOverdraftPool(StrictModel):
    amountMinor: AmountMinor
    pin: Pin
    currency: Literal["BDT"] = "BDT"


class FundOverdraftPool(CreateOverdraftPool):
    pass


class CreateOverdraftGrant(StrictModel):
    beneficiaryPhone: Phone
    maxDrawMinor: Annotated[int, Field(gt=0, le=50_000)]
    pin: Pin


# -- money requests ----------------------------------------------------------


class CreateMoneyRequest(StrictModel):
    payerPhone: Phone
    amountMinor: AmountMinor
    note: Note | None = None
    currency: Literal["BDT"] = "BDT"


class AcceptMoneyRequest(StrictModel):
    pin: Pin


# -- responses ---------------------------------------------------------------
# Declared for the OpenAPI document. Handlers return the envelope directly so
# that success and error share one shape.


class AccountSummary(BaseModel):
    accountId: str
    accountNumber: str
    displayName: str
    phone: str
    balanceMinor: int
    currency: str
    status: str


class TransferReceipt(BaseModel):
    transferId: str
    reference: str
    status: str
    amountMinor: int
    feeMinor: int
    currency: str
    senderBalanceMinor: int
    completedAt: str


class RecipientLookup(BaseModel):
    userId: str
    displayName: str
    phone: str
