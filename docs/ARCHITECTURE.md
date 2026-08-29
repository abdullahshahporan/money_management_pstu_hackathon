# Architecture

How this system stays correct, what it does when things fail, and where it goes next.

Every claim below names the test that proves it. Where we have not proved something, we say so.

---

## 1. The one idea

Moving money looks like `A → ৳500 → B`. What makes it hard is everything that happens
*around* that line: the user taps twice, the network drops the response, two payments race
for the same balance, the broker is down, someone sends `"٥"` as an amount.

So the design has a single organising principle:

> **Keep the money-moving transaction boundary strongly consistent and small.
> Everything else is arranged around it.**

Concretely: debit, credit, ledger posting, transfer record, idempotency record and outbox
event all commit together in one PostgreSQL transaction, or none of them do. Nothing else in
the system is allowed to change a balance.

---

## 2. Layering

```
HTTP route  ──▶  application use case  ──▶  domain  ◀── ports
                          │                            │
                          └──────▶ adapters (PostgreSQL, Redis, RabbitMQ)
```

- **domain** — `Money`, `Account`, `LedgerPosting`, policies. Pure Python; imports no
  framework. Unit-testable with no database.
- **application** — use cases that own the transaction boundary and depend on `Protocol`
  ports, never on SQLAlchemy.
- **adapters** — the only code that knows SQL, Redis or AMQP exist.

This is enforced, not merely intended. Four `import-linter` contracts in `pyproject.toml`
fail the build if `financial_core.domain` imports `fastapi`, `sqlalchemy`, `redis`, `pika`
or `psycopg`, or if `application` reaches into `adapters`.

```
$ lint-imports
Financial core domain is framework-free KEPT
All domain layers are framework-free KEPT
Hexagonal layering: domain <- application <- adapters KEPT
Domain never depends on another module's internals KEPT
```

It caught two genuine violations while this was being built — `open_account` importing an
ORM model, and `auth_service` importing concrete repositories. Both were fixed by
introducing a port, which is what the rule is for.

### Dependency injection

Everything is constructed once in `apps/api/container.py` and injected by constructor. No
DI framework and no decorators. The trade-off is a few more lines in exchange for an object
graph you can *read* — for a system whose central claim is that exactly one code path moves
money, being able to see `TransferUseCase` built once, with these collaborators, is worth
more than the brevity.

---

## 3. Money

`Money` is an immutable value object holding **integer minor units** (poisha) plus a
currency. Never a float, never a `Decimal` in storage.

Three decisions worth defending:

**Python's unbounded `int` is the reason this is safe at scale.** The issuance account's
balance at 10 million users is −10¹⁴ poisha. In any float64-based language that is
uncomfortably close to the 2⁵³ exact-integer limit, and aggregate `SUM` queries over the
whole ledger would pass it. Here the arithmetic is simply exact.
→ `tests/unit/test_money.py::TestPrecisionAtScale`

**The parser accepts ASCII digits only.** The first version used `\d`, which in Python is
Unicode-aware: `"٥"` (Arabic-Indic five) matched, and `int()` happily converted it. A caller
could have submitted a visually foreign amount that spent real money. The character class is
now `[0-9]`.
→ `tests/unit/test_money.py::TestParsing::test_non_ascii_digits_are_rejected`

**The transfer cap is a policy, not a property of `Money`.** An early version enforced a
maximum inside the value object, which made the issuance account unrepresentable — the
closed ecosystem could not be modelled by its own money type. Caps belong to
`DefaultLimitPolicy`.

---

## 4. The financial invariants

Every rule is enforced **twice**: the domain rejects it with a useful error, and a database
constraint refuses to persist it even if the application has a bug. The second line is the
one that cannot be bypassed by a code path someone forgot about.

| # | Invariant | Domain | Database |
|---|---|---|---|
| 1 | Amount is positive | `Field(gt=0)` | `CHECK (amount_minor > 0)` |
| 2 | Same currency both sides | `ensure_same_currency_as` | `CHECK` on currency columns |
| 3 | Sender ≠ receiver | use-case guard | `CHECK (sender <> receiver)` |
| 4 | Accounts are active | `ensure_active` | `CHECK (status IN …)` |
| 5 | Actor owns the sender account | `ensure_owned_by` | — |
| 6 | **User balance never negative** | `ensure_can_afford` | `CHECK (type='SYSTEM_ISSUANCE' OR balance_minor >= 0)` |
| 7 | One key + payload → at most one transfer | reserve-then-execute | `PRIMARY KEY (actor, endpoint, key)` |
| 8 | A simple transfer has exactly two entries | `LedgerPosting` | reconciliation |
| 9 | **Signed entries sum to zero** | unconstructable otherwise | reconciliation |
| 10 | A failed transfer changes nothing | one transaction | rollback |
| 11 | **Posted entries are immutable** | no update code path | `REVOKE UPDATE, DELETE` from the runtime role |
| 12 | **Balance == sum of its ledger entries** | — | reconciliation |
| 13 | An accepted request causes ≤ 1 transfer | conditional `UPDATE` | `UNIQUE (transfer_id)` |
| 14 | All timestamps UTC | injectable `Clock` | `TIMESTAMPTZ` |
| 15 | Every mutation has an actor and request id | — | `audit_logs` |

