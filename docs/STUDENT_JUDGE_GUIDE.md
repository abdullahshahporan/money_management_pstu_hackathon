# Taka — student presentation and judge guide

This is the simple explanation of the **whole project**. Use it for your presentation,
demo, and viva. If a judge asks “where is it?”, follow the code location beside the answer.

## 1. Explain the project in one sentence

**Taka is a secure closed-ecosystem digital wallet that keeps every money movement in a
double-entry ledger and remains correct during duplicate clicks, network failure and
simultaneous transactions.**

The special features are:

1. Ten-second Undo for a transfer.
2. SafePay escrow for buyer/seller commerce.
3. Community Spot-Me pool for a small balance shortfall.

It also has registration/login, PIN-protected send, money requests, statements, notifications,
reconciliation and an engineering health panel.

## 2. The main problem and our solution

A normal CRUD wallet may only update two balance fields. That becomes dangerous when two
requests run together, the client retries, or the server crashes halfway through.

Our solution has four rules:

- Money is integer **poisha**, never floating point. BDT 500.00 is `50000` minor units.
- Every movement produces equal debit and credit ledger entries; their signed total is zero.
- Debit, credit, ledger, transfer, idempotency and outbox event commit in one PostgreSQL
  transaction.
- PostgreSQL account row locks serialize competing payments and prevent a negative balance.

Main code:

- Money/account rules: `backend/modules/financial_core/domain/account.py`
- Balanced ledger object: `backend/modules/financial_core/domain/ledger.py`
- The one central transfer path: `backend/modules/financial_core/application/transfer.py`
- Database account/ledger models: `backend/modules/financial_core/adapters/persistence/models.py`
- Real concurrency proofs: `backend/tests/concurrency/test_concurrent_transfers.py`

## 3. High-level architecture

```text
React browser
     |
   nginx  (serves UI and load-balances /api)
     |
     +---- FastAPI replica 1 ----+
     +---- FastAPI replica 2 ----+---- PostgreSQL (financial truth)
                                 +---- Redis (rate limiting only)

PostgreSQL outbox --> outbox relay --> RabbitMQ --> notification worker
PostgreSQL scheduled_tasks --> scheduler worker --> delayed settlement
```

Why this design:

- The API is stateless, so multiple replicas can run.
- PostgreSQL, not API memory, protects money shared by all replicas.
- RabbitMQ failure cannot roll back money because the event first goes to a transactional
  outbox table.
- A durable scheduler, not the user's browser, owns the 10-second and 72-hour timers.

Where it is:

- Application startup/routes: `backend/apps/api/main.py`
- Dependency construction: `backend/apps/api/container.py`
- Nginx/load balancing: `nginx/nginx.conf`
- Containers: `docker-compose.yml`
- Outbox relay: `backend/apps/outbox_relay/main.py`
- Notification consumer: `backend/apps/notification_worker/main.py`
- Durable timer worker: `backend/apps/scheduler_worker/main.py`

## 4. Folder structure in easy language

```text
backend/apps/          entry points: API and background workers
backend/modules/       business features
backend/platform_/     database, security, web and messaging utilities
backend/alembic/       versioned database migrations
backend/tests/         unit, integration and real concurrency tests
frontend/src/          React UI and API client
docs/                  architecture and presentation notes
```

Inside a feature module:

- `domain/` = pure business rules.
- `application/` = use cases/state machines.
- `adapters/persistence/` = SQLAlchemy queries and tables.
- `apps/api/routes/` = HTTP input/output layer.

This separation makes a feature easier to test and scale without mixing UI, SQL and business
logic in one file.

## 5. Registration, login and security

Registration creates the user and their account, then posts the opening balance from a system
issuance account. It does not secretly type a number into `balance_minor`, so the opening money
also has a ledger history.

Login password and transaction PIN are separate. The password creates access/refresh tokens;
the PIN approves financial commands. Refresh tokens rotate, and reuse can revoke the token
family. Repeated wrong PIN attempts persist and can lock the account even though the payment
itself rolls back.

Where it is:

- Auth routes: `backend/apps/api/routes/auth.py`
- Auth workflow: `backend/modules/identity/application/auth_service.py`
- User/session persistence: `backend/modules/identity/adapters/persistence/`
- Opening account and ledger balance: `backend/modules/financial_core/application/open_account.py`
- PIN commit boundary: `backend/apps/api/dependencies.py`

Judge answer:

> Password account-e login kore, PIN financial action authorize kore. Wrong PIN counter-ke
> transaction error-er rollback theke baire commit kora hoy, tai attacker retry kore counter
> erase korte parbe na.

## 6. Ordinary send plus 10-second Undo

Flow:

1. The UI looks up the exact phone number and shows the receiver's name.
2. The sender submits amount, PIN and a client-generated `Idempotency-Key`.
3. The backend verifies owner, PIN, limits and available balance.
4. The money immediately leaves the sender and enters a system `PENDING_SETTLEMENT` account.
5. The hold becomes `PENDING_UNDO`, and a database task is due in 10 seconds.
6. Undo posts a refund. If there is no Undo, the scheduler posts settlement to the receiver.

The client cannot double-spend because the money is physically outside its available account.
The browser may close after Send; the committed scheduled task still exists. Undo and timer
both perform a conditional update from `PENDING_UNDO`, so only one can win.

Where it is:

- Send/Undo endpoints: `backend/apps/api/routes/transfers.py`
- Undo state machine: `backend/modules/financial_core/application/undo.py`
- Shared hold/settlement latch: `backend/modules/financial_core/application/holding.py`
- Idempotency logic: `backend/modules/financial_core/application/idempotency.py`
- Scheduled task repository: `backend/platform_/scheduling/repository.py`
- Countdown and Undo UI: `frontend/src/screens/Send.jsx`

Important honesty: the task is **due** at 10 seconds. A polling worker normally executes at or
just after that deadline; a real operating system cannot promise zero-millisecond scheduling
jitter.

## 7. Idempotency — why duplicate taps are safe

The idempotency identity is `(user, endpoint, key)`, and the key is bound to a fingerprint of
the request body.

- First request reserves the key and executes.
- Same key + same body returns the stored response.
- Same key + different body returns a conflict.
- Another request using a key still in progress is told to retry.

This is stored in PostgreSQL, so it works across two API replicas and after a process restart.
It is not only a disabled frontend button.

Where it is:

- Service: `backend/modules/financial_core/application/idempotency.py`
- Table/repository: `backend/modules/financial_core/adapters/persistence/`
- Client key generation: `frontend/src/api.js`

## 8. SafePay escrow

SafePay is for buyer/seller transactions where neither side should blindly trust the other.

Flow:

1. Buyer creates SafePay; funds move to the real system `ESCROW` account.
2. The buyer gets a six-digit delivery code. Only its Argon2 hash is stored.
3. Seller records courier and tracking number.
4. Funds release by buyer confirmation, seller's correct buyer-provided code, or a trusted
   HMAC-signed courier `DELIVERED` event.
5. A courier event may release immediately or start a 72-hour auto-release task.
6. Buyer can dispute before settlement. The state becomes `DISPUTED`, so funds stay frozen.
7. Admin reviews evidence and performs exactly one release/refund decision. A proven
   fraudulent buyer may also be closed and signed out.

Five wrong delivery-code attempts cause a durable 15-minute lock. Six digits were selected
instead of four because five guesses against 10,000 values is weak for money; the stronger
code does not change the required user flow.

Where it is:

- HTTP and courier signature verification: `backend/apps/api/routes/safepay.py`
- State machine: `backend/modules/safepay/application/service.py`
- Queries/conditional updates: `backend/modules/safepay/adapters/persistence/repositories.py`
- Tables/constraints: `backend/modules/safepay/adapters/persistence/models.py`
- SafePay UI: `frontend/src/screens/Advanced.jsx`

Current integration boundary: a secure generic courier webhook is complete. A production
Pathao/RedX rollout still needs that vendor's credentials and a small payload/auth adapter;
the escrow state machine does not need redesign.

## 9. Community Spot-Me pool

This feature avoids rejecting a payment when a trusted user is short by a small amount.

Flow:

1. Sponsor transfers real money into an `OVERDRAFT_POOL` sub-account.
2. Sponsor grants one trusted beneficiary a maximum single draw, with a hard BDT 500 cap.
3. During Send, if the sender is short, the service draws exactly the missing amount.
4. It creates a zero-interest loan and then creates the normal 10-second transfer hold.
5. Later incoming credits trigger a lien hook. By default, 50% of each incoming credit—up to
   the debt—is repaid to the original pool inside the same transaction.

Why the pool cannot go negative:

- Candidate pool account rows use `SELECT ... FOR UPDATE`.
- All locks are taken in deterministic account-ID order.
- The service checks the locked balance.
- The database has a non-negative account constraint.

If two borrowers race, PostgreSQL lets them inspect/spend the pool sequentially. Draw, loan
and final send share one ACID transaction, so a failed final send leaves no ghost loan.

Where it is:

- Pool/grant API: `backend/apps/api/routes/overdraft.py`
- Exact draw, send and lien: `backend/modules/overdraft/application/service.py`
- Pool/grant/loan repository: `backend/modules/overdraft/adapters/persistence/repositories.py`
- Tables/constraints: `backend/modules/overdraft/adapters/persistence/models.py`
- Central incoming-credit hook: `backend/modules/financial_core/application/transfer.py`
- Shared-pool race test: `backend/tests/concurrency/test_concurrent_transfers.py`
- Spot-Me UI: `frontend/src/screens/Advanced.jsx`

