"""Opening a funded account.

Spec 8.3: the BDT 100,000 opening balance is *not* an UPDATE to a balance
field. It is a real, balanced ledger transaction that debits the system
issuance account and credits the new user. Two consequences follow, and both
matter:

1.  Every taka in the system can be traced to an issuance event, so
    "no money was created" is a SUM over the ledger rather than an assertion.
2.  A new account is indistinguishable, structurally, from an account that
    earned its balance - there is no privileged back door that writes
    balances without double entry.

The grant runs through ``TransferUseCase`` like every other movement, so it
inherits the same atomicity, the same ledger posting and the same idempotency.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from modules.financial_core.application.idempotency import fingerprint_request
from modules.financial_core.application.ports import AccountCreator
from modules.financial_core.application.transfer import (
    TransferCommand,
    TransferKind,
    TransferUseCase,
)
from modules.financial_core.domain.account import SYSTEM_ISSUANCE_ACCOUNT_ID
from platform_.kernel.ids import new_ulid
from platform_.kernel.money import BDT, Money

__all__ = ["OpenAccountUseCase"]


class OpenAccountUseCase:
    def __init__(
        self, transfer_use_case: TransferUseCase, account_creator: AccountCreator
    ) -> None:
        self._transfers = transfer_use_case
        self._accounts = account_creator

    def execute(
        self,
        session: Session,
        *,
        user_id: str,
        opening_balance: Money,
        account_number: str,
        now: datetime,
        request_id: str | None = None,
    ) -> str:
        """Create the account and fund it. Returns the new account id."""
        account_id = new_ulid()

        self._accounts.create(
            session,
            account_id=account_id,
            user_id=user_id,
            account_number=account_number,
            currency=opening_balance.currency.code,
            now=now,
        )
        # The account must be visible to the FOR UPDATE below, which runs in
        # this same transaction but issues its own SELECT.
        session.flush()

        if opening_balance.is_positive:
            # A deterministic key derived from the user id. If registration is
            # retried after an ambiguous failure, the grant cannot be paid
            # twice - the idempotency record already exists.
            grant_key = f"signup-grant:{user_id}"
            self._transfers.execute(
                session,
                TransferCommand(
                    actor_user_id=user_id,
                    sender_account_id=SYSTEM_ISSUANCE_ACCOUNT_ID,
                    receiver_account_id=account_id,
                    amount=opening_balance,
                    idempotency_key=grant_key,
                    request_fingerprint=fingerprint_request(
                        {"userId": user_id, "amountMinor": opening_balance.minor}
                    ),
                    kind=TransferKind.SIGNUP_GRANT,
                    note="Welcome bonus",
                    request_id=request_id,
                    # The issuance account is exempt from limits by design; it
                    # is the source of all money, not a spender.
                    enforce_limits=False,
                ),
            )

        return account_id

    @staticmethod
    def default_currency() -> Money:
        return Money.zero(BDT)