Two are worth expanding.

**Invariant 9 is enforced by making violation unrepresentable.** `LedgerPosting` refuses to
construct unless its lines sum to zero, there are at least two of them, they share a
currency, and no account appears twice. An unbalanced posting is not an error state to
detect — it is a value that cannot exist.

**Invariant 11 is enforced by PostgreSQL privileges, not discipline.** The runtime role
(`mm_app`) can `INSERT` and `SELECT` on `ledger_entries`, `ledger_transactions` and
`audit_logs`, but `UPDATE` and `DELETE` are revoked. Even SQL injection reaching the database
with the application's own credentials cannot rewrite history. Corrections happen only by
posting a compensating reversal.

```
$ psql -U mm_app -c "UPDATE ledger_entries SET amount_minor = 999 WHERE …"
ERROR:  permission denied for table ledger_entries
```

### The opening BDT 100,000

Registration does **not** write `balance_minor = 10000000`. It posts a balanced ledger
transaction debiting `SYSTEM_ISSUANCE_ACCOUNT` and crediting the new user, in the same
transaction that creates the user.

The payoff: the issuance account's negative balance is *exactly* the total money in
circulation, so "no money was created" is one `SUM` over the ledger rather than an act of
faith. It also means a new account is structurally identical to one that earned its balance —
there is no privileged back door that writes balances without double entry.

This mattered in practice. The test factory originally set balances directly, and the
reconciliation assertions correctly failed: those accounts had money with no ledger behind
them. Test setup now goes through `OpenAccountUseCase` like everything else.

---

## 5. Concurrency

`READ COMMITTED` plus explicit `SELECT … FOR UPDATE` on both accounts, **always ordered by
ascending account id**.

```sql
SELECT * FROM accounts WHERE id IN (:a, :b) ORDER BY id FOR UPDATE;
```

The ordering is the deadlock prevention. `Alice → Bob` and `Bob → Alice` running at the same
instant would otherwise grab the two rows in opposite orders and deadlock. Acquiring them in
one agreed order makes that impossible. Deadlocks from elsewhere are still caught: `UnitOfWork`
retries SQLSTATE `40001`/`40P01` a bounded number of times with exponential backoff and full
jitter, and never retries a business rejection — retrying cannot create money.

### The proof

Spec §11.6, stated exactly and run against real PostgreSQL with real threads:

> Sender starts at BDT 10,000. Fire **100 simultaneous** transfers of BDT 200.

```
$ pytest tests/concurrency -v
TestOverdraftRace::test_hundred_simultaneous_withdrawals_cannot_overspend PASSED
TestIdempotencyStorm::test_same_key_fired_concurrently_creates_exactly_one_transfer PASSED
TestBidirectionalDeadlock::test_opposite_direction_transfers_do_not_deadlock_or_corrupt PASSED
```

Asserted: exactly **50 succeed**, exactly **50 rejected for insufficient funds**, sender ends
at **0**, receiver gains exactly BDT 10,000, **50 transfers and 100 ledger entries** exist,
the ledger totals **0**, and no account is negative.

The idempotency storm fires one key from 25 threads simultaneously and asserts exactly one
transfer exists. The deadlock test runs 60 opposite-direction transfers between the same pair
and asserts all 60 succeed with money conserved.

> **Why not an in-process lock?** Two API replicas have two separate memories, so a mutex in
> one grants nothing in the other. Financial concurrency has to be protected at the shared
> data layer. The same reasoning is why rate limits live in Redis rather than in process
> memory — with two replicas an in-memory limiter silently doubles every quota.

---

## 6. Idempotency

Every money mutation requires an `Idempotency-Key`, scoped to `(actor, endpoint, key)` and
bound to a SHA-256 fingerprint of the canonical request body.

```
first use            → PROCEED, reservation owned by this caller
completed, same body → REPLAY the stored response  (HTTP 200, not 201)
completed, different → 409 IDEMPOTENCY_KEY_REUSED
still in flight      → 409 REQUEST_IN_PROGRESS, retryable
```