Why 50% repayment: it starts repayment immediately while leaving some salary/refund/emergency
money usable. It is configuration and can be changed to 100% by policy.

## 10. Money request, statement and reconciliation

Money request is not money by itself. A requester creates it; the payer can accept, reject,
or leave it until expiry. Accepting uses the same central transfer use case, PIN protection
and idempotency rules.

Statements use cursor/keyset pagination instead of `OFFSET`, so later pages do not become
linearly slower. Reconciliation independently checks balance-versus-ledger, zero-sum ledger
transactions, negative accounts and total money conservation.

Where it is:

- Request routes: `backend/apps/api/routes/money_requests.py`
- Request workflow: `backend/modules/money_request/application/money_request_service.py`
- Account/statement routes: `backend/apps/api/routes/accounts.py`
- Statement repository: `backend/modules/financial_core/adapters/persistence/repositories.py`
- Integrity checker: `backend/modules/reconciliation/service.py`
- Request/history UI: `frontend/src/screens/Requests.jsx`, `frontend/src/screens/History.jsx`

## 11. Outbox and notification reliability

The API never tries to both commit money and directly publish RabbitMQ in one fragile step.
It inserts an `outbox_events` row in the same database transaction as the transfer. The relay
publishes committed rows later, and the notification worker consumes them.

Therefore:

- database fails -> money and event both roll back;
- broker fails -> money remains correct and event waits in outbox;
- consumer fails -> message can retry/dead-letter without repeating the transfer.

Where it is:

- Outbox persistence: `backend/modules/financial_core/adapters/persistence/`
- Relay: `backend/apps/outbox_relay/main.py`
- Consumer: `backend/apps/notification_worker/main.py`
- Engineering backlog endpoint: `backend/apps/api/routes/system.py`

## 12. Frontend screen map

- `frontend/src/screens/Auth.jsx` — register and login.
- `frontend/src/screens/Home.jsx` — balance, shortcuts and recent activity.
- `frontend/src/screens/Send.jsx` — recipient lookup, send, receipt and Undo timer.
- `frontend/src/screens/Requests.jsx` — create/accept/reject/cancel requests.
- `frontend/src/screens/History.jsx` — paginated statement.
- `frontend/src/screens/Advanced.jsx` — SafePay and Spot-Me.
- `frontend/src/screens/Engineering.jsx` — ledger/outbox/scheduler health and admin disputes.
- `frontend/src/api.js` — every HTTP request and idempotency key.
- `frontend/src/App.jsx` — session state and navigation.

The frontend is intentionally not trusted for financial correctness. Button disabling improves
UX, but authorization, role checks, state transitions and duplicate prevention are server-side.

## 13. Database tables a judge may ask about

- `users`, `sessions` — identity and refresh sessions.
- `accounts` — USER, system, escrow and pool balances.
- `transfers` — business record/reference/status.
- `ledger_transactions`, `ledger_entries` — immutable accounting history.
- `idempotency_keys` — command replay protection.
- `transfer_holds` — Undo and escrow holding state.
- `scheduled_tasks` — 10-second and 72-hour durable timers.
- `escrows`, `escrow_disputes` — SafePay workflow/evidence decision.
- `overdraft_pools`, `overdraft_grants`, `overdraft_loans`, `overdraft_repayments` — Spot-Me.
- `money_requests` — request lifecycle.
- `outbox_events`, `notification_deliveries` — reliable async work.
- `audit_logs` — who did what and when.

Schema code is in each module's `adapters/persistence/models.py`. Database evolution is in
`backend/alembic/versions/`; never edit a production database manually.

## 14. How we scale it

Do these in order:

1. Add stateless API and scheduler replicas. Scheduler workers use `FOR UPDATE SKIP LOCKED`
   so different workers claim different tasks.
2. Put PgBouncer in front of PostgreSQL before hundreds of replicas exhaust connections.
3. Send statement/report reads to replicas, but keep financial decisions on the primary.
4. Partition append-only ledger/audit tables by time and archive old outbox/task rows.
5. Add monitoring for overdue tasks, dead letters, lock latency and reconciliation mismatch.
6. Extract notifications, analytics and identity when their load requires it. Keep the
   strongly consistent financial core together.
7. Shard last. A transfer crossing two database shards loses the simple local ACID guarantee
   and needs clearing accounts, distributed SQL or another explicitly designed protocol.

For SafePay, add one courier adapter per vendor, persist vendor event IDs, rotate keys and
support document/object storage for dispute evidence. For Spot-Me at large scale, route one
pool and all commands touching it to the same shard.

