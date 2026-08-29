"""Single import surface for every mapped table.

Alembic autogenerate and the test harness both need the complete metadata.
Importing the modules for their side effect of registering mappers is the
whole point of this file, hence the explicit re-export list.
"""

from __future__ import annotations

from modules.audit.models import AuditLogRecord
from modules.financial_core.adapters.persistence.models import (
    AccountRecord,
    IdempotencyRecord,
    LedgerEntryRecord,
    LedgerTransactionRecord,
    TransferRecord,
)
from modules.group_wallet.adapters.persistence.models import (
    GroupMemberRecord,
    GroupWalletRecord,
    GroupWithdrawalApprovalRecord,
    GroupWithdrawalRequestRecord,
)
from modules.identity.adapters.persistence.models import SessionRecord, UserRecord
from modules.money_request.adapters.persistence.models import MoneyRequestRecord
from modules.overdraft.adapters.persistence.models import (
    OverdraftGrantRecord,
    OverdraftLoanRecord,
    OverdraftPoolRecord,
    OverdraftRepaymentRecord,
)
from modules.payment_link.adapters.persistence.models import (
    PaymentLinkPaymentRecord,
    PaymentLinkRecord,
)
from modules.safepay.adapters.persistence.models import SafePayEscrowRecord
from platform_.database.base import Base, metadata
from platform_.messaging.models import ConsumerInboxRecord, OutboxEventRecord
from platform_.scheduling.models import ScheduledTaskRecord

__all__ = [
    "AccountRecord",
    "AuditLogRecord",
    "Base",
    "ConsumerInboxRecord",
    "GroupMemberRecord",
    "GroupWalletRecord",
    "GroupWithdrawalApprovalRecord",
    "GroupWithdrawalRequestRecord",
    "IdempotencyRecord",
    "LedgerEntryRecord",
    "LedgerTransactionRecord",
    "MoneyRequestRecord",
    "OutboxEventRecord",
    "OverdraftGrantRecord",
    "OverdraftLoanRecord",
    "OverdraftPoolRecord",
    "OverdraftRepaymentRecord",
    "PaymentLinkPaymentRecord",
    "PaymentLinkRecord",
    "SafePayEscrowRecord",
    "ScheduledTaskRecord",
    "SessionRecord",
    "TransferRecord",
    "UserRecord",
    "metadata",
]