The concurrency control is `INSERT … ON CONFLICT DO NOTHING … RETURNING`. If a competing
transaction has inserted the key but not committed, PostgreSQL blocks until it resolves, so
we never observe a half-made decision.

Two details that cost real debugging time:

- **`RETURNING`, not `rowcount`.** psycopg3 reports `rowcount = -1` for that statement, so
  branching on it treated *every* first attempt as a duplicate. `RETURNING` yields one row
  when the insert happened and none when it conflicted — unambiguous.
- **Binding the payload matters.** A client reusing a key with a different body has a bug.
  Silently replaying the first response would hide it while the second payment never happens.

The key belongs to the *intent*, not the HTTP attempt. The web client generates it once when
the user commits to the review screen and reuses it across every retry.

→ `tests/integration/test_api_e2e.py::TestSendMoney::test_double_tap_with_same_key_sends_once`

---

## 7. Money requests

A money request is a conversation, not money. It never touches a balance; accepting one calls
the same `TransferUseCase` as any other payment.

It has exactly one non-terminal state, and every transition out of it is a conditional update:

```sql
UPDATE money_requests SET status='ACCEPTED', transfer_id=:t
 WHERE id=:id AND status='PENDING';     -- 0 rows ⇒ someone else won
```

Accept, reject, cancel and the expiry sweep can all race; exactly one wins. Three independent
layers make double-settlement impossible: the row lock, the `status = 'PENDING'` predicate,
and a `UNIQUE` constraint on `transfer_id`.

→ `tests/integration/test_api_e2e.py::TestMoneyRequest::test_accepting_twice_is_refused`

---

## 8. The outbox

Committing to PostgreSQL and then publishing to a broker are two operations that fail
independently. A crash between them leaves a transfer that really happened but that nobody
downstream ever hears about.

So publishing is a *consequence* of the commit rather than a second write: the event row is
inserted in the same transaction as the money. A separate relay publishes it afterwards using
`FOR UPDATE SKIP LOCKED`, so several relays can run without competing for the same rows.

Delivery is **at-least-once, and we say so** rather than claiming exactly-once across a
database and a broker. Consumers insert `(consumer_name, event_id)` into `consumer_inbox`
before applying any effect; the composite primary key makes a duplicate delivery fail
harmlessly, so the *effect* happens exactly once even though delivery did not.

Failures back off exponentially and dead-letter after a bounded number of attempts, so a
poison event cannot loop forever.

**Demonstrated live:**

```
1. RabbitMQ stopped.
   transfer -> HTTP 201  ref=TRX-20260829-M20F1FA6
2. Transfer still committed while the broker was down.
3. Outbox backlog: pending=3 deadLettered=0
4. RabbitMQ restarted.
5. Backlog drained automatically: pending=0 published=22 deadLettered=0
6. Reconciliation still clean: balanced=True ledger_sum=0
```

→ `tests/integration/test_outbox_relay.py` covers all four cases, including dead-lettering.

One subtlety found while testing: a new event's `next_attempt_at` is `NULL`, meaning
"eligible now". Seeding it from the *application's* clock made events ineligible whenever the
app server ran ahead of the database, silently stalling the relay. Only a failure sets a
concrete retry time, and the database sets it from its own `now()`.

---

## 9. Failure behaviour

| Failure | What happens | Why |
|---|---|---|
| Client double-tap | one transfer | durable idempotency key |
| Response lost after commit | reported as **unknown**, never failure | client retries the same key and gets the original receipt |
| Crash before commit | full rollback | one ACID boundary |
| Crash after commit | money stands; event still queued | outbox committed with the money |
| **Broker down** | transfer commits; backlog grows | proven above |
| **Redis down** | login and transfers **fail closed**; safe reads fail open | losing throttling on high-risk endpoints is worse than refusing; money is unaffected either way |
| Notification provider down | transfer unaffected | entirely off the money path |
| **Database down** | `503`, no guessed balance, no fabricated success | readiness fails, load balancer drains the replica |
| Replica lag | never used for a financial decision | balances read from the primary |
| One API replica down | traffic moves to the other | nginx health-aware routing |
| Deadlock / serialization failure | retried whole, with jitter | no partial effect exists to clean up |
| Poison event | dead-lettered after bounded attempts | operator replays after the fault is fixed |

The client-side UX mirrors this. On a timeout the app shows **"Checking transaction status"**,
keeps the same idempotency key, and offers to check again. It never says "failed" about a
payment it cannot see, and never quietly starts a second one.

### CAP, per bounded context

CAP is not one global label for a product. Each context chooses:

| Context | During a partition |
|---|---|
| Financial writes, ledger, balance | **CP** — refuse rather than accept against an unverifiable balance |
| Authentication | consistency and security first |
| Notifications, analytics | AP-friendly, eventually consistent |
| Profile cache | available with bounded staleness |