## 15. Three-minute presentation script

> Assalamu alaikum. Our project is Taka, a closed-ecosystem money movement platform. Our main
> focus is not only sending money; it is keeping money correct when users double-click, lose
> network, or send concurrently.
>
> Every amount is integer poisha and every movement creates a balanced double-entry ledger.
> The debit, credit, ledger, idempotency and notification outbox commit in one PostgreSQL
> transaction. Row locking prevents concurrent overspending, and reconciliation proves the
> stored balance still matches the ledger.
>
> Our first advanced feature is 10-second Undo. The sender's money immediately goes to a
> pending account, so it cannot be spent twice. A durable server task settles it after the
> deadline even if the browser disconnects. Undo and settlement race through one conditional
> state update, so only one wins.
>
> Second is SafePay. Buyer money stays in escrow. It releases through buyer confirmation, a
> buyer-only delivery code, or a signed courier delivery event. A dispute freezes funds, and
> admin can perform one audited release or refund decision.
>
> Third is Spot-Me. A trusted sponsor pre-funds a small pool and grants a borrower a limit. If
> the borrower is short, only the exact missing amount is drawn. Later incoming money repays
> the pool automatically. PostgreSQL locks prevent two borrowers from draining beyond the
> real pool balance.
>
> The React frontend is behind nginx and two FastAPI replicas. PostgreSQL is the financial
> truth; Redis handles rate limits, RabbitMQ handles notifications, and a database-backed
> scheduler handles timers. Our automated tests include real concurrent requests against
> PostgreSQL, not only mocked unit tests.

## 16. Recommended live demo order

1. Login as Alice and show balance/history.
2. Send to Bob; show `PENDING_UNDO` and the countdown.
3. Undo it and show the refund/history.
4. Send again, close or refresh the page, wait, then show server-side completion.
5. Create SafePay for Bob, copy the buyer code, switch/login as Bob, ship and release using
   the code. If time permits, create another and show dispute/frozen state.
6. Sponsor a Spot-Me pool and grant Bob. Make Bob's payment larger than current balance and
   show only the shortfall became debt. Send incoming funds to Bob and show automatic repay.
7. Open Engineering and show ledger `balanced=true`, outbox and scheduler backlog.

Seeded demo users and exact commands are in `README.md`.

## 17. Fast viva questions and answers

**Why PostgreSQL instead of only Redis?**

Redis is useful for rate limits/cache, but PostgreSQL gives ACID transactions, constraints,
durable ledger history and row locks. Redis is never the source of money.

**Why not microservices for every module?**

Debit and credit need one atomic transaction. Splitting the financial core early creates a
distributed transaction problem. Async notifications are already separated because temporary
delay there cannot corrupt money.

**If API crashes after debit?**

Debit, credit/hold, ledger and command record are one transaction. Before commit, everything
rolls back; after commit, idempotency can replay and workers can continue durable tasks.

**If Send is pressed twice?**

One client key plus payload produces one command. Same request replays; changed payload is
rejected. The key lives in PostgreSQL, so multiple API instances remain safe.

**Where is the money during Undo or dispute?**

In real system accounts (`PENDING_SETTLEMENT` or `ESCROW`) with balanced ledger postings—not
in a Boolean flag and not missing from accounting.

**Can a seller release SafePay alone?**

Only with the buyer-provided code or a trusted signed courier event. Server-side role and
state checks reject other attempts.

**How is timer reliable after disconnect?**

The database transaction stores a `scheduled_tasks` row. An independent scheduler worker
claims and executes it; no JavaScript timer is trusted.

**Can a Spot-Me loan exist if payment fails?**

No. Pool draw, loan creation and payment hold use one transaction. Any failure rolls back all
of them.

**What is not production-complete?**

Live bank/MFS rails, KYC/AML, SMS/push provider, vendor-specific Pathao/RedX credentials,
multi-region writes and proven load capacity are outside this closed-ecosystem build. We show
the correct integration seams without claiming those external systems are present.

## 18. Verification commands

```powershell
cd backend
python -m pytest tests -q
python -m ruff check .
lint-imports
python -m alembic check

cd ..\frontend
npm run build
```

The important test files are:

- `backend/tests/unit/` — pure business logic.
- `backend/tests/integration/test_api_e2e.py` — end-to-end API behavior.
- `backend/tests/integration/test_advanced_features.py` — Undo, SafePay, Spot-Me and PIN cases.
- `backend/tests/concurrency/test_concurrent_transfers.py` — real PostgreSQL race tests.

For the deeper engineering justification, read `docs/ARCHITECTURE.md`. For only the three
advanced feature state machines, read `docs/ADVANCED_FEATURES.md`.
