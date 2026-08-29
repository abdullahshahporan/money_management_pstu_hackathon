"""Deterministic demo seed (spec 29.2).

Creates a known set of users with known balances so the demo can be repeated
identically, and so a judge can be handed working credentials rather than
watching someone type a registration form.

Everything goes through the real use cases - registration, the ledger-backed
opening grant, real transfers - so the seeded state is indistinguishable from
state produced by ordinary use. A seed script that wrote balances directly
would quietly break the very reconciliation it is meant to demonstrate.
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from apps.api.container import build_container
from modules.financial_core.application.idempotency import fingerprint_request
from modules.financial_core.application.transfer import TransferCommand, TransferKind
from platform_.config import get_settings
from platform_.kernel.ids import new_ulid
from platform_.kernel.money import Money
from platform_.observability.logging import configure_logging

DEMO_PASSWORD = "demo-password-2026"  # noqa: S105 - demo credentials, printed below
DEMO_PIN = "1234"

DEMO_USERS = [
    ("01711111111", "Alice Rahman"),
    ("01722222222", "Bob Hasan"),
    ("01733333333", "Chowdhury Karim"),
    ("01744444444", "Dina Akter"),
]

# A couple of movements so history and reconciliation have something to show.
DEMO_TRANSFERS = [
    ("01711111111", "01722222222", 250_000, "Lunch"),          # BDT 2,500.00
    ("01733333333", "01711111111", 75_000, "Bus fare"),        # BDT   750.00
]

DEMO_REQUESTS = [
    ("01722222222", "01711111111", 120_000, "Dinner split"),   # BDT 1,200.00
]

# Every table whose rows belong to a demo run. Keep this explicit instead of
# relying on incidental FK cascades from ``transfers``: features such as group
# wallets and grants can reference a user without having created a transfer.
MUTABLE_TABLES = (
    "overdraft_repayments",
    "overdraft_loans",
    "overdraft_grants",
    "overdraft_pools",
    "safepay_escrows",
    "group_withdrawal_approvals",
    "group_withdrawal_requests",
    "group_members",
    "group_wallets",
    "payment_link_payments",
    "payment_links",
    "scheduled_tasks",
    "ledger_entries",
    "ledger_transactions",
    "money_requests",
    "transfers",
    "idempotency_records",
    "outbox_events",
    "consumer_inbox",
    "audit_logs",
    "sessions",
)


def reset(container) -> None:  # noqa: ANN001
    """Wipe demo data while preserving singleton system accounts."""
    owner_url = container.settings.migration_database_url
    from sqlalchemy import create_engine

    engine = create_engine(owner_url)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(MUTABLE_TABLES)} CASCADE"))
        # USER and OVERDRAFT_POOL accounts both have an owner. The previous
        # account_type='USER' predicate left Spot-Me pool accounts behind, so
        # deleting their owner failed with fk_accounts_user_id. Ownership is
        # the future-safe predicate for every user-owned sub-account.
        conn.execute(
            text("DELETE FROM accounts WHERE user_id IS NOT NULL OR account_type = 'GROUP'")
        )
        conn.execute(text("DELETE FROM users"))
        # Pending/escrow accounts may contain money when a demo is reset. Their
        # ledger rows were truncated above, so every remaining singleton must
        # return to its empty initial state, including its optimistic version.
        conn.execute(
            text("UPDATE accounts SET balance_minor = 0, version = 0 WHERE user_id IS NULL")
        )
    engine.dispose()


def main() -> int:
    settings = get_settings()
    configure_logging(level="WARNING", environment=settings.environment)
    container = build_container(settings)

    print("Resetting demo data...")
    reset(container)

    print("Registering users...")
    for phone, name in DEMO_USERS:
        container.unit_of_work.run(
            lambda s, p=phone, n=name: container.auth_service.register(
                s, phone=p, display_name=n, password=DEMO_PASSWORD, pin=DEMO_PIN
            )
        )
        print(f"  {name:20} {phone}  BDT 100,000.00")

    print("Sending transfers...")
    for from_phone, to_phone, amount_minor, note in DEMO_TRANSFERS:

        def send(session, f=from_phone, t=to_phone, a=amount_minor, n=note):  # noqa: ANN001, ANN202
            sender = container.accounts.get_by_phone(session, f)
            receiver = container.accounts.get_by_phone(session, t)
            return container.transfer_use_case.execute(
                session,
                TransferCommand(
                    actor_user_id=sender.user_id,
                    sender_account_id=sender.id,
                    receiver_account_id=receiver.id,
                    amount=Money.from_minor(a),
                    idempotency_key=new_ulid(),
                    request_fingerprint=fingerprint_request({"to": t, "amount": a}),
                    kind=TransferKind.P2P_SEND,
                    note=n,
                ),
            )

        result = container.unit_of_work.run(send)
        print(f"  {from_phone} -> {to_phone}  BDT {amount_minor / 100:>10,.2f}  {result.reference}")

    print("Creating money requests...")
    for requester_phone, payer_phone, amount_minor, note in DEMO_REQUESTS:

        def ask(session, r=requester_phone, p=payer_phone, a=amount_minor, n=note):  # noqa: ANN001, ANN202
            requester = container.accounts.get_by_phone(session, r)
            return container.money_request_service.create(
                session,
                requester_user_id=requester.user_id,
                payer_phone=p,
                amount=Money.from_minor(a),
                note=n,
            )

        result = container.unit_of_work.run(ask)
        print(
            f"  {requester_phone} asks {payer_phone} for "
            f"BDT {amount_minor / 100:>10,.2f}  {result.reference} (PENDING)"
        )

    report = container.unit_of_work.run(
        lambda s: container.reconciliation_service.run(s)
    )
    print()
    print("Reconciliation after seeding:")
    print(f"  balanced ............... {report.is_balanced}")
    print(f"  balance mismatches ..... {report.balance_mismatches}")
    print(f"  unbalanced postings .... {report.unbalanced_ledger_transactions}")
    print(f"  negative accounts ...... {report.negative_user_accounts}")
    print(f"  ledger sum (must be 0) . {report.system_wide_ledger_sum_minor}")
    print(f"  money in circulation ... BDT {report.money_in_circulation_minor / 100:,.2f}")
    print()
    print("Demo credentials:")
    print(f"  password: {DEMO_PASSWORD}")
    print(f"  PIN:      {DEMO_PIN}")
    for phone, name in DEMO_USERS:
        print(f"  {phone}  {name}")

    container.dispose()
    return 0 if report.is_balanced else 1


if __name__ == "__main__":
    sys.exit(main())