---

## 10. Security

Argon2id for both password and the separate transaction PIN — a stolen session cannot move
money. Refresh tokens are stored only as SHA-256 hashes and rotate on use; presenting an
already-rotated token means it leaked, so the whole family is revoked. Login returns one
generic message regardless of cause, and hashes even for an unknown phone so response timing
does not reveal which numbers are registered.

PIN verification locks the user row. Without it, simultaneous wrong guesses would each read
the same attempt counter and overwrite one another, turning "lock after five attempts" into
no lockout at all.

Object-level authorization on every resource, returning `404` rather than `403` where the
existence of the record is itself sensitive. Pydantic runs in `strict=True` with
`extra="forbid"`, so unknown fields are rejected and `"2500"` is not silently coerced where an
integer is required. All SQL is parameterized. Logs redact anything whose key looks like a
secret.

Two security fixes made during the build, both worth stating plainly:

- **The Docker image was baking `backend/.env`** — database passwords, JWT secret,
  engineering key — into every layer, and the app then read it in preference to the values
  the orchestrator injected. `.dockerignore` now excludes it.
- **Compose interpolation depended on the invoking directory.** Running `docker compose` from
  `backend/` substituted a developer's `localhost` URLs, and the API came up unable to reach
  Redis, which then *fail-closed* every transfer. Service addresses are now literal.

---

## 11. Observability

Structured JSON logs carrying a request id that flows from nginx through the use case to the
audit row and the outbox event. Health probes are split: **liveness ignores dependencies**
(otherwise a brief database blip would make the orchestrator kill healthy processes and turn
a recoverable incident into an outage); **readiness** reflects database reachability, so an
unhealthy replica leaves the load-balancer rotation while staying up for debugging.

The financial counters that matter are the ones that must always read zero: ledger imbalance,
balance mismatch, negative accounts. `/engineering/reconcile` recomputes all of them from the
raw ledger — it trusts nothing the application says:

```
balanced ....................... True
unbalanced_ledger_transactions.. 0
balance_mismatches.............. 0
negative_user_accounts.......... 0
system_wide_ledger_sum_minor.... 0
money in circulation ........... BDT 1,200,000.00
issuance + user balances ....... 0
```

A logging bug in the notification consumer taught a lesson worth keeping: passing
`extra={"message": …}` raises inside `logging`, which nacked real events to the dead-letter
queue. A log call must never be able to take down a worker handling money, so colliding
`extra` keys are now renamed instead of raising.

---

## 12. Scaling to 10 million users

In order, and only when measurement justifies the next step.

1. **Already done.** Integer money, keyset pagination (never `OFFSET` — it makes PostgreSQL
   walk and discard every skipped row, so page 10,000 costs 10,000× page 1), indexes matched
   to real access patterns, a bounded connection pool, stateless replicas.
2. **Indexes stay honest.** Every index is paid for on each write. An earlier version
   auto-indexed `created_at` on every table; all eight were removed because no query used
   them — history is always scoped to an account first.
3. **Read replicas** for statements and reports. Never for a financial decision, and never
   for the read immediately after a write.
4. **Partition** `ledger_entries` and `audit_logs` by month. They are append-only time series
   and are the tables that grow without bound.
5. **PgBouncer** before scaling replicas further. 100 API instances × 20 connections would
   ask PostgreSQL for 2,000 connections and collapse it.
6. **Extract services** in risk order: notifications first, then analytics, then user
   directory, then identity. The financial core stays intact.
7. **Sharding, last.** Hashing by `user_id` spreads accounts well but puts Alice and Bob on
   different shards, so a transfer crosses two databases and cannot use one local
   transaction. Options — co-locating paired accounts, per-shard clearing accounts with
   asynchronous settlement, or a distributed SQL engine — all trade away something real.
   None is free, and this build does not pretend otherwise.

Because exactly one module moves money, each of these changes touches one place.

**What we have not measured:** throughput at scale. The concurrency suite proves correctness
under contention on a laptop; it is not a capacity claim, and "supports 10 million users"
is not something a laptop run can establish.

---

## 13. Honest limitations

- Notifications are recorded, not delivered — no SMS or push provider is integrated.
- One currency (BDT). The model is currency-aware; only BDT is configured.
- Fees are a `Strategy` with a no-fee implementation. The seam exists; no policy uses it.
- No load test results. Locust is a dependency and the scenarios are not written.
- The web client is deliberately small — the brief weights backend engineering at 90%.
- Timed commands use a durable PostgreSQL scheduler. Production still needs alerting,
  retention and an operator retry UI for terminally failed tasks.
