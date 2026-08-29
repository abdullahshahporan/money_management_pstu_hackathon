# Advanced Features — implementation and judge guide

This document explains what is actually implemented. All three features still move money
through `TransferUseCase`; they do not write balances directly.

## What changed from the project state we first reviewed

| Feature | Before this implementation | Now |
|---|---|---|
| Ten-second Undo | Hold/service and scheduler table scaffolding existed, but Send was not fully wired to it; there was no complete Undo API/UI or running scheduler container. | Every Send becomes a spend-proof hold, Undo is idempotent, a durable worker settles disconnected clients, and the UI shows the countdown. |
| SafePay | Database/model scaffolding existed, but there was no complete repository, business service, API or usable screen. | Buyer escrow, six-digit code, shipment, signed courier delivery, 72-hour release, dispute freezing, admin queue, release/refund and buyer ban are end-to-end. |
| Spot-Me | Pool/grant/loan tables were only scaffolding; a normal Send could not use them. | Sponsors fund real pool accounts, grants cap access, Send draws the exact shortfall, incoming credits repay debt, and shared-pool races are row-locked and tested. |
| Shared reliability | Multi-account lookup became ambiguous, one scheduler SQL path was invalid, and failed-PIN counters could be rolled back with the rejected command. | User-account lookup is type-scoped, task scheduling uses valid durable SQL, and security failure counters commit before the API returns an error. |

The final UI change was the **SafePay dispute-resolution panel** in
`frontend/src/screens/Engineering.jsx`. The backend resolution endpoint already existed; the
panel now lists every frozen ticket with buyer, seller, claim, courier/tracking evidence and
lets the administrator apply one refund/release decision or close a proven fraudulent buyer.

## 1. Ten-second delayed settlement

Flow:

1. `POST /transfers` authenticates the user and verifies the transaction PIN.
2. The amount moves from the sender into the singleton `PENDING_SETTLEMENT` account.
3. The transfer is recorded as `PENDING_UNDO`; a durable task is due after 10 seconds.
4. Undo changes the hold to `REFUNDED` and posts a separate refund transfer.
5. If no Undo wins, the scheduler changes the hold to `SUCCEEDED` and posts a separate
   settlement transfer to the receiver.

Easy state picture:

```text
Send -> PENDING_UNDO -> SUCCEEDED (worker after deadline)
                    -> REFUNDED  (user presses Undo)
```

The amount is not only marked “locked”; it leaves the sender balance. Therefore another
concurrent send cannot spend it. Undo and the worker race on one conditional status update,
so exactly one outcome can win.

Main code locations:

- HTTP send/undo: `backend/apps/api/routes/transfers.py`
- State machine: `backend/modules/financial_core/application/undo.py`
- Shared hold/settle latch: `backend/modules/financial_core/application/holding.py`
- Durable task table/repository: `backend/platform_/scheduling/`
- Worker: `backend/apps/scheduler_worker/main.py`
- UI countdown and stable Undo key: `frontend/src/screens/Send.jsx`

Judge answers:

- **Network disconnect হলে কী হবে?** The API and timer are not connected to the browser.
  `scheduled_tasks` is committed with the hold, so another worker/process settles it later.
- **দুইবার Undo চাপলে?** The endpoint requires a client idempotency key and stores the first
  response. The refund leg also has a deterministic internal key.
- **Undo আর timer একই সময়ে চললে?** `UPDATE ... WHERE status='PENDING_UNDO'`; one transaction
  changes one row, the loser changes zero rows and cannot move money.

## 2. Conditional SafePay

Flow:

1. Buyer creates SafePay; money moves into the system `ESCROW` account.
2. A six-digit buyer-only delivery code is derived with HMAC and only its Argon2 hash is
   stored in the escrow row.
3. Seller records courier and tracking number.
4. Settlement can happen through buyer confirmation, seller submission of the buyer's code,
   or an HMAC-signed courier `DELIVERED` event.
5. A courier event can release immediately or start the 72-hour dispute window. A durable
   scheduled task releases after that window.
6. Buyer dispute changes the escrow to `DISPUTED` and cancels auto-release. Admin can release
   to seller or refund buyer; a proven fraudulent buyer can also be closed and signed out.

Easy state picture:

```text
AWAITING_SHIPMENT -> SHIPPED -> DELIVERED -> RELEASED
          |             |          |
          +-------------+----------+-> DISPUTED -> RELEASED or REFUNDED
```

Five incorrect delivery-code attempts create a durable 15-minute lock. The route commits the
failure counter before returning the HTTP error, so exception rollback cannot erase it.

Main code locations:

- API and courier HMAC verification: `backend/apps/api/routes/safepay.py`
- State machine: `backend/modules/safepay/application/service.py`
- Escrow queries and conditional updates: `backend/modules/safepay/adapters/persistence/repositories.py`
- Database constraints: `backend/modules/safepay/adapters/persistence/models.py`
- Buyer/seller UI: `frontend/src/screens/Advanced.jsx`
- Admin dispute panel: `frontend/src/screens/Engineering.jsx`

Judge answers:

- **Seller নিজে fund release করতে পারবে?** Not without a buyer-provided code or a trusted
  signed courier event. Status and role checks are server-side.
- **Buyer ইচ্ছা করে টাকা আটকে রাখলে?** Confirmed delivery can release immediately; otherwise
  the configured auto-release window ends the hold if no dispute exists.
- **Dispute হলে টাকা কোথায়?** Still in the real ESCROW account. `DISPUTED` freezes the state;
  no refund or seller credit exists until one admin decision wins.
- **Pathao/RedX integration কোথায়?** The secure generic webhook boundary is implemented.
  Production adds each vendor's authentication/payload adapter and credentials without
  changing the escrow state machine.

## 3. Community Spot-Me pool

Flow:

1. Sponsor moves real BDT from their main account into an `OVERDRAFT_POOL` sub-account.
2. Sponsor grants a beneficiary a maximum single draw (hard system cap BDT 500).
3. During Send, the service locks the sender, pending-settlement account and candidate pool
   accounts in deterministic order.
4. If the main balance is short, it draws exactly the missing amount, creates a zero-interest
   loan, then creates the ordinary 10-second hold. All steps share one transaction.
5. Every later incoming credit invokes the lien hook. By default, 50% of that credit (up to
   outstanding debt) is moved back to the original pool before the transaction commits.

Easy money picture:

```text
Sponsor main balance -> Spot-Me pool
Spot-Me pool --exact shortfall--> Borrower --full payment--> Receiver/Undo hold
Borrower's later incoming credit --configured lien share--> Original Spot-Me pool
```

No credit is created. The pool is a normal non-negative account, and each draw/repayment is a
balanced ledger posting. If ten borrowers race for one pool, the pool account row lock
serialises them; once its balance reaches zero, remaining requests fail.

Main code locations:

- API: `backend/apps/api/routes/overdraft.py`
- Composite send, exact draw and lien hook: `backend/modules/overdraft/application/service.py`
- Pool/grant/loan/repayment queries: `backend/modules/overdraft/adapters/persistence/repositories.py`
- Database constraints: `backend/modules/overdraft/adapters/persistence/models.py`
- Central credit-hook call: `backend/modules/financial_core/application/transfer.py`
- Real PostgreSQL race proof: `backend/tests/concurrency/test_concurrent_transfers.py`
- UI: `frontend/src/screens/Advanced.jsx`

Judge answers:

- **Pool negative হবে না কীভাবে?** PostgreSQL `SELECT ... FOR UPDATE`, an application balance
  check, and a database non-negative check enforce the rule at three levels.
- **Send fail হলে loan থেকে যাবে?** No. Draw, loan and final hold are one ACID transaction;
  any later failure rolls all three back.
- **Repayment bypass করা যাবে?** Every incoming transfer passes the central transfer use case.
  The lien repayment runs inside that same transaction before the credit becomes visible.
- **কেন 50%?** It repays immediately but leaves part of wages/refunds/emergency help usable.
  The basis points are configuration and can become 100% if product/legal policy requires.

## Scaling these features

- Run multiple API and scheduler replicas. `FOR UPDATE SKIP LOCKED` gives each scheduler a
  different batch; account row locks still protect money.
- Indexes already target due tasks, escrow user/status lists, beneficiary grants and
  outstanding borrower loans.
- Partition old ledger/audit rows by time, use read replicas only for statements/reports, and
  add PgBouncer before increasing API replica count heavily.
- Add a vendor adapter per courier, store processed vendor event IDs for longer retention,
  rotate webhook keys, and alert on overdue/failed scheduled tasks.
- At much larger scale, route all commands touching one pool/account to the same database
  shard. Cross-shard transfers need a different consistency design and are intentionally not
  claimed here.

## Verification

The advanced integration suite covers hold/Undo replay, disconnected-client settlement,
SafePay code release, durable code lockout, dispute/refund, immediate courier release,
72-hour scheduled release, exact Spot-Me shortfall, lien repayment and PIN lockout. The
concurrency suite fires simultaneous draws against one real PostgreSQL pool and verifies that
only the funded number succeed.
